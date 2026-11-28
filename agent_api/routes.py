import datetime as dt
import logging
import os
import time

from flask import Blueprint, g, jsonify, make_response, request

from .auth import AuthError, COOKIE_NAME, authenticate as _authenticate, cookie_options, redeem, assert_origin
from .bubble_client import BubbleUnavailable
from .cache import dashboard_cache, reference_cache
from .serializers import inactive, inventory, profile, relation_id, relation_ids

blueprint = Blueprint("agent_api", __name__, url_prefix="/api/agent")
logger = logging.getLogger(__name__)

DISPLAY_FIELDS = ("name", "Name", "Condo name", "title")


class RelationshipResolver:
    """Request-scoped, bulk relationship resolver shared by every serializer."""
    def __init__(self, client):
        self.client = client
        self.names = {"condo": {}, "geo": {}, "agency": {}}
        self.loaded = {"condo": set(), "geo": set(), "agency": set()}

    def resolve(self, object_type, ids):
        wanted = {str(value) for value in ids if value}
        ttl = int(os.getenv("AGENT_REFERENCE_CACHE_TTL_SECONDS", "3600"))
        for value in tuple(wanted):
            label, hit = reference_cache.get((object_type, value))
            add_cache(hit)
            if hit:
                self.loaded[object_type].add(value)
                if label:
                    self.names[object_type][value] = label
        missing = wanted - self.loaded[object_type]
        if missing:
            started = time.perf_counter()
            rows = self.client.many(object_type, missing)
            add_timing("relationship", time.perf_counter() - started)
            self.loaded[object_type].update(missing)
            for row in rows:
                label = next((row.get(field) for field in DISPLAY_FIELDS if row.get(field)), None)
                if row.get("_id") and label:
                    key = str(row["_id"]); self.names[object_type][key] = str(label)
                    reference_cache.set((object_type, key), str(label), ttl)
            for value in missing:
                if value not in self.names[object_type]:
                    reference_cache.set((object_type, value), "", ttl)
        return self.names[object_type]

    def collect(self, records=(), users=()):
        condo_ids, geo_ids, agency_ids = [], [], []
        for record in records:
            for field in ("development", "condo", "preferredDevelopments", "preferredCondos"):
                condo_ids.extend(relation_ids(record.get(field)))
            geo_ids.extend(relation_ids(record.get("Geo")))
        for user in users:
            value = relation_id((user or {}).get("Agency"))
            if value:
                agency_ids.append(value)
        self.resolve("condo", condo_ids)
        self.resolve("geo", geo_ids)
        self.resolve("agency", agency_ids)
        return self.names


def resolver(client):
    current = getattr(g, "relationship_resolver", None)
    if current is None:
        current = g.relationship_resolver = RelationshipResolver(client)
    return current


def prefetch_match_records(client, rows, cache):
    ids = {"lead": set(), "listing": set()}
    for row in rows:
        for kind in ids:
            value = relation_id(row.get(kind))
            if value and (kind, value) not in cache:
                ids[kind].add(value)
    started = time.perf_counter()
    for kind, values in ids.items():
        for record in client.many(kind, values):
            cache[(kind, record.get("_id"))] = record
        for value in values:
            cache.setdefault((kind, value), None)
    owner_ids = {
        relation_id(record.get("owner"))
        for (kind, _record_id), record in cache.items()
        if kind in {"lead", "listing"} and record and relation_id(record.get("owner"))
    }
    for user in client.many("user", owner_ids):
        cache[("user", user.get("_id"))] = user
    for value in owner_ids:
        cache.setdefault(("user", value), None)
    add_timing("relationship", time.perf_counter() - started)


def add_timing(name, seconds):
    timings = getattr(g, "agent_timings", None)
    if timings is not None:
        timings[name] = timings.get(name, 0.0) + seconds


def add_cache(hit):
    timings = getattr(g, "agent_timings", None)
    if timings is not None:
        key = "cache_hits" if hit else "cache_misses"
        timings[key] = timings.get(key, 0) + 1


def authenticate(client=None):
    """Attach Bubble counters to this request without changing auth behavior."""
    result = _authenticate(client)
    g.bubble_client = result[2]
    return result


@blueprint.errorhandler(AuthError)
def auth_error(exc): return jsonify(error=str(exc)), exc.status


@blueprint.errorhandler(BubbleUnavailable)
def bubble_error(_exc): return jsonify(error="Data is temporarily unavailable"), 503


@blueprint.after_request
def cors(response):
    from .config import permitted_origins
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin in permitted_origins():
        response.headers.update({"Access-Control-Allow-Origin":origin,"Access-Control-Allow-Credentials":"true","Referrer-Policy":"no-referrer","Cache-Control":"no-store"})
        response.vary.add("Origin")
    started = getattr(g, "agent_started", None)
    if started is not None:
        total = time.perf_counter() - started
        client = getattr(g, "bubble_client", None)
        timings = getattr(g, "agent_timings", {})
        logger.info(
            "Agent API timing endpoint=%s status=%s total_ms=%.1f bubble_requests=%s bubble_ms=%.1f records_retrieved=%s records_returned=%s relationship_ms=%.1f match_filter_ms=%.1f stats_ms=%.1f cache_hits=%s cache_misses=%s",
            request.endpoint or "unknown", response.status_code, total * 1000,
            getattr(client, "request_count", 0), getattr(client, "request_seconds", 0.0) * 1000,
            getattr(client, "records_retrieved", 0), timings.get("records_returned", 0),
            timings.get("relationship", 0.0) * 1000, timings.get("match_filter", 0.0) * 1000,
            timings.get("stats", 0.0) * 1000, timings.get("cache_hits", 0), timings.get("cache_misses", 0),
        )
    return response


@blueprint.before_request
def options():
    """Answer every Agent API preflight before route auth can run.

    Flask's automatic OPTIONS handlers take precedence over a catch-all OPTIONS
    route, so this must run at blueprint level to cover every Agent endpoint.
    """
    g.agent_started = time.perf_counter()
    g.agent_timings = {}
    if request.method != "OPTIONS":
        return None
    assert_origin()
    response = make_response("", 204)
    response.headers.update({"Access-Control-Allow-Methods":"GET, POST, OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
    return response


@blueprint.route("/<path:_path>", methods=["OPTIONS"])
def options_route(_path):
    # Retain the explicit OPTIONS-only rule so unsupported Agent paths keep
    # their previous method-not-allowed behavior for non-OPTIONS requests.
    return options()


def matched_records(client, row, uid, cache=None):
    """Resolve a match and recheck participation against current inventory owners."""
    if not row:
        return None
    score = row.get("match_score")
    if not isinstance(score, (int, float)) or score < int(os.getenv("AGENT_INBOX_MIN_SCORE", "80")):
        return None
    lead_id, listing_id = relation_id(row.get("lead")), relation_id(row.get("listing"))
    if not lead_id or not listing_id:
        return None
    cache = cache if cache is not None else {}
    def cached(kind, record_id):
        key=(kind,record_id)
        if key not in cache: cache[key]=client.one(kind,record_id)
        return cache[key]
    started = time.perf_counter()
    lead, listing = cached("lead", lead_id), cached("listing", listing_id)
    add_timing("relationship", time.perf_counter() - started)
    if not lead or not listing:
        return None
    lead_owner, listing_owner = relation_id(lead.get("owner")), relation_id(listing.get("owner"))
    if uid not in {lead_owner, listing_owner} or not lead_owner or not listing_owner or lead_owner == listing_owner:
        return None
    side = "lead" if lead_owner == uid else "listing"
    return lead, listing, lead_owner, listing_owner, side


def _created_at(row):
    try:
        value = dt.datetime.fromisoformat(str(row.get("Created Date") or "").replace("Z", "+00:00"))
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    except (TypeError, ValueError):
        return None


def _related_matches(client, kind, record_ids):
    ids = list(dict.fromkeys(str(value) for value in record_ids if value))
    rows = []
    for offset in range(0, len(ids), 50):
        group = ids[offset:offset + 50]
        constraint = {"key": kind, "constraint_type": "equals" if len(group) == 1 else "in",
                      "value": group[0] if len(group) == 1 else group}
        rows.extend(client.all("match", [constraint], None))
    return rows


def match_pipeline(client, uid, owned_listings=None, owned_leads=None):
    """Return historical/actionable matches and safe stage diagnostics."""
    started = time.perf_counter()
    owned_listings = owned_listings if owned_listings is not None else client.all("listing", [{"key":"owner", "constraint_type":"equals", "value":uid}], None)
    owned_leads = owned_leads if owned_leads is not None else client.all("lead", [{"key":"owner", "constraint_type":"equals", "value":uid}], None)
    by_lead_owner = client.all("match", [{"key":"lead_owner", "constraint_type":"equals", "value":uid}], None)
    by_listing_owner = client.all("match", [{"key":"listing_owner", "constraint_type":"equals", "value":uid}], None)
    discovered = _related_matches(client, "listing", [row.get("_id") for row in owned_listings])
    discovered += _related_matches(client, "lead", [row.get("_id") for row in owned_leads])
    combined = by_lead_owner + by_listing_owner + discovered
    keys = ("raw_lead_owner", "raw_listing_owner", "inventory_discovered", "combined_before_filtering",
            "ownership_excluded", "missing_score_excluded", "below_score_excluded", "inactive_excluded",
            "age_excluded_actionable", "review_excluded", "duplicates_removed", "final_historical", "final_actionable")
    counts = dict.fromkeys(keys, 0)
    counts.update(raw_lead_owner=len(by_lead_owner), raw_listing_owner=len(by_listing_owner),
                  inventory_discovered=len(discovered), combined_before_filtering=len(combined))
    records = {}
    for row in combined:
        key = row.get("_id") or id(row)
        if key in records: counts["duplicates_removed"] += 1
        else: records[key] = row
    cache = {}; prefetch_match_records(client, records.values(), cache)
    qualified = []; threshold = int(os.getenv("AGENT_INBOX_MIN_SCORE", "80"))
    for row in records.values():
        score = row.get("match_score")
        if isinstance(score, bool) or not isinstance(score, (int, float)): counts["missing_score_excluded"] += 1; continue
        if score < threshold: counts["below_score_excluded"] += 1; continue
        lead_id, listing_id = relation_id(row.get("lead")), relation_id(row.get("listing"))
        lead, listing = cache.get(("lead", lead_id)), cache.get(("listing", listing_id))
        lead_owner = relation_id((lead or {}).get("owner")); listing_owner = relation_id((listing or {}).get("owner"))
        if not lead or not listing or not lead_owner or not listing_owner or lead_owner == listing_owner or uid not in {lead_owner, listing_owner}:
            counts["ownership_excluded"] += 1; continue
        if inactive(lead, "lead") or inactive(listing, "listing"):
            counts["inactive_excluded"] += 1; continue
        side = "lead" if lead_owner == uid else "listing"
        qualified.append((row, (lead, listing, lead_owner, listing_owner, side)))
    unique = {}
    for row, resolved in qualified:
        pair = (relation_id(row.get("listing")), relation_id(row.get("lead"))); current = unique.get(pair)
        if current is None or (str(row.get("Created Date") or ""), str(row.get("_id") or "")) > (str(current[0].get("Created Date") or ""), str(current[0].get("_id") or "")):
            if current is not None: counts["duplicates_removed"] += 1
            unique[pair] = (row, resolved)
        else: counts["duplicates_removed"] += 1
    historical = list(unique.values()); actionable = []
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
    for row, resolved in historical:
        created = _created_at(row)
        if created is None or created < cutoff: counts["age_excluded_actionable"] += 1; continue
        if row.get(f"{resolved[-1]}_review_status"): counts["review_excluded"] += 1; continue
        actionable.append((row, resolved))
    counts.update(final_historical=len(historical), final_actionable=len(actionable))
    logger.info("Agent match diagnostics " + " ".join(f"{key}=%d" for key in keys), *(counts[key] for key in keys))
    add_timing("match_filter", time.perf_counter() - started)
    return historical, actionable, cache


def matched_inventory(client, record, kind, owner_id, cache=None):
    cache = cache if cache is not None else {}
    key=("user",owner_id)
    started = time.perf_counter()
    if key not in cache: cache[key]=client.one("user",owner_id)
    add_timing("relationship", time.perf_counter() - started)
    relationships = resolver(client).collect([record], [cache[key]])
    return inventory(record, kind, cache[key], True, relationships)


def deduplicated_match_rows(client, uid):
    historical, _actionable, _cache = match_pipeline(client, uid)
    return [row for row, _resolved in historical]


@blueprint.post("/auth/redeem")
def redeem_route():
    payload = request.get_json(silent=True) or {}
    token, days = redeem(payload.get("token"))
    response = jsonify(ok=True); response.set_cookie(COOKIE_NAME, token, **cookie_options(days)); return response


@blueprint.post("/auth/logout")
def logout():
    assert_origin(); _user, session, client = authenticate()
    client.patch("AgentSession", session["_id"], {"revoked":True})
    confirmed = client.one("AgentSession", session["_id"])
    if not confirmed or confirmed.get("revoked") is not True: return jsonify(error="Logout could not be confirmed"), 503
    response = jsonify(ok=True); response.delete_cookie(COOKIE_NAME, path="/api/agent"); return response


@blueprint.get("/me")
def me():
    user, _session, client = authenticate(); relationships=resolver(client).collect(users=[user]); return jsonify(profile(user,relationships))


def page_args():
    default = int(os.getenv("AGENT_PAGE_SIZE", "20"))
    try: return max(1,int(request.args.get("page","1"))), min(100,max(1,int(request.args.get("limit",str(default)))))
    except ValueError: raise AuthError("Invalid pagination", 400)


def own_inventory(kind):
    user, _session, client = authenticate(); page, limit = page_args()
    constraints=[{"key":"owner","constraint_type":"equals","value":user["_id"]}]
    status=request.args.get("status")
    if status and status != "all":
        constraints.append({"key":"active","constraint_type":"equals","value":status != "inactive"})
    rows, remaining = client.search(kind, constraints, limit, (page-1)*limit, "Created Date", True)
    relationships=resolver(client).collect(rows,[user])
    items=[inventory(row,kind,user,True,relationships) for row in rows]
    g.agent_timings["records_returned"]=len(items)
    return jsonify(items=items, page=page, page_size=limit, has_more=remaining>0, next_page=page+1 if remaining>0 else None)


@blueprint.get("/listings")
def listings(): return own_inventory("listing")


@blueprint.get("/leads")
def leads(): return own_inventory("lead")


def own_detail(kind, record_id):
    user, _session, client = authenticate(); row = client.one(kind, record_id)
    if not row or relation_id(row.get("owner")) != user["_id"]: return jsonify(error="Not found"), 404
    relationships=resolver(client).collect([row],[user])
    return jsonify(inventory(row,kind,user,True,relationships))


@blueprint.get("/listings/<record_id>")
def listing_detail(record_id): return own_detail("listing",record_id)


@blueprint.get("/leads/<record_id>")
def lead_detail(record_id): return own_detail("lead",record_id)


@blueprint.get("/matches")
def matches():
    user, _session, client = authenticate(); uid=user["_id"]; page, limit=page_args()
    valid, actionable, cache = match_pipeline(client,uid)
    actionable_ids={row.get("_id") for row,_resolved in actionable}
    ordered=sorted(valid,key=lambda item:(item[0].get("match_score") or 0,item[0].get("Created Date") or ""),reverse=True)
    selected=ordered[(page-1)*limit:page*limit]
    selected_record_ids={relation_id(row.get(kind)) for row,_resolved in selected for kind in ("lead","listing")}
    related_records=[value for (kind,record_id),value in cache.items() if kind in {"lead","listing"} and record_id in selected_record_ids and value]
    selected_owner_ids={relation_id(record.get("owner")) for record in related_records}
    related_users=[value for (kind,record_id),value in cache.items() if kind=="user" and record_id in selected_owner_ids and value]
    resolver(client).collect(related_records,related_users)
    items=[]
    for row, resolved in selected:
        lead, listing, lead_owner, listing_owner, side = resolved
        items.append({"id":row.get("_id"),"listing_id":relation_id(row.get("listing")),"lead_id":relation_id(row.get("lead")),"score":row.get("match_score"),"reason":row.get("match_reason") or [],"created_at":row.get("Created Date"),"review_status":row.get(f"{side}_review_status") or "","actionable":row.get("_id") in actionable_ids,"listing":matched_inventory(client,listing,"listing",listing_owner,cache),"lead":matched_inventory(client,lead,"lead",lead_owner,cache)})
    g.agent_timings["records_returned"]=len(items)
    return jsonify(items=items,page=page,page_size=limit,has_more=page*limit<len(ordered),next_page=page+1 if page*limit<len(ordered) else None,total=len(ordered))


@blueprint.get("/matches/<record_id>")
def match_detail(record_id):
    user,_session,client=authenticate(); row=client.one("match",record_id); uid=user["_id"]
    resolved=matched_records(client,row,uid)
    if not resolved: return jsonify(error="Not found"),404
    lead,listing,lead_owner,listing_owner,side=resolved
    return jsonify(id=row.get("_id"),score=row.get("match_score"),reason=row.get("match_reason") or [],created_at=row.get("Created Date"),review_status=row.get(f"{side}_review_status") or "",listing=matched_inventory(client,listing,"listing",listing_owner),lead=matched_inventory(client,lead,"lead",lead_owner))


@blueprint.get("/matches/<record_id>/contact")
def match_contact(record_id):
    user,_session,client=authenticate(); row=client.one("match",record_id); uid=user["_id"]
    resolved=matched_records(client,row,uid)
    if not resolved: return jsonify(error="Not found"),404
    _lead,_listing,lead_owner,listing_owner,side=resolved
    owner_id=listing_owner if side == "lead" else lead_owner
    owner=client.one("user",owner_id)
    phone=(owner or {}).get("phone")
    if not phone: return jsonify(error="Contact is unavailable"),404
    relationships=resolver(client).collect(users=[owner])
    owner_profile=profile(owner,relationships)
    return jsonify(agent={"name":owner_profile["name"],"agency":owner_profile["agency"]},phone=phone)


@blueprint.get("/home")
def home():
    user,_session,client=authenticate(); uid=user["_id"]
    cache_key=("home",uid)
    cached,hit=dashboard_cache.get(cache_key); add_cache(hit)
    if hit:
        g.agent_timings["records_returned"]=1
        return jsonify(cached)
    # The network collections already contain the current agent's inventory;
    # reuse them instead of issuing two repeated owner queries.
    stats_started=time.perf_counter()
    network_listings=client.all("listing",[],5000); network_leads=client.all("lead",[],5000); network_matches=client.all("match",[],5000)
    listings=[x for x in network_listings if relation_id(x.get("owner"))==uid]
    leads=[x for x in network_leads if relation_id(x.get("owner"))==uid]
    relationships=resolver(client).collect(listings+leads,[user])
    sl=[inventory(x,"listing",user,False,relationships) for x in listings]; sd=[inventory(x,"lead",user,False,relationships) for x in leads]
    valid_rows, actionable_rows, _cache=match_pipeline(client,uid,listings,leads)
    valid=[(row,resolved[-1]) for row,resolved in valid_rows]
    actionable=len(actionable_rows)
    myt=dt.timezone(dt.timedelta(hours=8)); local_now=dt.datetime.now(dt.timezone.utc).astimezone(myt); week_start=(local_now-dt.timedelta(days=local_now.weekday())).date()
    def this_week(row):
        try: return dt.datetime.fromisoformat(str(row.get("Created Date") or "").replace("Z","+00:00")).astimezone(myt).date() >= week_start
        except ValueError: return False
    network_listing_views=[inventory(x,"listing",None,False,relationships) for x in network_listings]; network_lead_views=[inventory(x,"lead",None,False,relationships) for x in network_leads]
    network_unique={}
    for row in network_matches:
        pair=(relation_id(row.get("listing")),relation_id(row.get("lead")))
        if not all(pair): continue
        current=network_unique.get(pair)
        if current is None or (str(row.get("Created Date") or ""),str(row.get("_id"))) > (str(current.get("Created Date") or ""),str(current.get("_id"))): network_unique[pair]=row
    owners={relation_id(x.get("owner")) for x in network_listings+network_leads if relation_id(x.get("owner"))}
    network={"active_listings":sum(x["active"] for x in network_listing_views),"active_leads":sum(x["active"] for x in network_lead_views),"agents":len(owners),"matches_this_week":sum(this_week(x) for x in network_unique.values())}
    add_timing("stats",time.perf_counter()-stats_started)
    recent=sorted(valid,key=lambda item:str(item[0].get("Created Date") or ""),reverse=True)[:5]
    payload=dict(profile=profile(user,relationships),active_listings=sum(x["active"] for x in sl),active_leads=sum(x["active"] for x in sd),listings_needing_confirmation=sum(x["freshness"]=="needs_confirmation" for x in sl),leads_needing_confirmation=sum(x["freshness"]=="needs_confirmation" for x in sd),actionable_matches=actionable,matches_this_week=sum(this_week(row) for row,_side in valid),lifetime_matches=len(valid),network=network,recent_activities=[{"id":row.get("_id"),"created_at":row.get("Created Date"),"score":row.get("match_score")} for row,_side in recent])
    dashboard_cache.set(cache_key,payload,int(os.getenv("AGENT_DASHBOARD_CACHE_TTL_SECONDS","60")))
    g.agent_timings["records_returned"]=1
    return jsonify(payload)
