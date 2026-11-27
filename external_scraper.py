"""Feature-gated client for Rentee's separately deployed iProperty scraper.

This module deliberately does not schedule work.  ``run_external_lookup`` is a
worker-friendly unit that can be called by durable job infrastructure once one
is available.
"""

from __future__ import annotations

import os
import math
import datetime
import json
import uuid
from urllib.parse import urlparse

import requests


LOG_PREFIX = "[EXTERNAL SCRAPER]"
EXPECTED_SOURCE = "iproperty"
EXPECTED_HOSTS = {"iproperty.com.my", "www.iproperty.com.my"}
REQUEST_TIMEOUT_SECONDS = 165
JOB_TYPE = "external_lookup_job"
MAX_JOB_ATTEMPTS = 4
DEFAULT_EXTERNAL_SCRAPER_MAX_AGE_DAYS = 7


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


def is_scraped_listing(listing) -> bool:
    """Recognize Bubble yes/no values plus legacy string serialization."""
    value = (listing or {}).get("scraped?")
    if value is True:
        return True
    if isinstance(value, str) and value.strip().casefold() in {"true", "yes"}:
        return True
    return str((listing or {}).get("source") or "").strip().casefold() == EXPECTED_SOURCE


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
    """Build one scrape request per resolved development without a Tier 1 match."""
    if not external_scraper_enabled():
        return []
    transactions = set(lead.get("TransactionType") or [])
    if "Rent/Let" not in transactions or "Buy/Sell" in transactions:
        return []

    development_ids = list(dict.fromkeys(
        _relationship_ids(lead.get("preferredDevelopments"))
    ))
    by_id = {
        str(record.get("_id")): record
        for record in development_records or [] if record.get("_id")
    }

    budget = _number(lead.get("budgetRent"), positive=True)
    bedrooms = _number(lead.get("bedroomsMin"), nonnegative=True)
    if budget is None or bedrooms is None:
        return []
    internal_matches = [
        listing for listing in matches or []
        if not is_scraped_listing(listing)
    ]
    lookups = []
    for development_id in development_ids:
        development_record = by_id.get(development_id)
        development = str(
            (development_record or {}).get("name")
            or (development_record or {}).get("Name")
            or ""
        ).strip()
        if not development:
            print(
                f"{LOG_PREFIX} development_id={development_id} "
                "action=skipped reason=development_unresolved",
                flush=True,
            )
            continue
        tier_one_exists = any(
            development_id in _relationship_ids(listing.get("development"))
            and classify_match(lead, listing) == 1
            for listing in internal_matches
        )
        if tier_one_exists:
            print(
                f"{LOG_PREFIX} development={development} "
                "action=skipped reason=internal_tier1_exists",
                flush=True,
            )
            continue
        print(
            f"{LOG_PREFIX} development={development} "
            "action=eligible reason=no_internal_tier1",
            flush=True,
        )
        lookups.append({
            "lead_id": str(lead_id),
            "development_id": development_id,
            "development": development,
            "bedrooms_min": bedrooms,
            "budget_rent": budget,
        })
    return lookups


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
                or price < lookup["budget_rent"] * 0.8
                or price > lookup["budget_rent"] * 1.2
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
    ``iproperty:<lead_id>:<development_id>`` before calling this function.
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


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _bubble():
    # Lazy import avoids coupling application startup to worker-only code.
    import app
    return app


def enqueue_external_lookup(lookup, phone, bubble_env="live"):
    """Persist a deduplicated lookup request for the background worker."""
    if not external_scraper_enabled() or not lookup:
        return None, False
    app = _bubble()
    base_url = app.get_bubble_base_url(bubble_env)
    job_key = f"iproperty:{lookup['lead_id']}:{lookup['development_id']}"
    constraints = [{"key": "jobKey", "constraint_type": "equals", "value": job_key}]
    existing = next(iter(app._bubble_records(base_url, JOB_TYPE, constraints)), None)
    if existing and existing.get("_id"):
        return str(existing["_id"]), False

    # Jobs created before development-specific keys must still suppress a
    # duplicate notification for the development stored in their payload.
    legacy_key = f"iproperty:{lookup['lead_id']}"
    legacy_constraints = [
        {"key": "jobKey", "constraint_type": "equals", "value": legacy_key}
    ]
    legacy = next(iter(app._bubble_records(
        base_url, JOB_TYPE, legacy_constraints
    )), None)
    if legacy and legacy.get("_id"):
        try:
            legacy_lookup = json.loads(legacy.get("payload") or "{}")
        except (TypeError, ValueError):
            legacy_lookup = {}
        legacy_development_id = str(
            legacy_lookup.get("development_id") or ""
        ).strip()
        if not legacy_development_id or legacy_development_id == str(
            lookup["development_id"]
        ):
            return str(legacy["_id"]), False
    payload = {
        "jobKey": job_key,
        "lead": lookup["lead_id"],
        "phone": app.normalize_phone(phone),
        "payload": json.dumps(lookup, separators=(",", ":")),
        "status": "Queued",
        "attempts": 0,
        "availableAt": _iso(_utcnow()),
        "notificationSent": False,
        "notificationState": "Pending",
    }
    try:
        return app._bubble_create(base_url, JOB_TYPE, payload), True
    except Exception:
        # A Bubble uniqueness rule may reject a concurrent duplicate create.
        existing = next(iter(app._bubble_records(base_url, JOB_TYPE, constraints)), None)
        if existing and existing.get("_id"):
            return str(existing["_id"]), False
        raise


def _parse_date(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(datetime.timezone.utc)


def external_scraper_max_age_days():
    """Return the configured positive freshness window, defaulting to 7 days."""
    try:
        value = int(os.getenv(
            "EXTERNAL_SCRAPER_MAX_AGE_DAYS",
            str(DEFAULT_EXTERNAL_SCRAPER_MAX_AGE_DAYS),
        ))
    except (TypeError, ValueError):
        return DEFAULT_EXTERNAL_SCRAPER_MAX_AGE_DAYS
    return value if value > 0 else DEFAULT_EXTERNAL_SCRAPER_MAX_AGE_DAYS


def get_fresh_external_listings(lookup, bubble_env="live", *, now=None):
    """Return up to three suitable, recently refreshed iProperty listings."""
    app = _bubble()
    base_url = app.get_bubble_base_url(bubble_env)
    constraints = [
        {"key": "scraped?", "constraint_type": "equals", "value": True},
        {"key": "source", "constraint_type": "equals", "value": EXPECTED_SOURCE},
        {"key": "development", "constraint_type": "equals",
         "value": lookup["development_id"]},
    ]
    current = now or _utcnow()
    cutoff = current - datetime.timedelta(days=external_scraper_max_age_days())
    suitable = []
    for listing in app._bubble_records(base_url, "listing", constraints):
        refreshed = _parse_date(listing.get("scraped_last_date"))
        price = _number(listing.get("priceRent"), positive=True)
        beds = _number(listing.get("beds"), nonnegative=True)
        transactions = listing.get("TransactionType") or []
        if isinstance(transactions, str):
            transactions = [transactions]
        if (
            not listing.get("_id")
            or not is_scraped_listing(listing)
            or str(listing.get("source") or "").strip().casefold() != EXPECTED_SOURCE
            or lookup["development_id"] not in _relationship_ids(
                listing.get("development")
            )
            or refreshed is None or refreshed < cutoff or refreshed > current
            or price is None or price > lookup["budget_rent"]
            or beds is None or beds < lookup["bedrooms_min"]
            or "Rent/Let" not in transactions
            or not _valid_listing_url(listing.get("sourceURL"))
        ):
            continue
        suitable.append((refreshed, listing))
    suitable.sort(key=lambda item: item[0], reverse=True)
    return [listing for _, listing in suitable[:3]]


def _cached_listing_item(listing):
    """Normalize a Bubble Listing for existing Match/message processing."""
    item = {
        "source_listing_id": str(listing.get("sourceListingID") or listing["_id"]),
        "source_url": listing["sourceURL"],
        "price_rent": listing["priceRent"],
        "beds": listing["beds"],
        "bubble_listing_id": str(listing["_id"]),
    }
    optional = {
        "agent": listing.get("sourceAgentName"),
        "sq_ft": listing.get("Sq Ft"),
        "description": listing.get("Notes"),
    }
    item.update({key: value for key, value in optional.items()
                 if value is not None and str(value).strip()})
    return item


def claim_external_job(bubble_env="live", *, lease_seconds=240):
    """Cooperatively lease one job using Bubble's non-atomic Data API.

    The read/patch/read token check reduces overlapping work but is not an
    atomic compare-and-swap. Production must run exactly one worker instance
    unless claiming is moved to an atomic backend workflow or database.
    """
    app = _bubble()
    base_url = app.get_bubble_base_url(bubble_env)
    now = _utcnow()
    jobs = app._bubble_records(base_url, JOB_TYPE)
    candidates = []
    for job in jobs:
        attempts = int(job.get("attempts") or 0)
        available = _parse_date(job.get("availableAt")) or now
        lease_until = _parse_date(job.get("leaseUntil"))
        status = job.get("status")
        lease_available = not lease_until or lease_until <= now
        if (((status in {"Queued", "Retry"} and available <= now)
             or (status == "Running" and lease_available))
                and attempts < MAX_JOB_ATTEMPTS and lease_available):
            candidates.append(job)
    candidates.sort(key=lambda item: str(item.get("availableAt") or ""))
    if not candidates:
        return None
    job = candidates[0]
    token = str(uuid.uuid4())
    patch = {
        "status": "Running", "workerId": token,
        "leaseUntil": _iso(now + datetime.timedelta(seconds=lease_seconds)),
        "attempts": int(job.get("attempts") or 0) + 1,
    }
    app._bubble_patch(f"{base_url}/obj/{JOB_TYPE}/{job['_id']}", patch)
    current = next(iter(app._bubble_records(
        base_url, JOB_TYPE,
        [{"key": "jobKey", "constraint_type": "equals", "value": job["jobKey"]}],
    )), None)
    if not current or current.get("workerId") != token:
        return None
    return current


def _external_listing_matches(lead, listing):
    budget = _number(lead.get("budgetRent"), positive=True)
    bedrooms = _number(lead.get("bedroomsMin"), nonnegative=True)
    price = _number(listing.get("priceRent"), positive=True)
    beds = _number(listing.get("beds"), nonnegative=True)
    lead_developments = set(_relationship_ids(lead.get("preferredDevelopments")))
    listing_developments = set(_relationship_ids(listing.get("development")))
    return bool(
        "Rent/Let" in (lead.get("TransactionType") or [])
        and budget is not None and bedrooms is not None
        and price is not None and beds is not None
        and budget * 0.8 <= price <= budget * 1.2
        and beds >= bedrooms
        and lead_developments & listing_developments
    )


def _reusable_external_listing_matches(lead, listing):
    """Recheck cached-listing eligibility against the current lead record."""
    budget = _number(lead.get("budgetRent"), positive=True)
    bedrooms = _number(lead.get("bedroomsMin"), nonnegative=True)
    price = _number(listing.get("priceRent"), positive=True)
    beds = _number(listing.get("beds"), nonnegative=True)
    lead_developments = set(_relationship_ids(lead.get("preferredDevelopments")))
    listing_developments = set(_relationship_ids(listing.get("development")))
    transactions = listing.get("TransactionType") or []
    if isinstance(transactions, str):
        transactions = [transactions]
    return bool(
        "Rent/Let" in (lead.get("TransactionType") or [])
        and "Rent/Let" in transactions
        and budget is not None and bedrooms is not None
        and price is not None and price <= budget
        and beds is not None and beds >= bedrooms
        and lead_developments & listing_developments
        and _valid_listing_url(listing.get("sourceURL"))
    )


def upsert_external_listing(item, lookup, bubble_env="live"):
    """Create or refresh scraper-owned fields without touching manual fields."""
    app = _bubble()
    base_url = app.get_bubble_base_url(bubble_env)
    constraints = [
        {"key": "source", "constraint_type": "equals", "value": EXPECTED_SOURCE},
        {"key": "sourceListingID", "constraint_type": "equals",
         "value": item["source_listing_id"]},
    ]
    existing = next(iter(app._bubble_records(base_url, "listing", constraints)), None)
    payload = {
        "scraped?": True, "scraped_last_date": _iso(_utcnow()),
        "source": EXPECTED_SOURCE, "sourceListingID": item["source_listing_id"],
        "sourceURL": item["source_url"], "priceRent": item["price_rent"],
        "beds": item["beds"], "development": lookup["development_id"],
        "TransactionType": ["Rent/Let"], "propertyType": "Condo",
    }
    optional = {
        "sourceAgentName": item.get("agent"),
        "sourceAgencyName": item.get("source_agency_name"),
        "Sq Ft": _number(item.get("sq_ft"), positive=True),
        "Notes": item.get("description"),
    }
    payload.update({key: value for key, value in optional.items()
                    if value is not None and str(value).strip()})
    if existing and existing.get("_id"):
        app._bubble_patch(f"{base_url}/obj/listing/{existing['_id']}", payload)
        return dict(existing, **payload, _id=str(existing["_id"])), False
    try:
        listing_id = app._bubble_create(base_url, "listing", payload)
    except Exception:
        concurrent = next(iter(app._bubble_records(
            base_url, "listing", constraints
        )), None)
        if not concurrent or not concurrent.get("_id"):
            raise
        app._bubble_patch(
            f"{base_url}/obj/listing/{concurrent['_id']}", payload
        )
        return dict(concurrent, **payload, _id=str(concurrent["_id"])), False
    return dict(payload, _id=str(listing_id)), True


def create_external_match(lead, listing, bubble_env="live", *, prequalified=False):
    """Create the explicit no-alert iProperty Match for one eligible pair."""
    eligible = (
        _reusable_external_listing_matches(lead, listing)
        if prequalified else _external_listing_matches(lead, listing)
    )
    if not eligible:
        return None, False
    app = _bubble()
    base_url = app.get_bubble_base_url(bubble_env)
    constraints = [
        {"key": "lead", "constraint_type": "equals", "value": lead["_id"]},
        {"key": "listing", "constraint_type": "equals", "value": listing["_id"]},
    ]
    existing = next(iter(app._bubble_records(base_url, "match", constraints)), None)
    if existing and existing.get("_id"):
        return str(existing["_id"]), False
    payload = {
        "lead": lead["_id"], "listing": listing["_id"],
        "match_source": EXPECTED_SOURCE, "match_tier": 1,
    }
    lead_owner = next(iter(_relationship_ids(lead.get("owner"))), None)
    if lead_owner:
        payload["lead_owner"] = lead_owner
    try:
        return app._bubble_create(base_url, "match", payload), True
    except Exception:
        concurrent = next(iter(app._bubble_records(
            base_url, "match", constraints
        )), None)
        if concurrent and concurrent.get("_id"):
            return str(concurrent["_id"]), False
        raise


def process_external_job(job, bubble_env="live", *, request_post=requests.post,
                         send_text=None, save_message=None):
    """Run one leased job, retaining partial failures for a safe retry."""
    app = _bubble()
    send_text = send_text or app.send_whatsapp_text
    if save_message is None:
        from whatsapp_importer import save_whatsapp_message as importer_save_message
        save_message = importer_save_message
    base_url = app.get_bubble_base_url(bubble_env)
    job_url = f"{base_url}/obj/{JOB_TYPE}/{job['_id']}"
    try:
        lookup = json.loads(job["payload"])
        lead_records, _ = app._bubble_records_by_ids(
            base_url, "lead", [lookup["lead_id"]]
        )
        lead = next(iter(lead_records), None)
        if not lead:
            raise ValueError("Lead no longer exists")
        fresh_listings = get_fresh_external_listings(lookup, bubble_env)
        reuse_fresh = bool(fresh_listings)
        if reuse_fresh:
            print(
                f"{LOG_PREFIX} action=reuse_fresh "
                f"development={lookup['development']} listings={len(fresh_listings)} "
                f"max_age_days={external_scraper_max_age_days()}",
                flush=True,
            )
            matches = [_cached_listing_item(listing) for listing in fresh_listings]
        else:
            print(
                f"{LOG_PREFIX} action=fetch_required "
                f"development={lookup['development']} reason=no_fresh_matches",
                flush=True,
            )
            matches = fetch_external_matches(lookup, request_post=request_post)
            if matches is None:
                raise RuntimeError("External scraper request failed")
            print(
                f"{LOG_PREFIX} action=fetch_completed "
                f"development={lookup['development']} listings={len(matches)}",
                flush=True,
            )
        persisted = []
        errors = []
        for item in matches:
            try:
                if reuse_fresh:
                    listing = next(
                        listing for listing in fresh_listings
                        if str(listing["_id"]) == item["bubble_listing_id"]
                    )
                else:
                    listing, _ = upsert_external_listing(item, lookup, bubble_env)
                match_id, _ = create_external_match(
                    lead, listing, bubble_env, prequalified=reuse_fresh
                )
                if match_id:
                    persisted.append(dict(item, bubble_listing_id=listing["_id"]))
            except Exception as error:
                import traceback

                print(
                    f"[EXTERNAL SCRAPER] listing_failed "
                    f"source_listing_id={item.get('source_listing_id')} "
                    f"error={type(error).__name__}: {error}",
                    flush=True,
                )
                traceback.print_exc()

                errors.append(
                    f"{item.get('source_listing_id')}: "
                    f"{type(error).__name__}: {error}"
                )
        if errors:
            raise RuntimeError("; ".join(errors))
        notification_state = str(job.get("notificationState") or "Pending")
        if not job.get("notificationSent") and notification_state == "Sending":
            # The previous worker may have sent through Meta before dying. Never
            # send automatically again because Meta offers no application-level
            # idempotency key for this operation.
            app._bubble_patch(job_url, {
                "status": "NotificationUncertain", "leaseUntil": None,
                "lastError": (
                    "Notification delivery is uncertain after worker interruption; "
                    "inspect Meta/Bubble message history before manual retry."
                ),
            })
            return {"status": "notification_uncertain", "listings": persisted}
        if not job.get("notificationSent"):
            message = format_external_matches(lookup["development"], persisted)
            app._bubble_patch(job_url, {
                "notificationState": "Sending",
                "notificationAttemptedAt": _iso(_utcnow()),
            })
            try:
                sent_ids = send_text(job["phone"], message) or []
                if not any(sent_ids):
                    raise RuntimeError("WhatsApp send returned no message IDs")
            except Exception:
                # No accepted Meta message ID is known, so a retry is safe.
                app._bubble_patch(job_url, {"notificationState": "Pending"})
                raise
            try:
                for meta_id in sent_ids:
                    if meta_id:
                        save_message(
                            job["phone"], meta_id, message, "Outbound", "text", bubble_env,
                            lead_id=lookup["lead_id"],
                        )
                app._bubble_patch(job_url, {
                    "notificationSent": True, "notificationState": "Sent",
                })
            except Exception:
                # Meta accepted at least one delivery. Preserve Sending so the
                # next run stops for reconciliation instead of sending twice.
                raise
        app._bubble_patch(job_url, {
            "status": "Completed", "completedAt": _iso(_utcnow()),
            "leaseUntil": None, "lastError": "",
        })
        return {"status": "completed", "listings": persisted}
    except Exception as error:
        attempts = int(job.get("attempts") or 1)
        terminal = attempts >= MAX_JOB_ATTEMPTS
        app._bubble_patch(job_url, {
            "status": "Failed" if terminal else "Retry",
            "availableAt": _iso(_utcnow() + datetime.timedelta(minutes=2 ** attempts)),
            "leaseUntil": None, "lastError": f"{type(error).__name__}: {error}"[:500],
        })
        print(f"{LOG_PREFIX} action=job_failed job_id={job.get('_id')} "
              f"error={type(error).__name__}", flush=True)
        return {"status": "failed" if terminal else "retry"}
