"""Import forwarded WhatsApp property requirements into Bubble.

This module deliberately owns all ingestion-specific parsing, validation,
resolution and payload construction.  It reuses app.py's configured OpenAI
client and Bubble Data API helpers, but is not wired into the webhook yet.
"""

from __future__ import annotations

import datetime
import json
import re
from typing import Any, Iterable

import app as rentee_app
import development_resolver


LOG_PREFIX = "[WHATSAPP IMPORT]"
TRANSACTION_TYPES = ("Rent/Let", "Buy/Sell")
PROPERTY_TYPES = ("Condo", "Landed", "Apartment", "House")
IMPORT_ROUTING_THRESHOLD = 0.90

STRONG_IMPORT_MARKERS = (
    ("lead_import", "WTR", r"(?<![A-Z0-9])WTR(?![A-Z0-9])"),
    ("lead_import", "WTB", r"(?<![A-Z0-9])WTB(?![A-Z0-9])"),
    ("lead_import", "Want To Rent", r"\bWANT\s+TO\s+RENT\b"),
    ("lead_import", "Want To Buy", r"\bWANT\s+TO\s+BUY\b"),
    ("listing_import", "WTS", r"(?<![A-Z0-9])WTS(?![A-Z0-9])"),
    ("listing_import", "WTL", r"(?<![A-Z0-9])WTL(?![A-Z0-9])"),
    ("listing_import", "Want To Sell", r"\bWANT\s+TO\s+SELL\b"),
    ("listing_import", "Want To Let", r"\bWANT\s+TO\s+LET\b"),
)


PARSER_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["lead", "listing", "unknown"]},
        "geo_names": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "Actual residential areas/neighbourhoods explicitly preferred by the sender; "
                "never Development, condo, or project names, nor schools, workplaces, malls, "
                "landmarks, offices, or stations. Use an empty list when no actual area is stated."
            ),
        },
        "geo_name": {
            "type": ["string", "null"],
            "description": (
                "Explicit actual residential area/neighbourhood of the listing; never a "
                "Development, condo, project, school, workplace, mall, landmark, office, or "
                "station. Use null when no actual area is stated."
            ),
        },
        "preferred_development_names": {
            "type": "array", "items": {"type": "string"},
            "description": "Development, condo, and project names; never place these in geo_names.",
        },
        "development_name": {
            "type": ["string", "null"],
            "description": "Listing Development, condo, or project name; never place it in geo_name.",
        },
        "transaction_types": {
            "type": "array",
            "items": {"type": "string", "enum": list(TRANSACTION_TYPES)},
        },
        "property_types": {
            "type": "array",
            "items": {"type": "string", "enum": list(PROPERTY_TYPES)},
        },
        "property_type": {"type": ["string", "null"]},
        "budget": {"type": ["number", "null"], "minimum": 0},
        "asking_price": {"type": ["number", "null"], "minimum": 0},
        "bedrooms_min": {"type": ["integer", "null"], "minimum": 0},
        "lead_name": {
            "type": ["string", "null"],
            "description": "Explicit Lead/client name; never the forwarding or proposing agent.",
        },
        "adults": {"type": ["integer", "null"], "minimum": 0},
        "children": {"type": ["integer", "null"], "minimum": 0},
        "nationality": {"type": ["string", "null"]},
        "occupation": {"type": ["string", "null"]},
        "move_in_date": {
            "type": ["string", "null"],
            "description": "Exact date as YYYY-MM-DD, otherwise null.",
        },
        "pets": {"type": ["string", "null"]},
        "furnishing_preference": {
            "type": ["string", "null"],
            "enum": ["Fully Furnished", "Partially Furnished", "Unfurnished", None],
        },
        "bathrooms_min": {"type": ["integer", "null"], "minimum": 0},
        "start_date": {
            "type": ["string", "null"],
            "description": "Exact date as YYYY-MM-DD, otherwise null.",
        },
        "helpers": {"type": ["integer", "null"], "minimum": 0},
        "notes": {
            "type": ["string", "null"],
            "description": (
                "Concise useful requirements not represented by another structured field; "
                "do not duplicate structured facts."
            ),
        },
        "beds": {"type": ["integer", "null"], "minimum": 0},
        "baths": {"type": ["number", "null"], "minimum": 0},
        "sqft": {"type": ["number", "null"], "minimum": 0},
        "land_sqft": {"type": ["number", "null"], "minimum": 0},
        "furnished": {"type": ["string", "null"], "enum": ["Yes", "No", None]},
        "furnishing": {
            "type": ["string", "null"],
            "enum": ["Fully Furnished", "Partially Furnished", "Unfurnished", None],
        },
        "available": {"type": ["boolean", "null"]},
        "availability_date": {
            "type": ["string", "null"],
            "description": "Exact date as YYYY-MM-DD, otherwise null.",
        },
        "balcony": {"type": ["string", "null"], "enum": ["Yes", "No", None]},
        "study": {"type": ["number", "null"], "minimum": 0},
        "family_room": {"type": ["number", "null"], "minimum": 0},
        "maid_room": {"type": ["number", "null"], "minimum": 0},
        "outdoor_area": {"type": ["string", "null"], "enum": ["Yes", "No", None]},
        "unit_number": {"type": ["string", "null"]},
        "owner_name": {"type": ["string", "null"]},
        "owner_contact": {"type": ["string", "null"]},
        "source_agency_name": {"type": ["string", "null"]},
        "proposing_agent": {
            "type": "object",
            "properties": {
                "name": {"type": ["string", "null"]},
                "phone": {"type": ["string", "null"]},
                "ren": {"type": ["string", "null"]},
            },
            "required": ["name", "phone", "ren"],
            "additionalProperties": False,
        },
    },
    "required": [
        "type", "geo_names", "geo_name", "preferred_development_names",
        "development_name", "transaction_types", "property_types",
        "property_type", "budget", "asking_price", "bedrooms_min", "beds",
        "lead_name",
        "adults", "children", "nationality", "occupation", "move_in_date",
        "pets", "furnishing_preference", "bathrooms_min", "start_date",
        "helpers", "notes",
        "baths", "sqft", "land_sqft", "furnished", "furnishing", "available",
        "availability_date", "balcony", "study", "family_room", "maid_room",
        "outdoor_area", "unit_number", "owner_name", "owner_contact",
        "source_agency_name",
        "proposing_agent",
    ],
    "additionalProperties": False,
}


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())


def _record_name(record: dict) -> str:
    return _compact(next(
        (record.get(key) for key in ("name", "Name", "Condo name") if record.get(key)),
        "",
    ))


def _relationship_ids(value: Any) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    result = []
    for item in values:
        candidate = item.get("_id") if isinstance(item, dict) else item
        if candidate and str(candidate) not in result:
            result.append(str(candidate))
    return result


def _unique_strings(values: Iterable[Any]) -> list[str]:
    result, seen = [], set()
    for value in values or []:
        clean = _compact(value)
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result


def _validate_parsed(value: Any) -> dict:
    if not isinstance(value, dict) or value.get("type") not in {"lead", "listing", "unknown"}:
        return {"type": "unknown"}
    if value["type"] == "unknown":
        return {"type": "unknown"}

    transactions = _unique_strings(value.get("transaction_types") or [])
    transactions = [item for item in transactions if item in TRANSACTION_TYPES]
    property_types = _unique_strings(value.get("property_types") or [])
    property_types = [item for item in property_types if item in PROPERTY_TYPES]
    property_type = value.get("property_type")
    if property_type not in PROPERTY_TYPES:
        property_type = None
    raw_agent = value.get("proposing_agent")
    raw_agent = raw_agent if isinstance(raw_agent, dict) else {}
    proposing_agent = {
        key: (_compact(raw_agent.get(key)) or None)
        for key in ("name", "phone", "ren")
    }

    if value["type"] == "lead":
        result = {
            "type": "lead",
            "geo_names": _unique_strings(value.get("geo_names") or []),
            "preferred_development_names": _unique_strings(
                value.get("preferred_development_names") or []
            ),
            "transaction_types": transactions,
            "property_types": property_types,
            "proposing_agent": proposing_agent,
        }
        for key in ("budget", "bedrooms_min"):
            if isinstance(value.get(key), (int, float)) and value[key] >= 0:
                result[key] = int(value[key]) if float(value[key]).is_integer() else value[key]
        for key in ("adults", "children", "bathrooms_min", "helpers"):
            if (not isinstance(value.get(key), bool)
                    and isinstance(value.get(key), int) and value[key] >= 0):
                result[key] = value[key]
        for key in ("nationality", "occupation", "pets", "notes"):
            if _compact(value.get(key)):
                result[key] = _compact(value[key])
        if _compact(value.get("lead_name")):
            result["lead_name"] = _compact(value["lead_name"])
        furnishing = value.get("furnishing_preference")
        if furnishing in {"Fully Furnished", "Partially Furnished", "Unfurnished"}:
            result["furnishing_preference"] = furnishing
        for key in ("move_in_date", "start_date"):
            try:
                result[key] = datetime.date.fromisoformat(str(value.get(key))).isoformat()
            except (TypeError, ValueError):
                pass
        return result

    result = {
        "type": "listing",
        "transaction_types": transactions,
        "proposing_agent": proposing_agent,
    }
    if _compact(value.get("geo_name")):
        result["geo_name"] = _compact(value["geo_name"])
    if _compact(value.get("development_name")):
        result["development_name"] = _compact(value["development_name"])
    if property_type:
        result["property_type"] = property_type
    for key in ("asking_price", "beds"):
        if isinstance(value.get(key), (int, float)) and value[key] >= 0:
            result[key] = int(value[key]) if float(value[key]).is_integer() else value[key]
    for key in ("baths", "sqft", "land_sqft", "study", "family_room", "maid_room"):
        if (not isinstance(value.get(key), bool)
                and isinstance(value.get(key), (int, float)) and value[key] >= 0):
            result[key] = int(value[key]) if float(value[key]).is_integer() else value[key]
    for key in ("furnished", "balcony", "outdoor_area"):
        if value.get(key) in {"Yes", "No"}:
            result[key] = value[key]
    if value.get("furnishing") in {
        "Fully Furnished", "Partially Furnished", "Unfurnished",
    }:
        result["furnishing"] = value["furnishing"]
    if isinstance(value.get("available"), bool):
        result["available"] = value["available"]
    try:
        result["availability_date"] = datetime.date.fromisoformat(
            str(value.get("availability_date"))
        ).isoformat()
    except (TypeError, ValueError):
        pass
    for key in ("unit_number", "owner_name", "owner_contact", "source_agency_name", "notes"):
        if _compact(value.get(key)):
            result[key] = _compact(value[key])
    return result


def _parser_schema(import_type=None):
    schema = json.loads(json.dumps(PARSER_SCHEMA))
    if import_type:
        schema["properties"]["type"]["enum"] = [import_type]
    return schema


def parse_forwarded_message(raw_text: str, import_type: str | None = None) -> dict:
    """Classify and extract only strongly evidenced MVP fields."""
    text = str(raw_text or "").strip()
    if not text:
        return {"type": "unknown"}
    if import_type not in {None, "lead", "listing"}:
        raise ValueError("import_type must be 'lead', 'listing', or None.")
    task = (
        f"Extract this existing {import_type} record for ingestion. The type is already "
        f"known to be {import_type}; do not classify it. "
        if import_type else
        "Classify it as lead (a seeker requirement), listing (a property offered), or unknown. "
    )
    try:
        response = rentee_app.client.responses.create(
            model="gpt-5-mini",
            input=(
            "Parse one forwarded Malaysian property WhatsApp message. " + task +
            "Lead evidence: "
            "WTR, WTB, looking for, seeking, wanted, requirement, tenant requirement, buyer "
            "requirement. Listing evidence: WTS, WTL, for sale, for rent, available, owner "
            "asking, asking RM, unit available. Map renter/for rent/WTR/WTL to Rent/Let and "
            "buyer/for sale/WTB/WTS to Buy/Sell; classification determines which side. "
            "Normalize RM8k=8000, RM 8,500=8500, 1.8m=1800000 and RM3.5 million=3500000. "
            "Normalize bedroom forms to an integer. Property types may only be Condo, Landed, "
            "Apartment, House. Map only obvious variants and never infer Condo merely from a "
            "development name. Put actual areas/neighbourhoods only in geo_names/geo_name, and "
            "put Development, condo, or project names only in preferred_development_names/"
            "development_name. A named Development is not a Geo. If no actual area is stated, "
            "geo_names must be empty and geo_name must be null; Geo can be derived later. Geo fields "
            "are only residential search areas or listing locations explicitly stated as such. "
            "Never put schools, workplaces, malls, landmarks, offices, or stations in Geo fields; "
            "they remain context only. Extract proposing_agent only from a credible agent/contact "
            "signature, such as a final name + registration/REN + agency + phone block, or an "
            "explicit agent/negotiator/contact/PIC association. Registration forms include REN "
            "12345, REN12345, E2265, and PEA 1234. If multiple phone numbers make ownership "
            "ambiguous, leave the agent phone null. Do not mistake tenant or owner contacts for "
            "the proposing agent. Never invent agent fields. Return "
            "null/empty values when evidence is weak; do not invent facts. For unknown, leave "
            "all other fields empty/null. For leads, extract adults, children, nationality, "
            "occupation, pets, furnishing preference, minimum bathrooms, helpers, and exact "
            "move-in/start dates only when explicitly and clearly stated. Absence never means "
            "zero. Dates must be YYYY-MM-DD and must be null when approximate or not responsibly "
            "resolvable. Extract lead_name only when an actual Lead/client name is explicitly "
            "provided; never use the proposing agent or the Rentee user who forwarded the message. "
            "Put concise useful requirements that have no structured Lead field "
            "(such as desired sqft, tenancy length, employer/work details, school, family context, "
            "or unusual requirements) in notes. Never repeat in notes anything captured in a "
            "structured field. For listings, extract baths, built-up sqft, land sqft, furnished "
            "Yes/No, canonical furnishing status, availability, exact availability date, balcony, "
            "study/family/maid room counts, outdoor area, unit number, owner name/contact, and "
            "source agency only when explicitly stated. Put useful listing details not captured "
            "by another structured field in notes, without duplicating structured facts. Never "
            "extract or infer exposure.\n\nMESSAGE:\n" + text
            ),
            reasoning={"effort": "low"},
            max_output_tokens=2000,
            timeout=20,
            text={"format": {
                "type": "json_schema", "name": "whatsapp_property_import",
                "strict": True, "schema": _parser_schema(import_type),
            }},
        )
    except Exception as error:
        print(f"[WHATSAPP IMPORT PARSER ERROR] type={type(error).__name__} "
              f"error={error}", flush=True)
        raise
    response_status = str(getattr(response, "status", "completed") or "completed")
    if response_status.lower() != "completed":
        print(f"[WHATSAPP IMPORT PARSER ERROR] status={response_status} "
              f"incomplete_details={getattr(response, 'incomplete_details', None)} "
              f"usage={getattr(response, 'usage', None)}", flush=True)
        print("[WHATSAPP IMPORT PARSER ERROR] type=ValueError "
              "error=WhatsApp parser did not complete.", flush=True)
        raise ValueError("WhatsApp parser did not complete.")
    try:
        parsed = json.loads(str(response.output_text or ""))
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("WhatsApp parser returned invalid structured output.") from error
    result = _validate_parsed(parsed)
    print(f"{LOG_PREFIX} parsed type={result['type']}", flush=True)
    return result


def normalize_phone_number(raw_phone: str | None) -> str | None:
    """Canonicalize a Malaysian mobile using Rentee's WhatsApp digit normalization."""
    digits = rentee_app.normalize_phone(raw_phone)
    if digits.startswith("0"):
        digits = "60" + digits[1:]
    if not re.fullmatch(r"601\d{8,9}", digits):
        return None
    return digits


def build_internal_user_email(normalized_phone: str) -> str:
    """Use the repository's deterministic WhatsApp-only User email convention."""
    return f"whatsapp-{normalized_phone}@users.rentee.internal"


def _agent_value(value):
    return _compact(value) or None


def _enrich_existing_agent(user, name, ren, normalized_phone, bubble_env):
    updates = {}
    existing_name = _agent_value(user.get("name"))
    existing_ren = _agent_value(user.get("REN"))
    if name and not existing_name:
        updates["name"] = name
    elif name and existing_name and name.casefold() != existing_name.casefold():
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} conflict=name "
              f"existing={existing_name!r} incoming={name!r}", flush=True)
    if ren and not existing_ren:
        updates["REN"] = ren
    elif ren and existing_ren and ren.casefold() != existing_ren.casefold():
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} conflict=REN "
              f"existing={existing_ren!r} incoming={ren!r}", flush=True)
    if updates:
        try:
            rentee_app._bubble_patch(
                f"{rentee_app.get_bubble_base_url(bubble_env)}/obj/user/{user['_id']}",
                updates,
            )
        except Exception as error:
            print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} "
                  f"action=update_failed error={type(error).__name__}", flush=True)
        else:
            user = {**user, **updates}
            for field in updates:
                print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} "
                      f"action=updated field={field}", flush=True)
    return user


def resolve_or_create_proposing_agent(name: str | None, phone: str | None,
                                      ren: str | None,
                                      bubble_env: str = "live") -> dict:
    """Resolve one proposing agent by canonical phone, creating conservatively."""
    clean_name, clean_ren = _agent_value(name), _agent_value(ren)
    normalized = normalize_phone_number(phone)
    print(f"[WHATSAPP IMPORT AGENT] raw_phone={_compact(phone)!r} "
          f"normalized={normalized!r}", flush=True)
    if not normalized:
        return {"status": "no_phone", "user_id": None,
                "normalized_phone": None, "name": clean_name, "ren": clean_ren}
    try:
        matches = rentee_app.find_bubble_users_by_phone(normalized, bubble_env)
    except Exception as error:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=lookup_failed "
              f"error={type(error).__name__}", flush=True)
        return {"status": "error", "user_id": None,
                "normalized_phone": normalized, "name": clean_name, "ren": clean_ren}
    if len(matches) > 1:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} "
              f"action=duplicate_existing count={len(matches)}", flush=True)
        return {"status": "duplicate_existing", "user_id": None,
                "normalized_phone": normalized, "name": clean_name, "ren": clean_ren}
    if len(matches) == 1:
        user = _enrich_existing_agent(
            dict(matches[0]), clean_name, clean_ren, normalized, bubble_env
        )
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=existing "
              f"user_id={user['_id']}", flush=True)
        return {"status": "existing", "user_id": str(user["_id"]),
                "normalized_phone": normalized,
                "name": _agent_value(user.get("name")) or clean_name,
                "ren": _agent_value(user.get("REN")) or clean_ren,
                "user": user}
    payload = {
        "phone": normalized,
        "email": build_internal_user_email(normalized),
    }
    if clean_name:
        payload["name"] = clean_name
    if clean_ren:
        payload["REN"] = clean_ren
    try:
        user_id = rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), "user", payload
        )
    except Exception as create_error:
        # Mirror the existing WhatsApp identity race recovery: query once after
        # a failed create and reuse a concurrently-created exact phone match.
        try:
            raced = rentee_app.find_bubble_users_by_phone(normalized, bubble_env)
        except Exception:
            raced = []
        if len(raced) == 1:
            user = _enrich_existing_agent(
                dict(raced[0]), clean_name, clean_ren, normalized, bubble_env
            )
            print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=existing "
                  f"user_id={user['_id']}", flush=True)
            return {"status": "existing", "user_id": str(user["_id"]),
                    "normalized_phone": normalized,
                    "name": _agent_value(user.get("name")) or clean_name,
                    "ren": _agent_value(user.get("REN")) or clean_ren,
                    "user": user}
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=create_failed "
              f"error={type(create_error).__name__}", flush=True)
        return {"status": "error", "user_id": None,
                "normalized_phone": normalized, "name": clean_name, "ren": clean_ren}
    user = {"_id": user_id, **payload}
    print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=created "
          f"user_id={user_id}", flush=True)
    return {"status": "created", "user_id": str(user_id),
            "normalized_phone": normalized, "name": clean_name, "ren": clean_ren,
            "user": user}


def _detector_result(intent, confidence, signals, reason, marker=None):
    result = {
        "intent": intent,
        "confidence": round(float(confidence), 2),
        "signals": list(signals),
    }
    suffix = f" marker={marker!r}" if marker else ""
    print(
        f"[WHATSAPP IMPORT DETECTOR] intent={intent} "
        f"confidence={result['confidence']} reason={reason}{suffix}",
        flush=True,
    )
    return result


def detect_import_intent(raw_text: str) -> dict:
    """Conservatively distinguish record ingestion from ordinary property chat."""
    text = str(raw_text or "").strip()
    upper = text.upper()
    if not text:
        return _detector_result("normal_chat", 1.0, [], "empty")

    for intent, marker, pattern in STRONG_IMPORT_MARKERS:
        if re.search(pattern, upper, flags=re.IGNORECASE):
            return _detector_result(
                intent, 1.0, [f"strong_marker:{marker}"], "strong_marker", marker
            )

    lower = text.casefold()
    conversational = bool(re.search(
        r"(?:^|[.!?]\s*)(?:show me|what do you have|can you|could you|please find|"
        r"find me|would you recommend|do you recommend|is\s+rm[\d,.]+\s+enough|"
        r"tell me|help me|suggest|recommend)\b|\?$", lower
    ))
    signals = []
    checks = (
        ("money", r"\b(?:rm|myr)\s*[\d,.]+|\b\d+(?:\.\d+)?\s*(?:k|m|million)\b"),
        ("bedrooms", r"\b\d+\s*(?:bed(?:room)?s?|br)\b"),
        ("property_type", r"\b(?:condo(?:minium)?|apartment|landed|house)\b"),
        ("size", r"\b\d[\d,]*(?:\s*[-–]\s*\d[\d,]*)?\s*(?:sq\s*ft|sqft|sf)\b"),
        ("tenancy", r"\b(?:\d+\s*(?:year|month)s?\s+tenancy|tenancy\s+duration)\b"),
        ("phone", r"(?<!\d)(?:\+?6?0?1\d[-\s]?\d{3,4}[-\s]?\d{4})(?!\d)"),
        ("agent_block", r"\([A-Z]\d{3,6}\)|\b(?:properties|realty|real estate)\b"),
        ("bullets", r"(?m)^\s*[-*•]\s+"),
        ("multiline", r"\n\s*\S+.*\n"),
        ("available", r"\b(?:available|unit available|owner asking|asking\s+rm)\b"),
        ("lead_wording", r"\b(?:looking for|seeking|tenant requirement|buyer requirement|requirement)\b"),
        ("listing_wording", r"\b(?:for sale|for rent|unit available|owner asking|asking\s+rm)\b"),
        ("tenant_profile", r"\b(?:tenant profile|family tenant|nationality|husband|wife|children|daughter|son)\b"),
    )
    for name, pattern in checks:
        if re.search(pattern, text, flags=re.IGNORECASE):
            signals.append(name)

    if conversational:
        signals.append("conversational_request")
        return _detector_result("normal_chat", 0.96, signals, "conversational_request")

    facts = len(set(signals) & {
        "money", "bedrooms", "property_type", "size", "tenancy", "phone",
        "agent_block", "available", "tenant_profile",
    })
    structured = "bullets" in signals or "multiline" in signals
    if "listing_wording" in signals and facts >= 3 and structured:
        return _detector_result("listing_import", 0.93, signals, "structured_content")
    if "lead_wording" in signals and facts >= 3 and structured:
        return _detector_result("lead_import", 0.93, signals, "structured_content")

    # A classifier is worthwhile only for a record-like block. Sparse or
    # conversational messages deliberately stay in normal chat.
    if facts < 3 or not structured:
        return _detector_result("normal_chat", 0.80, signals, "ambiguous")

    response = rentee_app.client.responses.create(
        model="gpt-5-mini",
        input=(
            "Classify whether this structured property block is an existing seeker "
            "requirement to ingest (lead_import), an existing offered property to ingest "
            "(listing_import), or an ordinary request/question (normal_chat). Prefer "
            "normal_chat when uncertain. Do not extract fields.\n\nMESSAGE:\n" + text
        ),
        reasoning={"effort": "low"},
        max_output_tokens=100,
        timeout=10,
        text={"format": {
            "type": "json_schema", "name": "whatsapp_import_intent",
            "strict": True, "schema": {
                "type": "object",
                "properties": {
                    "intent": {"type": "string", "enum": [
                        "lead_import", "listing_import", "normal_chat",
                    ]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["intent", "confidence"],
                "additionalProperties": False,
            },
        }},
    )
    try:
        classified = json.loads(str(response.output_text or ""))
        intent = classified.get("intent")
        confidence = float(classified.get("confidence", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        intent, confidence = "normal_chat", 0.0
    if intent not in {"lead_import", "listing_import"} or confidence < IMPORT_ROUTING_THRESHOLD:
        return _detector_result("normal_chat", max(0.72, confidence), signals,
                                "model_ambiguous")
    return _detector_result(intent, confidence, signals, "model_classifier")


def resolve_geo_name(name, geo_records):
    return development_resolver.resolve_geo_name(name, geo_records)


def resolve_geo_names(names, geo_records):
    return [resolve_geo_name(name, geo_records) for name in names or []]


def _verification_context(parsed, raw_text):
    return {
        "geo_names": _unique_strings(parsed.get("geo_names") or []),
        "geo_name": _compact(parsed.get("geo_name")) or None,
        "property_type": parsed.get("property_type"),
        "property_types": [
            item for item in parsed.get("property_types", []) if item in PROPERTY_TYPES
        ],
        "transaction_types": [
            item for item in parsed.get("transaction_types", []) if item in TRANSACTION_TYPES
        ],
        "other_development_names": _unique_strings(
            (parsed.get("preferred_development_names") or [])
            + ([parsed["development_name"]] if parsed.get("development_name") else [])
        ),
        "raw_text": str(raw_text or "")[:4000],
    }


def _resolve_or_verify_developments(names, development_records, geo_records,
                                    context, bubble_env):
    resolutions, created = [], []
    for raw_name in _unique_strings(names):
        try:
            outcome = development_resolver.resolve_or_create_development(
                raw_name, context, bubble_env,
                development_records=development_records,
                geo_records=geo_records,
            )
        except Exception as error:
            print(f"[DEVELOPMENT VERIFY] raw={raw_name!r} status=error "
                  f"reason='unexpected_resolver_error' "
                  f"error={type(error).__name__}", flush=True)
            outcome = {"status": "error", "action": "none",
                       "raw_name": raw_name, "reason": "unexpected_resolver_error"}
        if outcome.get("status") != "resolved":
            resolutions.append({
                "matched": False, "raw_name": raw_name,
                "reason": outcome.get("reason"),
                "resolver_status": outcome.get("status"),
            })
            continue
        resolution = {
            "matched": True,
            "id": str(outcome["development_id"]),
            "name": outcome["canonical_name"],
            "method": outcome.get("method") or (
                "web_verified_created" if outcome.get("action") == "created"
                else "existing"
            ),
            "record": outcome["record"],
        }
        if outcome.get("action") == "created":
            resolution["created"] = True
            created.append(resolution)
            development_records.append(outcome["record"])
        resolutions.append(resolution)
    return resolutions, created
def _matched_ids(resolutions: Iterable[dict]) -> list[str]:
    return list(dict.fromkeys(item["id"] for item in resolutions or [] if item.get("matched")))


def _apply_proposing_agent_payload(payload, proposing_agent, name_field, number_field):
    if not proposing_agent:
        return
    normalized = proposing_agent.get("normalized_phone")
    name = _agent_value(proposing_agent.get("name"))
    if normalized:
        payload[number_field] = normalized
        if name:
            payload[name_field] = name


def build_lead_payload(parsed, resolved_geos, resolved_developments,
                       proposing_agent=None) -> dict:
    payload = {}
    geo_ids = _matched_ids(resolved_geos)
    development_ids = _matched_ids(resolved_developments)
    transactions = [v for v in parsed.get("transaction_types", []) if v in TRANSACTION_TYPES]
    property_types = [v for v in parsed.get("property_types", []) if v in PROPERTY_TYPES]
    if geo_ids:
        payload["Geo"] = geo_ids
    if development_ids:
        payload["preferredDevelopments"] = development_ids
    if transactions:
        payload["TransactionType"] = list(dict.fromkeys(transactions))
    if property_types:
        payload["propertyTypes"] = list(dict.fromkeys(property_types))
    if isinstance(parsed.get("bedrooms_min"), (int, float)):
        payload["bedroomsMin"] = parsed["bedrooms_min"]
    if isinstance(parsed.get("budget"), (int, float)):
        if "Rent/Let" in transactions:
            payload["budgetRent"] = parsed["budget"]
        if "Buy/Sell" in transactions:
            payload["budgetBuy"] = parsed["budget"]
    field_mapping = {
        "adults": "adults",
        "children": "children",
        "nationality": "nationality",
        "occupation": "occupation",
        "pets": "pets",
        "furnishing_preference": "furnishingPreference",
        "bathrooms_min": "bathroomsMin",
        "helpers": "helpers",
        "notes": "notes",
    }
    for parsed_field, bubble_field in field_mapping.items():
        value = parsed.get(parsed_field)
        if value is not None:
            payload[bubble_field] = value
    for parsed_field, bubble_field in (
        ("move_in_date", "moveInDate"), ("start_date", "startDate")
    ):
        if parsed.get(parsed_field):
            payload[bubble_field] = f"{parsed[parsed_field]}T00:00:00.000Z"
    _apply_proposing_agent_payload(
        payload, proposing_agent,
        name_field="ProposedAgentNameLead",
        number_field="ProposedAgentNumberLead",
    )
    if parsed.get("lead_name"):
        payload["name"] = parsed["lead_name"]
    elif payload.get("ProposedAgentNameLead"):
        transaction_label = next((
            label for transaction, label in (("Rent/Let", "WTR"), ("Buy/Sell", "WTB"))
            if transaction in transactions
        ), None)
        location = next((
            item.get("name") for item in resolved_developments or []
            if item.get("matched") and _compact(item.get("name"))
        ), None)
        if not location:
            location = next((
                item.get("name") for item in resolved_geos or []
                if item.get("matched") and _compact(item.get("name"))
            ), None)
        if transaction_label and location:
            payload["name"] = (
                f"{payload['ProposedAgentNameLead']} (Agent) "
                f"{transaction_label} {_compact(location)}"
            )
    return payload


def build_listing_payload(parsed, resolved_geo, resolved_development,
                          proposing_agent=None) -> dict:
    payload = {"exposure": "public"}
    if resolved_geo and resolved_geo.get("matched"):
        payload["Geo"] = resolved_geo["id"]
    if resolved_development and resolved_development.get("matched"):
        payload["development"] = resolved_development["id"]
    transactions = [v for v in parsed.get("transaction_types", []) if v in TRANSACTION_TYPES]
    if transactions:
        payload["TransactionType"] = list(dict.fromkeys(transactions))
    if parsed.get("property_type") in PROPERTY_TYPES:
        payload["propertyType"] = parsed["property_type"]
    if isinstance(parsed.get("beds"), (int, float)):
        payload["beds"] = parsed["beds"]
    if isinstance(parsed.get("asking_price"), (int, float)):
        if "Rent/Let" in transactions:
            payload["priceRent"] = parsed["asking_price"]
        if "Buy/Sell" in transactions:
            payload["priceSale"] = parsed["asking_price"]
    field_mapping = {
        "baths": "baths",
        "sqft": "Sq Ft",
        "land_sqft": "Landed_sqft",
        "furnished": "furnished",
        "furnishing": "Furnishing",
        "available": "availability",
        "balcony": "balcony",
        "study": "study",
        "family_room": "family room",
        "maid_room": "maid room",
        "outdoor_area": "outdoor area",
        "unit_number": "unitNumber",
        "owner_name": "ownerName",
        "owner_contact": "ownerContact",
        "source_agency_name": "sourceAgencyName",
        "notes": "Notes",
    }
    for parsed_field, bubble_field in field_mapping.items():
        value = parsed.get(parsed_field)
        if value is not None:
            payload[bubble_field] = value
    if parsed.get("availability_date"):
        payload["availability_date"] = parsed["availability_date"]
    _apply_proposing_agent_payload(
        payload, proposing_agent,
        name_field="ProposingAgentName",
        number_field="ProposingAgentNumber",
    )
    return payload


def _geo_by_id(geo_records: Iterable[dict]) -> dict[str, dict]:
    return {str(item["_id"]): item for item in geo_records or [] if item.get("_id")}


def _derived_geos(development_resolutions, geo_records) -> list[dict]:
    by_id = _geo_by_id(geo_records)
    results = []
    seen = set()
    for development in development_resolutions or []:
        if not development.get("matched"):
            continue
        for geo_id in _relationship_ids(development.get("record", {}).get("Geo")):
            record = by_id.get(geo_id)
            if record and geo_id not in seen:
                results.append({"matched": True, "id": geo_id, "name": _record_name(record),
                                "method": "development_geo", "record": record})
                seen.add(geo_id)
                print(f"{LOG_PREFIX} geo derived development={development['name']!r} "
                      f"geo={_record_name(record)!r}", flush=True)
    return results


def _public_resolution(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {key: item for key, item in value.items() if key != "record"}


def _money(value: Any) -> str:
    return f"RM{value:,.0f}" if isinstance(value, (int, float)) else "price not specified"


def _confirmation(parsed, geos, developments) -> str:
    beds = parsed.get("bedrooms_min") if parsed["type"] == "lead" else parsed.get("beds")
    bed_text = f", {beds:g} bed" if isinstance(beds, (int, float)) else ""
    if parsed["type"] == "lead":
        place = ", ".join(item["name"] for item in geos if item.get("matched"))
        property_text = ", ".join(parsed.get("property_types") or [])
        bits = ", ".join(item for item in (place, property_text) if item)
        suffix = f", up to {_money(parsed.get('budget'))}" if parsed.get("budget") is not None else ""
        parsed_agent = parsed.get("proposing_agent") or {}
        agent_name = _compact(parsed_agent.get("name"))
        agent_phone = normalize_phone_number(parsed_agent.get("phone"))
        prefix = (f"Added lead to {agent_name}: {agent_phone}:"
                  if agent_name and agent_phone else "Added lead:")
        confirmation = f"{prefix} {bits}{bed_text}{suffix}."
        development_names = list(dict.fromkeys(
            item["name"] for item in developments if item.get("matched") and item.get("name")
        ))
        if development_names:
            confirmation += f" Developments: {', '.join(development_names)}."
        return confirmation
    development_name = next(
        (item["name"] for item in developments if item.get("matched")), None
    )
    geo_name = next((item["name"] for item in geos if item.get("matched")), None)
    place = ", ".join(dict.fromkeys(
        item for item in (development_name, geo_name) if item
    )) or "Property"
    details = []
    if isinstance(parsed.get("beds"), (int, float)):
        details.append(f"{parsed['beds']:g} bed")
    if isinstance(parsed.get("baths"), (int, float)):
        details.append(f"{parsed['baths']:g} bath")
    if isinstance(parsed.get("sqft"), (int, float)):
        details.append(f"{parsed['sqft']:,.0f} sqft")
    elif isinstance(parsed.get("land_sqft"), (int, float)):
        details.append(f"{parsed['land_sqft']:,.0f} land sqft")
    if parsed.get("furnishing"):
        details.append(parsed["furnishing"].lower())
    elif parsed.get("furnished") == "Yes":
        details.append("furnished")
    elif parsed.get("furnished") == "No":
        details.append("not furnished")
    period = "/month" if "Rent/Let" in parsed.get("transaction_types", []) else ""
    if parsed.get("asking_price") is not None:
        details.append(f"{_money(parsed['asking_price'])}{period}")
    parsed_agent = parsed.get("proposing_agent") or {}
    agent_name = _compact(parsed_agent.get("name"))
    agent_phone = normalize_phone_number(parsed_agent.get("phone"))
    prefix = (f"Added listing to {agent_name}: {agent_phone}:"
              if agent_name and agent_phone else "Added listing:")
    detail_text = f" — {', '.join(details)}" if details else ""
    return f"{prefix} {place}{detail_text}."


def _sanitized_payload(payload):
    hidden = {"authorization", "token", "password", "secret", "api_key"}
    return {
        key: ("[REDACTED]" if str(key).casefold() in hidden else value)
        for key, value in (payload or {}).items()
    }


def _create_import_record(object_type, payload, bubble_env):
    try:
        return rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), object_type, payload
        )
    except Exception as error:
        response = getattr(error, "response", None)
        status = getattr(response, "status_code", None)
        body = _compact(getattr(response, "text", ""))[:2000]
        print(
            f"[WHATSAPP IMPORT BUBBLE ERROR] type={object_type} "
            f"status={status if status is not None else 'unknown'} "
            f"response={body!r} payload={_sanitized_payload(payload)!r}",
            flush=True,
        )
        raise


def lead_matches_listing(lead: dict, listing: dict) -> bool:
    """Return whether one Bubble Lead and Listing satisfy deterministic match rules."""
    lead_transactions = {
        value for value in lead.get("TransactionType") or [] if value in TRANSACTION_TYPES
    }
    listing_transactions = {
        value for value in listing.get("TransactionType") or [] if value in TRANSACTION_TYPES
    }
    shared_transactions = lead_transactions & listing_transactions
    if not shared_transactions:
        return False

    lead_geos = set(_relationship_ids(lead.get("Geo")))
    lead_developments = set(_relationship_ids(lead.get("preferredDevelopments")))
    listing_geo = next(iter(_relationship_ids(listing.get("Geo"))), None)
    listing_development = next(iter(_relationship_ids(listing.get("development"))), None)
    if not (
        listing_development in lead_developments
        or listing_geo in lead_geos
    ):
        return False

    bedrooms_min = lead.get("bedroomsMin")
    applicable_budgets = {
        "Rent/Let": lead.get("budgetRent"),
        "Buy/Sell": lead.get("budgetBuy"),
    }
    if not any(
        not isinstance(applicable_budgets[transaction], bool)
        and isinstance(applicable_budgets[transaction], (int, float))
        for transaction in lead_transactions
    ) and not (
        not isinstance(bedrooms_min, bool)
        and isinstance(bedrooms_min, (int, float))
    ):
        return False

    if not isinstance(bedrooms_min, bool) and isinstance(bedrooms_min, (int, float)):
        beds = listing.get("beds")
        if isinstance(beds, bool) or not isinstance(beds, (int, float)) or beds < bedrooms_min:
            return False

    price_fields = {"Rent/Let": "priceRent", "Buy/Sell": "priceSale"}
    for transaction in shared_transactions:
        budget = applicable_budgets[transaction]
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            return True
        price = listing.get(price_fields[transaction])
        if (not isinstance(price, bool) and isinstance(price, (int, float))
                and budget * 0.8 <= price <= budget * 1.2):
            return True
    return False


def find_import_matches(created_type: str, record: dict, bubble_env: str) -> list[dict]:
    """Load the opposite Bubble object type and return deterministic matches."""
    other_type = "listing" if created_type == "lead" else "lead"
    records = rentee_app._bubble_records(
        rentee_app.get_bubble_base_url(bubble_env), other_type
    )
    if created_type == "lead":
        return [item for item in records if lead_matches_listing(record, item)]
    return [item for item in records if lead_matches_listing(item, record)]


def _matched_transaction(lead: dict, listing: dict) -> str | None:
    lead_transactions = lead.get("TransactionType") or []
    listing_transactions = listing.get("TransactionType") or []
    fields = {
        "Rent/Let": ("budgetRent", "priceRent"),
        "Buy/Sell": ("budgetBuy", "priceSale"),
    }
    for transaction in TRANSACTION_TYPES:
        if transaction not in lead_transactions or transaction not in listing_transactions:
            continue
        budget_field, price_field = fields[transaction]
        budget = lead.get(budget_field)
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            return transaction
        price = listing.get(price_field)
        if (not isinstance(price, bool) and isinstance(price, (int, float))
                and budget * 0.8 <= price <= budget * 1.2):
            return transaction
    return None


def format_import_matches(created_type: str, created_record: dict,
                          matches: list[dict], development_records=None) -> str:
    """Render deterministic matches for WhatsApp without affecting match logic."""
    development_names = {
        str(item["_id"]): _record_name(item)
        for item in development_records or [] if item.get("_id")
    }
    blocks = []
    for match in matches:
        if created_type == "lead":
            lead, listing = created_record, match
            development = match.get("development")
            if isinstance(development, dict):
                label = _record_name(development)
            else:
                label = development_names.get(str(development or ""), "")
            details = []
            if (not isinstance(match.get("beds"), bool)
                    and isinstance(match.get("beds"), (int, float)) and match["beds"] > 0):
                details.append(f"{match['beds']:g} bed")
            transaction = _matched_transaction(lead, listing)
            price_field = "priceRent" if transaction == "Rent/Let" else "priceSale"
            price = match.get(price_field) if transaction else None
            if not isinstance(price, bool) and isinstance(price, (int, float)) and price > 0:
                period = "/month" if transaction == "Rent/Let" else ""
                details.append(f"{_money(price)}{period}")
            record_id = _compact(match.get("_id"))
            path = "listing"
        else:
            lead, listing = match, created_record
            label = _compact(match.get("name"))
            details = []
            if (not isinstance(match.get("bedroomsMin"), bool)
                    and isinstance(match.get("bedroomsMin"), (int, float))
                    and match["bedroomsMin"] > 0):
                details.append(f"{match['bedroomsMin']:g}+ bed")
            transaction = _matched_transaction(lead, listing)
            budget_field = "budgetRent" if transaction == "Rent/Let" else "budgetBuy"
            budget = match.get(budget_field) if transaction else None
            if not isinstance(budget, bool) and isinstance(budget, (int, float)) and budget > 0:
                details.append(f"budget {_money(budget)}")
            record_id = _compact(match.get("_id"))
            path = "lead"
        summary = label + (f" — {', '.join(details)}" if label and details else "")
        if not label:
            summary = ", ".join(details)
        url = f"https://www.rentee.asia/{path}/{record_id}" if record_id else ""
        blocks.append("\n".join(item for item in (summary, url) if item))
    count = len(matches)
    noun = "listing" if created_type == "lead" else "lead"
    if count != 1:
        noun += "s"
    return f"Found {count} matching {noun}:\n\n" + "\n\n".join(blocks)


def process_whatsapp_import(raw_text: str, bubble_env: str = "live", *,
                            import_type: str | None = None,
                            geo_records=None, development_records=None) -> dict:
    """Parse, resolve, create directly through Bubble's Data API, and summarize."""
    parsed = parse_forwarded_message(raw_text, import_type=import_type)
    if parsed["type"] == "unknown":
        return {"status": "unknown", "type": "unknown"}
    if bubble_env not in {"live", "development"}:
        raise ValueError("bubble_env must be 'live' or 'development'.")
    valid_import = (
        any(parsed.get(field) for field in (
            "geo_names", "preferred_development_names", "transaction_types",
            "property_types",
        ))
        or parsed.get("budget") is not None
        or parsed.get("bedrooms_min") is not None
    ) if parsed["type"] == "lead" else (
        bool(parsed.get("development_name") or parsed.get("geo_name"))
        and (
            bool(parsed.get("transaction_types") or parsed.get("property_type"))
            or parsed.get("asking_price") is not None
            or parsed.get("beds") is not None
        )
    )
    if not valid_import:
        return {
            "status": "invalid", "type": parsed["type"], "parsed": parsed,
            "confirmation": (
                "I couldn't add that property record because it did not contain enough "
                "details. Please resend the full forwarded message."
            ),
        }
    parsed_agent = parsed.get("proposing_agent")
    parsed_agent = parsed_agent if isinstance(parsed_agent, dict) else {}
    try:
        proposing_agent = resolve_or_create_proposing_agent(
            parsed_agent.get("name"), parsed_agent.get("phone"),
            parsed_agent.get("ren"), bubble_env,
        )
    except Exception as error:
        normalized = normalize_phone_number(parsed_agent.get("phone"))
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=failed "
              f"error={type(error).__name__}", flush=True)
        proposing_agent = {
            "status": "error", "user_id": None,
            "normalized_phone": normalized,
            "name": _agent_value(parsed_agent.get("name")),
            "ren": _agent_value(parsed_agent.get("ren")),
        }
    if geo_records is None or development_records is None:
        records = rentee_app._property_entity_records(bubble_env)
        geo_records = records["geo"] if geo_records is None else geo_records
        development_records = records["condo"] if development_records is None else development_records
    geo_records, development_records = list(geo_records or []), list(development_records or [])
    verification_context = _verification_context(parsed, raw_text)

    if parsed["type"] == "lead":
        geos = resolve_geo_names(parsed.get("geo_names", []), geo_records)
        developments, created_developments = _resolve_or_verify_developments(
            parsed.get("preferred_development_names", []), development_records,
            geo_records, verification_context, bubble_env,
        )
        if not any(item.get("matched") for item in geos):
            geos.extend(_derived_geos(developments, geo_records))
        payload = build_lead_payload(
            parsed, geos, developments, proposing_agent
        )
        bubble_id = _create_import_record("lead", payload, bubble_env)
        result = {
            "status": "processed", "type": "lead", "bubble_id": bubble_id,
            "parsed": parsed,
            "proposing_agent_user_id": proposing_agent.get("user_id"),
            "proposing_agent": {
                key: proposing_agent.get(key) for key in (
                    "status", "user_id", "normalized_phone", "name", "ren",
                )
            },
            "resolved_geos": [_public_resolution(item) for item in geos if item.get("matched")],
            "resolved_developments": [_public_resolution(item) for item in developments
                                      if item.get("matched")],
            "created_developments": [_public_resolution(item)
                                     for item in created_developments],
            "unresolved_geo_names": [item["raw_name"] for item in geos
                                     if not item.get("matched")],
            "unresolved_development_names": [item["raw_name"] for item in developments
                                             if not item.get("matched")],
            "confirmation": _confirmation(parsed, geos, developments),
        }
    else:
        explicit = resolve_geo_name(parsed.get("geo_name"), geo_records) \
            if parsed.get("geo_name") else None
        developments, created_developments = _resolve_or_verify_developments(
            [parsed["development_name"]] if parsed.get("development_name") else [],
            development_records, geo_records, verification_context, bubble_env,
        )
        development = developments[0] if developments else None
        derived = _derived_geos([development] if development else [], geo_records)
        resolved_geo = explicit if explicit and explicit.get("matched") else (derived[0] if derived else None)
        if explicit and explicit.get("matched") and derived and explicit["id"] != derived[0]["id"]:
            print(f"{LOG_PREFIX} geo conflict explicit={explicit['name']!r} "
                  f"development_geo={derived[0]['name']!r}", flush=True)
        payload = build_listing_payload(
            parsed, resolved_geo, development, proposing_agent
        )
        bubble_id = _create_import_record("listing", payload, bubble_env)
        geos_for_confirmation = [resolved_geo] if resolved_geo else []
        developments_for_confirmation = [development] if development else []
        result = {
            "status": "processed", "type": "listing", "bubble_id": bubble_id,
            "parsed": parsed,
            "proposing_agent_user_id": proposing_agent.get("user_id"),
            "proposing_agent": {
                key: proposing_agent.get(key) for key in (
                    "status", "user_id", "normalized_phone", "name", "ren",
                )
            },
            "resolved_geo": _public_resolution(resolved_geo),
            "resolved_development": _public_resolution(development) if development and development.get("matched") else None,
            "created_developments": [_public_resolution(item)
                                     for item in created_developments],
            "unresolved_geo_names": ([explicit["raw_name"]]
                                     if explicit and not explicit.get("matched") else []),
            "unresolved_development_names": ([development["raw_name"]]
                                             if development and not development.get("matched") else []),
            "confirmation": _confirmation(parsed, geos_for_confirmation,
                                          developments_for_confirmation),
        }
    matches = find_import_matches(
        parsed["type"], dict(payload, _id=bubble_id), bubble_env
    )
    result["matches"] = matches
    if matches:
        result["confirmation"] += "\n\n" + format_import_matches(
            parsed["type"], dict(payload, _id=bubble_id), matches,
            development_records,
        )
    print(f"{LOG_PREFIX} created type={parsed['type']} id={bubble_id}", flush=True)
    return result
