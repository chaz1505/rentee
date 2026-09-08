"""Deterministic WhatsApp workflow for agents creating Bubble Listings."""

from dataclasses import dataclass
import datetime
import re
from typing import Optional


RENT_TRANSACTION = "Rent/Let"
BUY_TRANSACTION = "Buy/Sell"
ACTIVE_SKILL = "create_listing"


@dataclass
class ListingCreationResult:
    handled: bool
    response_text: str
    listing_id: Optional[str] = None
    published: bool = False
    cancelled: bool = False


def is_listing_creation_intent(text):
    normalized = " ".join(str(text or "").casefold().split())
    return bool(re.search(
        r"\b(new listing|create (?:a )?listing|add (?:a )?listing|new unit|"
        r"add this property|new (?:bungalow|terrace|semi[- ]?d|property))\b",
        normalized,
    ))


def _normalized_name(value):
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).split())


def _records(base_url, object_type, bubble_records):
    return [item for item in bubble_records(base_url, object_type) if item.get("_id")]


def _resolve_named_relationship(text, records):
    normalized_text = _normalized_name(text)
    matches = []
    for record in records:
        name = next((record.get(key) for key in ("name", "Name", "Condo name")
                     if record.get(key)), None)
        normalized_name = _normalized_name(name)
        if normalized_name and re.search(
            rf"(?<!\w){re.escape(normalized_name)}(?!\w)", normalized_text
        ):
            matches.append((len(normalized_name), record, str(name)))
    if not matches:
        return None, None, False
    longest = max(item[0] for item in matches)
    best = [item for item in matches if item[0] == longest]
    if len(best) != 1:
        return None, None, True
    return best[0][1]["_id"], best[0][2], False


def _relationship_name(records, relationship_id):
    for record in records:
        if str(record.get("_id") or "") != str(relationship_id or ""):
            continue
        return next((str(record[key]) for key in ("name", "Name", "Condo name")
                     if record.get(key)), None)
    return None


def extract_listing_updates(text, existing=None):
    """Extract only explicit Malaysian listing shorthand from one message."""
    raw = " ".join(str(text or "").split())
    normalized = raw.casefold()
    updates = {}
    transaction = None
    explicit_transaction = False
    if re.search(r"\b(for sale|sale|sell)\b", normalized):
        transaction = BUY_TRANSACTION
        explicit_transaction = True
    elif re.search(r"\b(for rent|rent|rental|to let)\b", normalized):
        transaction = RENT_TRANSACTION
        explicit_transaction = True
    price_match = re.search(
        r"(?:\brm\s*)?\b([0-9]+(?:\.[0-9]+)?)\s*([km])\b", normalized
    )
    if price_match:
        price = int(float(price_match.group(1)) * {"k": 1000, "m": 1000000}[price_match.group(2)])
        existing_transactions = (existing or {}).get("TransactionType") or []
        existing_transaction = (
            existing_transactions[0]
            if isinstance(existing_transactions, list) and existing_transactions
            else None
        )
        transaction = transaction or existing_transaction or RENT_TRANSACTION
        updates["priceSale" if transaction == BUY_TRANSACTION else "priceRent"] = price
    if transaction and (explicit_transaction or not (existing or {}).get("TransactionType")):
        updates["TransactionType"] = [transaction]
    size = re.search(r"\b([0-9][0-9,]*)\s*(?:sf|sq\s*ft|sqft)\b", normalized)
    if size:
        updates["Sq Ft"] = int(size.group(1).replace(",", ""))
    beds = re.search(r"\b(\d+)\s*(?:\+\s*(\d+))?\s*(?:bed(?:room)?s?)?\b", normalized)
    if beds and (beds.group(2) or "bed" in beds.group(0)):
        updates["beds"] = int(beds.group(1))
    furnishing = None
    if re.search(r"\b(ff|fully furnished)\b", normalized):
        furnishing = "Fully Furnished"
    elif re.search(r"\b(pf|partially furnished|partly furnished)\b", normalized):
        furnishing = "Partially Furnished"
    elif re.search(r"\b(uf|unfurnished)\b", normalized):
        furnishing = "Unfurnished"
    if furnishing:
        updates["Furnishing"] = furnishing
    if re.search(r"\b(avail(?:able)?(?: now)?|available immediately|immediate(?:ly)?)\b", normalized):
        updates["availability"] = True
        updates["availability_date"] = datetime.date.today().isoformat()
    date_match = re.search(r"\bavailable\s+([0-3]?\d\s+[a-z]{3,9}(?:\s+\d{4})?)\b", normalized)
    if date_match:
        updates["availability"] = True
        rendered_date = date_match.group(1).title()
        for format_string in ("%d %b %Y", "%d %B %Y", "%d %b", "%d %B"):
            try:
                parsed = datetime.datetime.strptime(rendered_date, format_string).date()
                if "%Y" not in format_string:
                    parsed = parsed.replace(year=datetime.date.today().year)
                    if parsed < datetime.date.today():
                        parsed = parsed.replace(year=parsed.year + 1)
                updates["availability_date"] = parsed.isoformat()
                break
            except ValueError:
                continue
    if re.search(r"\b(bungalow|terrace|semi[- ]?d|landed(?: house)?)\b", normalized):
        updates["propertyType"] = "Landed"
    elif re.fullmatch(r"(?:a )?(?:condo|condominium|apartment)", normalized):
        updates["propertyType"] = "Condo"
    unit = re.fullmatch(r"(?:unit\s*)?([a-z0-9]+(?:[-/][a-z0-9]+)+)", normalized)
    if unit:
        updates["unitNumber"] = unit.group(1).upper()
    return updates


def _display_price(listing):
    value = listing.get("priceSale") or listing.get("priceRent")
    if not value:
        return None
    return f"RM{float(value) / 1000:g}k" if float(value) < 1000000 else f"RM{float(value) / 1000000:g}m"


def listing_summary(listing, condo_name=None, geo_name=None):
    parts = [condo_name or geo_name or "Listing"]
    for value in (
        listing.get("unitNumber"),
        f"{listing['beds']} bed" if listing.get("beds") is not None else None,
        f"{int(listing['Sq Ft']):,} sqft" if listing.get("Sq Ft") else None,
        _display_price(listing), listing.get("Furnishing"),
        "available now" if listing.get("availability") is True else None,
        f"{len(listing.get('photos') or [])} photos" if listing.get("photos") else None,
    ):
        if value:
            parts.append(str(value))
    return " · ".join(parts)


def _next_response(listing, condo_name=None, geo_name=None, no_photos=False):
    if not listing.get("condo") and not listing.get("Geo"):
        return "Which condo or area is the property in?"
    if not listing.get("propertyType"):
        return "Is it a condo or landed property?"
    if not listing.get("unitNumber"):
        return f"Got it — {listing_summary(listing, condo_name, geo_name)}. Which unit is it?"
    if not listing.get("TransactionType"):
        return "Is this listing for rent or for sale?"
    modes = listing.get("TransactionType") or []
    if RENT_TRANSACTION in modes and not listing.get("priceRent"):
        return "What is the monthly rent?"
    if BUY_TRANSACTION in modes and not listing.get("priceSale"):
        return "What is the sale price?"
    if not listing.get("photos") and not no_photos:
        return "Any photos for this one?"
    return f"{listing_summary(listing, condo_name, geo_name)}. Publish?"


def handle_listing_creation(
    text, conversation, user_id, base_url, *, bubble_create, bubble_patch,
    bubble_get, bubble_records, image_url=None,
):
    """Start or continue one Listing identified by Conversation.Listing."""
    conversation = dict(conversation or {})
    conversation_id = str(conversation.get("_id") or "").strip()
    active = str(conversation.get("ActiveSkill") or "").strip() == ACTIVE_SKILL
    if not active and not is_listing_creation_intent(text):
        return ListingCreationResult(False, "")
    if not conversation_id or not user_id:
        raise ValueError("Listing creation requires Conversation and User identity.")
    normalized = " ".join(str(text or "").casefold().split())
    listing_id = str(conversation.get("Listing") or "").strip()
    if active and re.fullmatch(r"(?:cancel|stop|forget it|never mind|nevermind)", normalized):
        bubble_patch(f"{base_url}/obj/conversation/{conversation_id}", {"ActiveSkill": ""})
        return ListingCreationResult(True, "Okay — I’ve cancelled this listing.", listing_id, cancelled=True)
    if active and re.fullmatch(r"(?:yes|yep|correct|publish|go ahead|looks good)", normalized):
        listing = bubble_get(f"{base_url}/obj/listing/{listing_id}")
        modes = listing.get("TransactionType") or []
        publishable = bool(
            (listing.get("condo") or listing.get("Geo"))
            and listing.get("propertyType") and listing.get("unitNumber")
            and modes
            and ((RENT_TRANSACTION in modes and listing.get("priceRent"))
                 or (BUY_TRANSACTION in modes and listing.get("priceSale")))
        )
        if publishable:
            bubble_patch(f"{base_url}/obj/conversation/{conversation_id}", {"ActiveSkill": ""})
            condos = _records(base_url, "condo", bubble_records)
            development = _relationship_name(condos, listing.get("condo"))
            name = " ".join(filter(None, (
                development, str(listing.get("unitNumber") or "").strip()
            ))) or "listing"
            return ListingCreationResult(True, f"Done — {name} is added.", listing_id, published=True)
        return ListingCreationResult(True, _next_response(listing), listing_id)

    condos = _records(base_url, "condo", bubble_records)
    geos = _records(base_url, "geo", bubble_records)
    condo_id, condo_name, condo_ambiguous = _resolve_named_relationship(text, condos)
    geo_id, geo_name, geo_ambiguous = _resolve_named_relationship(text, geos)
    if condo_ambiguous or (not condo_id and geo_ambiguous):
        return ListingCreationResult(True, "Which exact condo or area do you mean?", listing_id or None)
    if not listing_id:
        payload = {"owner": user_id}
        if condo_id:
            payload.update({"condo": condo_id, "propertyType": "Condo"})
        elif geo_id:
            payload["Geo"] = geo_id
        payload.update(extract_listing_updates(text))
        listing_id = bubble_create(base_url, "listing", payload)
        bubble_patch(f"{base_url}/obj/conversation/{conversation_id}", {
            "ActiveSkill": ACTIVE_SKILL, "Listing": listing_id,
        })
        listing = {"_id": listing_id, **payload}
    else:
        listing = bubble_get(f"{base_url}/obj/listing/{listing_id}")
        condo_name = condo_name or _relationship_name(condos, listing.get("condo"))
        geo_name = geo_name or _relationship_name(geos, listing.get("Geo"))
        updates = extract_listing_updates(text, listing)
        if condo_id:
            updates.update({"condo": condo_id, "propertyType": "Condo"})
        elif geo_id:
            updates["Geo"] = geo_id
        if image_url:
            photos = list(listing.get("photos") or [])
            if image_url not in photos:
                photos.append(image_url)
            updates["photos"] = photos
            if not listing.get("coverPhoto"):
                updates["coverPhoto"] = image_url
        if updates:
            bubble_patch(f"{base_url}/obj/listing/{listing_id}", updates)
            listing.update(updates)
    no_photos = bool(re.search(
        r"\b(no photos?|none yet|do not have any|don't have any)\b", normalized
    ))
    response = _next_response(listing, condo_name, geo_name, no_photos)
    return ListingCreationResult(True, response, listing_id)
