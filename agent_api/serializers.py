import datetime as dt
import os
from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")


def relation_id(value):
    if isinstance(value, dict): return value.get("_id")
    return value


def relation_ids(value):
    values = value if isinstance(value, list) else [value]
    return [str(item) for item in (relation_id(value) for value in values) if item]


def text(value):
    if isinstance(value, dict): return value.get("name") or value.get("Name") or ""
    return value or ""


def date(value): return str(value or "")


def inactive(record, kind):
    status = str(record.get("status") or "").strip().casefold()
    if kind == "listing": return str(record.get("availability_option") or "").strip().casefold() == "no" or status == "closed"
    return record.get("cancelled") is True or status in {"closed won", "closed lost"}


def freshness(record, kind):
    if inactive(record, kind): return "inactive"
    threshold = int(os.getenv("AGENT_INVENTORY_FRESHNESS_DAYS", "30"))
    raw = record.get("last_confirmed_active") or record.get("Created Date")
    if not raw: return "needs_confirmation"
    try: when = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00")); fresh = dt.datetime.now(dt.timezone.utc)-when <= dt.timedelta(days=threshold)
    except ValueError: fresh = False
    if record.get("last_confirmed_active"): return "confirmed_active" if fresh else "needs_confirmation"
    return "recently_added" if fresh else "needs_confirmation"


def inventory(record, kind, owner=None, include_private=True, relationships=None):
    relationships = relationships or {}
    development_ids = []
    for field in (("development", "condo") if kind == "listing" else ("preferredDevelopments", "preferredCondos")):
        development_ids.extend(relation_ids(record.get(field)))
    geo_ids = relation_ids(record.get("Geo"))
    developments = [relationships.get("condo", {}).get(value) for value in dict.fromkeys(development_ids)]
    geos = [relationships.get("geo", {}).get(value) for value in dict.fromkeys(geo_ids)]
    developments = [value for value in developments if value]
    geos = [value for value in geos if value]
    is_listing = kind == "listing"
    price = record.get("priceRent") if str(record.get("TransactionType") or "").casefold().find("rent") >= 0 else record.get("priceSale")
    if not is_listing: price = record.get("budgetRent") if str(record.get("TransactionType") or "").casefold().find("rent") >= 0 else record.get("budgetBuy")
    size = record.get("Sq Ft") if is_listing else None
    availability = record.get("availability_date")
    owner_name = text(owner and (owner.get("name") or owner.get("Name"))) or "Rentee agent"
    agency_value = owner and owner.get("Agency")
    agency_id = relation_id(agency_value)
    agency = relationships.get("agency", {}).get(str(agency_id)) if agency_id else ""
    if not agency and isinstance(agency_value, str) and (" " in agency_value or len(agency_value) < 18):
        agency = agency_value
    result = {"id":record.get("_id"),"kind":kind,"title":developments[0] if developments else (geos[0] if geos else ("Listing" if is_listing else "Lead")),"location":", ".join(geos) or "Area not specified","developments":developments,"transaction_type":text(record.get("TransactionType")),"property_type":text(record.get("propertyType" if is_listing else "propertyTypes")),"bedrooms":record.get("beds" if is_listing else "bedroomsMin"),"bathrooms":record.get("baths" if is_listing else "bathroomsMin"),"size":size,"price":price,"furnishing":text(record.get("Furnishing" if is_listing else "furnishingPreference")),"availability":date(availability),"created_at":date(record.get("Created Date")),"last_confirmed_active":date(record.get("last_confirmed_active")),"active":not inactive(record,kind),"freshness":freshness(record,kind),"owner":{"id":relation_id(record.get("owner")),"name":owner_name,"agency":agency or "Agency not provided"}}
    if include_private: result["original_message"] = record.get("waMessage") or None
    return result


def profile(user, relationships=None):
    relationships = relationships or {}
    agency_value = user.get("Agency")
    agency_id = relation_id(agency_value)
    agency = relationships.get("agency", {}).get(str(agency_id)) if agency_id else ""
    if not agency and isinstance(agency_value, str) and (" " in agency_value or len(agency_value) < 18): agency = agency_value
    return {"id":user.get("_id"),"name":user.get("name") or user.get("Name") or "Agent","agency":agency or "Agency not provided","phone":user.get("phone") or None}
