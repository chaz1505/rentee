import datetime as dt
import os

from flask import Blueprint, jsonify, make_response, request

from .auth import AuthError, COOKIE_NAME, authenticate, cookie_options, redeem, assert_origin
from .bubble_client import BubbleUnavailable
from .serializers import inventory, profile, relation_id

blueprint = Blueprint("agent_api", __name__, url_prefix="/api/agent")


@blueprint.errorhandler(AuthError)
def auth_error(exc): return jsonify(error=str(exc)), exc.status


@blueprint.errorhandler(BubbleUnavailable)
def bubble_error(_exc): return jsonify(error="Data is temporarily unavailable"), 503


@blueprint.after_request
def cors(response):
    from .config import permitted_origins
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin in permitted_origins():
        response.headers.update({"Access-Control-Allow-Origin":origin,"Access-Control-Allow-Credentials":"true","Vary":"Origin","Referrer-Policy":"no-referrer","Cache-Control":"no-store"})
    return response


@blueprint.route("/<path:_path>", methods=["OPTIONS"])
def options(_path):
    assert_origin(); response = make_response("", 204)
    response.headers.update({"Access-Control-Allow-Methods":"GET, POST, OPTIONS","Access-Control-Allow-Headers":"Content-Type"})
    return response


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
    lead, listing = cached("lead", lead_id), cached("listing", listing_id)
    if not lead or not listing:
        return None
    lead_owner, listing_owner = relation_id(lead.get("owner")), relation_id(listing.get("owner"))
    if uid not in {lead_owner, listing_owner} or not lead_owner or not listing_owner or lead_owner == listing_owner:
        return None
    side = "lead" if lead_owner == uid else "listing"
    return lead, listing, lead_owner, listing_owner, side


def matched_inventory(client, record, kind, owner_id, cache=None):
    cache = cache if cache is not None else {}
    key=("user",owner_id)
    if key not in cache: cache[key]=client.one("user",owner_id)
    return inventory(record, kind, cache[key], True)


def deduplicated_match_rows(client, uid):
    rows = client.all("match", [{"key":"lead_owner","constraint_type":"equals","value":uid}], 500)+client.all("match", [{"key":"listing_owner","constraint_type":"equals","value":uid}], 500)
    unique={}
    for row in rows:
        lead, listing = relation_id(row.get("lead")), relation_id(row.get("listing")); score=row.get("match_score")
        if not lead or not listing or not isinstance(score,(int,float)) or score < int(os.getenv("AGENT_INBOX_MIN_SCORE","80")): continue
        key=(listing,lead); current=unique.get(key)
        if current is None or (str(row.get("Created Date") or ""),str(row.get("_id"))) > (str(current.get("Created Date") or ""),str(current.get("_id"))): unique[key]=row
    return list(unique.values())


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
    user, _session, _client = authenticate(); return jsonify(profile(user))


def page_args():
    try: return max(1,int(request.args.get("page","1"))), min(100,max(1,int(request.args.get("limit","30"))))
    except ValueError: raise AuthError("Invalid pagination", 400)


def own_inventory(kind):
    user, _session, client = authenticate(); page, limit = page_args()
    rows, remaining = client.search(kind, [{"key":"owner","constraint_type":"equals","value":user["_id"]}], limit, (page-1)*limit, "Created Date", True)
    return jsonify(items=[inventory(row,kind,user,True) for row in rows], page=page, has_more=remaining>0)


@blueprint.get("/listings")
def listings(): return own_inventory("listing")


@blueprint.get("/leads")
def leads(): return own_inventory("lead")


def own_detail(kind, record_id):
    user, _session, client = authenticate(); row = client.one(kind, record_id)
    if not row or relation_id(row.get("owner")) != user["_id"]: return jsonify(error="Not found"), 404
    return jsonify(inventory(row,kind,user,True))


@blueprint.get("/listings/<record_id>")
def listing_detail(record_id): return own_detail("listing",record_id)


@blueprint.get("/leads/<record_id>")
def lead_detail(record_id): return own_detail("lead",record_id)


@blueprint.get("/matches")
def matches():
    user, _session, client = authenticate(); uid=user["_id"]
    rows = deduplicated_match_rows(client,uid); cache={}
    items=[]
    for row in rows:
        resolved = matched_records(client, row, uid, cache)
        if not resolved:
            continue
        lead, listing, lead_owner, listing_owner, side = resolved
        items.append({"id":row.get("_id"),"listing_id":relation_id(row.get("listing")),"lead_id":relation_id(row.get("lead")),"score":row.get("match_score"),"reason":row.get("match_reason") or [],"created_at":row.get("Created Date"),"review_status":row.get(f"{side}_review_status") or "","listing":matched_inventory(client,listing,"listing",listing_owner,cache),"lead":matched_inventory(client,lead,"lead",lead_owner,cache)})
    items.sort(key=lambda x:(x["score"],x["created_at"] or ""),reverse=True)
    return jsonify(items=items)


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
    return jsonify(agent={"name":owner.get("name") or owner.get("Name") or "Agent","agency":owner.get("Agency") or ""},phone=phone)


@blueprint.get("/home")
def home():
    user,_session,client=authenticate(); uid=user["_id"]
    listings=client.all("listing",[{"key":"owner","constraint_type":"equals","value":uid}],1000); leads=client.all("lead",[{"key":"owner","constraint_type":"equals","value":uid}],1000)
    sl=[inventory(x,"listing",user,False) for x in listings]; sd=[inventory(x,"lead",user,False) for x in leads]
    rows=deduplicated_match_rows(client,uid); cache={}; valid=[]
    for row in rows:
        resolved=matched_records(client,row,uid,cache)
        if resolved: valid.append((row,resolved[-1]))
    actionable=sum(not (row.get(f"{side}_review_status") or "") for row,side in valid)
    myt=dt.timezone(dt.timedelta(hours=8)); local_now=dt.datetime.now(dt.timezone.utc).astimezone(myt); week_start=(local_now-dt.timedelta(days=local_now.weekday())).date()
    def this_week(row):
        try: return dt.datetime.fromisoformat(str(row.get("Created Date") or "").replace("Z","+00:00")).astimezone(myt).date() >= week_start
        except ValueError: return False
    network_listings=client.all("listing",[],5000); network_leads=client.all("lead",[],5000); network_matches=client.all("match",[],5000)
    network_listing_views=[inventory(x,"listing",None,False) for x in network_listings]; network_lead_views=[inventory(x,"lead",None,False) for x in network_leads]
    network_unique={}
    for row in network_matches:
        pair=(relation_id(row.get("listing")),relation_id(row.get("lead")))
        if not all(pair): continue
        current=network_unique.get(pair)
        if current is None or (str(row.get("Created Date") or ""),str(row.get("_id"))) > (str(current.get("Created Date") or ""),str(current.get("_id"))): network_unique[pair]=row
    owners={relation_id(x.get("owner")) for x in network_listings+network_leads if relation_id(x.get("owner"))}
    network={"active_listings":sum(x["active"] for x in network_listing_views),"active_leads":sum(x["active"] for x in network_lead_views),"agents":len(owners),"matches_this_week":sum(this_week(x) for x in network_unique.values())}
    return jsonify(profile=profile(user),active_listings=sum(x["active"] for x in sl),active_leads=sum(x["active"] for x in sd),listings_needing_confirmation=sum(x["freshness"]=="needs_confirmation" for x in sl),leads_needing_confirmation=sum(x["freshness"]=="needs_confirmation" for x in sd),actionable_matches=actionable,matches_this_week=sum(this_week(row) for row,_side in valid),lifetime_matches=len(valid),network=network)
