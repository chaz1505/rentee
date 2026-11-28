import datetime as dt
import os
from zoneinfo import ZoneInfo

MYT = ZoneInfo("Asia/Kuala_Lumpur")


def relation_id(value):
    if isinstance(value, dict): return value.get("_id")
    return value


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


def inventory(record, kind, owner=None, include_private=True):
    is_listing = kind == "listing"
    price = record.get("priceRent") if str(record.get("TransactionType") or "").casefold().find("rent") >= 0 else record.get("priceSale")
    if not is_listing: price = record.get("budgetRent") if str(record.get("TransactionType") or "").casefold().find("rent") >= 0 else record.get("budgetBuy")
    result = {"id":record.get("_id"),"kind":kind,"title":text(record.get("development")) or text(record.get("preferredDevelopments")) or text(record.get("Geo")) or kind.title(),"location":text(record.get("Geo")),"transaction_type":text(record.get("TransactionType")),"property_type":text(record.get("propertyType" if is_listing else "propertyTypes")),"bedrooms":record.get("beds" if is_listing else "bedroomsMin"),"bathrooms":record.get("baths" if is_listing else "bathroomsMin"),"price":price,"furnishing":text(record.get("Furnishing" if is_listing else "furnishingPreference")),"created_at":date(record.get("Created Date")),"last_confirmed_active":date(record.get("last_confirmed_active")),"active":not inactive(record,kind),"freshness":freshness(record,kind),"owner":{"id":relation_id(record.get("owner")),"name":text(owner and (owner.get("name") or owner.get("Name"))),"agency":text(owner and owner.get("Agency"))}}
    if include_private: result["original_message"] = record.get("waMessage") or None
    return result


def profile(user):
    return {"id":user.get("_id"),"name":user.get("name") or user.get("Name") or "Agent","agency":text(user.get("Agency")),"phone":user.get("phone") or None}
