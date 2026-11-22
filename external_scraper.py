"""Feature-gated client for Rentee's separately deployed iProperty scraper.

This module deliberately does not schedule work.  ``run_external_lookup`` is a
worker-friendly unit that can be called by durable job infrastructure once one
is available.
"""

from __future__ import annotations

import os
import math
from urllib.parse import urlparse

import requests


LOG_PREFIX = "[EXTERNAL SCRAPER]"
EXPECTED_DEVELOPMENT = "One Menerung"
EXPECTED_SOURCE = "iproperty"
EXPECTED_HOSTS = {"iproperty.com.my", "www.iproperty.com.my"}
REQUEST_TIMEOUT_SECONDS = 165


def external_scraper_enabled() -> bool:
    """Return true only for the feature flag's explicit ``true`` value."""
    return os.getenv("EXTERNAL_SCRAPER_ENABLED", "").strip().lower() == "true"


def _number(value, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    if positive and value <= 0:
        return None
    if nonnegative and value < 0:
        return None
    return value


def _relationship_ids(value) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        identifier = item.get("_id") if isinstance(item, dict) else item
        if identifier is not None and str(identifier).strip():
            result.append(str(identifier))
    return result


def build_external_lookup(lead, matches, development_records, lead_id,
                          classify_match):
    """Build a scrape request for the single supported no-Tier-1 lead case."""
    if not external_scraper_enabled():
        return None
    transactions = set(lead.get("TransactionType") or [])
    if "Rent/Let" not in transactions or "Buy/Sell" in transactions:
        return None

    development_ids = _relationship_ids(lead.get("preferredDevelopments"))
    if len(development_ids) != 1:
        return None
    by_id = {
        str(record.get("_id")): record
        for record in development_records or [] if record.get("_id")
    }
    development_record = by_id.get(development_ids[0])
    development = str(
        (development_record or {}).get("name")
        or (development_record or {}).get("Name")
        or ""
    ).strip()
    if development.casefold() != EXPECTED_DEVELOPMENT.casefold():
        return None

    budget = _number(lead.get("budgetRent"), positive=True)
    bedrooms = _number(lead.get("bedroomsMin"), nonnegative=True)
    if budget is None or bedrooms is None:
        return None
    if any(classify_match(lead, listing) == 1 for listing in matches or []):
        return None

    return {
        "lead_id": str(lead_id),
        "development": EXPECTED_DEVELOPMENT,
        "bedrooms_min": bedrooms,
        "budget_rent": budget,
    }


def _valid_listing_url(value) -> bool:
    try:
        parsed = urlparse(str(value or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and parsed.hostname in EXPECTED_HOSTS


def fetch_external_matches(lookup, *, request_post=requests.post):
    """Fetch and validate up to three matches; return ``None`` on failure."""
    if not external_scraper_enabled():
        return None
    try:
        url = os.environ["EXTERNAL_SCRAPER_URL"]
        api_key = os.environ["EXTERNAL_SCRAPER_API_KEY"]
        response = request_post(
            url,
            headers={"Content-Type": "application/json", "X-API-Key": api_key},
            json={
                "development": lookup["development"],
                "bedrooms_min": lookup["bedrooms_min"],
                "budget_rent": lookup["budget_rent"],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
    except (KeyError, requests.RequestException, ValueError, TypeError) as error:
        print(f"{LOG_PREFIX} action=fetch_failed error={type(error).__name__}",
              flush=True)
        return None

    if not isinstance(body, dict):
        print(f"{LOG_PREFIX} action=invalid_response reason=not_object", flush=True)
        return None
    if (str(body.get("source") or "").casefold() != EXPECTED_SOURCE
            or str(body.get("development") or "").strip().casefold()
            != lookup["development"].casefold()
            or not isinstance(body.get("matches"), list)):
        print(f"{LOG_PREFIX} action=invalid_response reason=metadata", flush=True)
        return None

    accepted = []
    seen_ids = set()
    for item in body["matches"]:
        if not isinstance(item, dict):
            continue
        listing_id = str(item.get("source_listing_id") or "").strip()
        price = _number(item.get("price_rent"), positive=True)
        beds = _number(item.get("beds"), nonnegative=True)
        if (not listing_id or listing_id in seen_ids or price is None or beds is None
                or price > lookup["budget_rent"]
                or beds < lookup["bedrooms_min"]
                or not _valid_listing_url(item.get("source_url"))):
            continue
        clean = {
            "source_listing_id": listing_id,
            "source_url": item["source_url"],
            "price_rent": price,
            "beds": beds,
        }
        for field in ("agent", "sq_ft", "description"):
            if item.get(field) is not None:
                clean[field] = item[field]
        seen_ids.add(listing_id)
        accepted.append(clean)
        if len(accepted) == 3:
            break
    return accepted


def _amount(value) -> str:
    return f"{value:,.0f}" if isinstance(value, (int, float)) else str(value)


def format_external_matches(development, matches) -> str:
    """Format a standalone WhatsApp response using validated API values only."""
    if not matches:
        return (
            f"🔎 I checked iProperty for {development} but couldn't find any "
            "additional listings matching this requirement."
        )
    lines = [
        f"🔎 *{len(matches)} additional "
        f"{'listing' if len(matches) == 1 else 'listings'} found on iProperty*",
        "",
        f"*{development}*",
    ]
    for index, listing in enumerate(matches, 1):
        details = [f"RM{_amount(listing['price_rent'])}/month",
                   f"{_amount(listing['beds'])} bed"]
        sq_ft = _number(listing.get("sq_ft"), positive=True)
        if sq_ft is not None:
            details.append(f"{_amount(sq_ft)} sqft")
        lines.extend(["", f"*{index}. {' · '.join(details)}*"])
        agent = str(listing.get("agent") or "").strip()
        if agent:
            lines.append(f"Agent: {agent}")
        lines.append(listing["source_url"])
    lines.extend([
        "",
        "_External listings from iProperty. Availability has not been verified by Rentee._",
    ])
    return "\n".join(lines)


def run_external_lookup(lookup, phone, *, send_text, save_message,
                        request_post=requests.post):
    """Execute one already-deduplicated job and persist its outbound segments.

    Durable infrastructure must claim the stable job key
    ``external_iproperty:<lead_id>`` before calling this function.
    """
    matches = fetch_external_matches(lookup, request_post=request_post)
    if matches is None:
        return {"status": "failed", "sent_ids": []}
    message = format_external_matches(lookup["development"], matches)
    sent_ids = send_text(phone, message) or []
    for meta_id in sent_ids:
        if not meta_id:
            continue
        save_message(
            phone, meta_id, message, "Outbound", "text", "live",
            lead_id=lookup["lead_id"],
        )
    return {
        "status": "sent", "matches": matches,
        "sent_ids": [item for item in sent_ids if item],
    }
