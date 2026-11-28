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
    rows = client.all("match", [{"key":"lead_owner","constraint_type":"equals","value":uid}], 500)+client.all("match", [{"key":"listing_owner","constraint_type":"equals","value":uid}], 500)
    unique={}
    for row in rows:
        lead, listing = relation_id(row.get("lead")), relation_id(row.get("listing")); score=row.get("match_score")
        if not lead or not listing or not isinstance(score,(int,float)) or score < int(os.getenv("AGENT_INBOX_MIN_SCORE","80")): continue
        key=(listing,lead); current=unique.get(key)
        if current is None or (str(row.get("Created Date") or ""),str(row.get("_id"))) > (str(current.get("Created Date") or ""),str(current.get("_id"))): unique[key]=row
    items=[]
    for row in unique.values():
        lead = client.one("lead", relation_id(row.get("lead")))
        listing = client.one("listing", relation_id(row.get("listing")))
        if not lead or not listing:
            continue
        lead_owner, listing_owner = relation_id(lead.get("owner")), relation_id(listing.get("owner"))
        if uid not in {lead_owner, listing_owner} or lead_owner == listing_owner:
            continue
        side="lead" if lead_owner==uid else "listing"
        items.append({"id":row.get("_id"),"listing_id":relation_id(row.get("listing")),"lead_id":relation_id(row.get("lead")),"score":row.get("match_score"),"reason":row.get("match_reason") or [],"created_at":row.get("Created Date"),"review_status":row.get(f"{side}_review_status") or "","listing":inventory(listing,"listing",include_private=listing_owner==uid),"lead":inventory(lead,"lead",include_private=lead_owner==uid)})
    items.sort(key=lambda x:(x["score"],x["created_at"] or ""),reverse=True)
    return jsonify(items=items)


@blueprint.get("/matches/<record_id>")
def match_detail(record_id):
    user,_session,client=authenticate(); row=client.one("match",record_id); uid=user["_id"]
    if not row: return jsonify(error="Not found"),404
    lead=client.one("lead",relation_id(row.get("lead"))); listing=client.one("listing",relation_id(row.get("listing")))
    lead_owner=relation_id((lead or {}).get("owner")); listing_owner=relation_id((listing or {}).get("owner"))
    if uid not in {lead_owner,listing_owner}: return jsonify(error="Not found"),404
    # Restrictive default: only own record carries private message; counterpart is teaser.
    return jsonify(id=row.get("_id"),score=row.get("match_score"),reason=row.get("match_reason") or [],created_at=row.get("Created Date"),listing=inventory(listing,"listing",include_private=listing_owner==uid),lead=inventory(lead,"lead",include_private=lead_owner==uid))


@blueprint.get("/home")
def home():
    user,_session,client=authenticate(); uid=user["_id"]
    listings=client.all("listing",[{"key":"owner","constraint_type":"equals","value":uid}],1000); leads=client.all("lead",[{"key":"owner","constraint_type":"equals","value":uid}],1000)
    sl=[inventory(x,"listing",user,False) for x in listings]; sd=[inventory(x,"lead",user,False) for x in leads]
    return jsonify(profile=profile(user),active_listings=sum(x["active"] for x in sl),active_leads=sum(x["active"] for x in sd),listings_needing_confirmation=sum(x["freshness"]=="needs_confirmation" for x in sl),leads_needing_confirmation=sum(x["freshness"]=="needs_confirmation" for x in sd))
