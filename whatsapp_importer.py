"""Import forwarded WhatsApp property requirements into Bubble.

This module deliberately owns all ingestion-specific parsing, validation,
resolution and payload construction.  It reuses app.py's configured OpenAI
client and Bubble Data API helpers, but is not wired into the webhook yet.
"""

from __future__ import annotations

import difflib
import json
import re
from typing import Any, Iterable

import app as rentee_app


LOG_PREFIX = "[WHATSAPP IMPORT]"
TRANSACTION_TYPES = ("Rent/Let", "Buy/Sell")
PROPERTY_TYPES = ("Condo", "Landed", "Apartment", "House")
FUZZY_THRESHOLD = 0.90
FUZZY_MARGIN = 0.08
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
                "Residential search areas explicitly preferred by the sender; never "
                "schools, workplaces, malls, landmarks, offices, or stations."
            ),
        },
        "geo_name": {
            "type": ["string", "null"],
            "description": (
                "Explicit residential area of the listing; never a school, workplace, "
                "mall, landmark, office, or station."
            ),
        },
        "preferred_development_names": {
            "type": "array", "items": {"type": "string"},
        },
        "development_name": {"type": ["string", "null"]},
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
        "beds": {"type": ["integer", "null"], "minimum": 0},
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
        "proposing_agent",
    ],
    "additionalProperties": False,
}


def _compact(value: Any) -> str:
    return " ".join(str(value or "").split())


def _normalized(value: Any) -> str:
    text = _compact(value).casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


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
            "development name. Put areas and developments in their distinct fields. Geo fields "
            "are only residential search areas or listing locations explicitly stated as such. "
            "Never put schools, workplaces, malls, landmarks, offices, or stations in Geo fields; "
            "they remain context only. Extract proposing_agent only from a credible agent/contact "
            "signature, such as a final name + registration/REN + agency + phone block, or an "
            "explicit agent/negotiator/contact/PIC association. Registration forms include REN "
            "12345, REN12345, E2265, and PEA 1234. If multiple phone numbers make ownership "
            "ambiguous, leave the agent phone null. Do not mistake tenant or owner contacts for "
            "the proposing agent. Never invent agent fields. Return "
            "null/empty values when evidence is weak; do not invent facts. For unknown, leave "
            "all other fields empty/null.\n\nMESSAGE:\n" + text
        ),
        reasoning={"effort": "low"},
        max_output_tokens=700,
        timeout=20,
        text={"format": {
            "type": "json_schema", "name": "whatsapp_property_import",
            "strict": True, "schema": _parser_schema(import_type),
        }},
    )
    if str(getattr(response, "status", "completed") or "completed").lower() != "completed":
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


def _resolve_name(name: Any, records: Iterable[dict], kind: str) -> dict:
    raw = _compact(name)
    if not raw:
        return {"matched": False, "raw_name": raw}
    candidates = [(record, _record_name(record)) for record in records or []]
    candidates = [(record, canonical) for record, canonical in candidates
                  if record.get("_id") and canonical]

    exact = [(record, canonical) for record, canonical in candidates if canonical == raw]
    method = "exact"
    if not exact:
        exact = [(record, canonical) for record, canonical in candidates
                 if canonical.casefold() == raw.casefold()]
        method = "case_insensitive_exact"
    if not exact:
        key = _normalized(raw)
        exact = [(record, canonical) for record, canonical in candidates
                 if _normalized(canonical) == key]
        method = "normalized_exact"
    if len(exact) == 1:
        record, canonical = exact[0]
        result = {"matched": True, "id": str(record["_id"]), "name": canonical,
                  "method": method, "record": record}
        print(f"{LOG_PREFIX} {kind} {method} raw={raw!r} canonical={canonical!r} "
              f"id={record['_id']}", flush=True)
        return result
    if len(exact) > 1:
        print(f"{LOG_PREFIX} {kind} unresolved raw={raw!r}", flush=True)
        return {"matched": False, "raw_name": raw, "reason": "ambiguous"}

    key = _normalized(raw)
    scored = sorted((
        (difflib.SequenceMatcher(None, key, _normalized(canonical)).ratio(), record, canonical)
        for record, canonical in candidates
    ), key=lambda item: item[0], reverse=True)
    best = scored[0] if scored else None
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if best and best[0] >= FUZZY_THRESHOLD and best[0] - second_score >= FUZZY_MARGIN:
        score, record, canonical = best
        print(f"{LOG_PREFIX} {kind} fuzzy raw={raw!r} canonical={canonical!r} "
              f"id={record['_id']} score={score:.3f}", flush=True)
        return {"matched": True, "id": str(record["_id"]), "name": canonical,
                "method": "fuzzy", "score": score, "record": record}
    print(f"{LOG_PREFIX} {kind} unresolved raw={raw!r}", flush=True)
    return {"matched": False, "raw_name": raw,
            **({"reason": "ambiguous"} if best and best[0] >= FUZZY_THRESHOLD else {})}


def resolve_geo_name(name, geo_records):
    return _resolve_name(name, geo_records, "geo")


def resolve_geo_names(names, geo_records):
    return [resolve_geo_name(name, geo_records) for name in names or []]


def resolve_development_name(name, development_records):
    return _resolve_name(name, development_records, "development")


def resolve_development_names(names, development_records):
    return [resolve_development_name(name, development_records) for name in names or []]


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


def verify_development_candidate(raw_name: str, context: dict,
                                 bubble_env: str = "live") -> dict:
    """Verify a missing Malaysian Development using the existing web-search client."""
    raw = _compact(raw_name)
    if not raw:
        return {"status": "not_found", "raw_name": raw}
    focused_context = {
        key: value for key, value in (context or {}).items()
        if value not in (None, "", [], {})
    }
    response = rentee_app.client.responses.create(
        model="gpt-5-mini",
        tools=[{"type": "web_search"}],
        input=(
            "Verify whether the DEVELOPMENT CANDIDATE is a real Malaysian residential "
            "property development. Use the import context only to disambiguate identity and "
            "area. Schools, workplaces, malls, landmarks, offices and stations are contextual "
            "clues, never the returned residential geo_name. Establish the canonical/current "
            "development name and residential area. Prefer an official developer/project "
            "source plus an established Malaysian property portal, publication, map/location, "
            "or major agency source. A same-word weak page is insufficient. Return verified "
            "only when identity is unambiguous and at least two independent credible source "
            "URLs corroborate it. Otherwise return ambiguous when multiple plausible identities "
            "exist, or not_found when credible evidence is absent. confidence is identity "
            "confidence, not search-result relevance.\n\n"
            f"DEVELOPMENT CANDIDATE: {raw}\n"
            f"IMPORT CONTEXT: {json.dumps(focused_context, ensure_ascii=False)}"
        ),
        reasoning={"effort": "low"},
        max_output_tokens=700,
        timeout=20,
        text={"format": {
            "type": "json_schema", "name": "development_web_verification",
            "strict": True, "schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": [
                        "verified", "ambiguous", "not_found",
                    ]},
                    "canonical_name": {"type": ["string", "null"]},
                    "geo_name": {"type": ["string", "null"]},
                    "verification_url": {"type": ["string", "null"]},
                    "evidence": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string"},
                            "source": {"type": "string"},
                            "support": {"type": "string"},
                        },
                        "required": ["url", "source", "support"],
                        "additionalProperties": False,
                    }},
                    "candidates": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "geo_name": {"type": ["string", "null"]},
                            "url": {"type": ["string", "null"]},
                        },
                        "required": ["name", "geo_name", "url"],
                        "additionalProperties": False,
                    }},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": [
                    "status", "canonical_name", "geo_name", "verification_url",
                    "evidence", "candidates", "confidence",
                ],
                "additionalProperties": False,
            },
        }},
    )
    try:
        value = json.loads(str(response.output_text or ""))
    except (TypeError, json.JSONDecodeError):
        value = {}
    status = value.get("status")
    confidence = value.get("confidence")
    evidence = value.get("evidence") if isinstance(value.get("evidence"), list) else []
    evidence_urls = {
        item.get("url") for item in evidence if isinstance(item, dict)
        and str(item.get("url") or "").startswith(("https://", "http://"))
    }
    verified = (
        status == "verified"
        and isinstance(confidence, (int, float)) and confidence >= 0.90
        and _compact(value.get("canonical_name"))
        and _compact(value.get("geo_name"))
        and str(value.get("verification_url") or "").startswith(("https://", "http://"))
        and len(evidence_urls) >= 2
    )
    if verified:
        result = {
            "status": "verified", "raw_name": raw,
            "canonical_name": _compact(value["canonical_name"]),
            "geo_name": _compact(value["geo_name"]),
            "verification_url": value["verification_url"],
            "evidence": evidence, "confidence": float(confidence),
        }
        print(
            f"[DEVELOPMENT VERIFY] raw={raw!r} status=verified "
            f"canonical={result['canonical_name']!r} geo={result['geo_name']!r} "
            f"confidence={result['confidence']:.2f}", flush=True,
        )
        return result
    candidates = value.get("candidates") if isinstance(value.get("candidates"), list) else []
    if status == "ambiguous" or candidates or status == "verified":
        result = {
            "status": "ambiguous", "raw_name": raw,
            "candidates": candidates,
            "confidence": float(confidence) if isinstance(confidence, (int, float)) else 0.0,
        }
        print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=ambiguous "
              f"candidates={len(candidates)}", flush=True)
        return result
    print(f"[DEVELOPMENT VERIFY] raw={raw!r} status=not_found", flush=True)
    return {"status": "not_found", "raw_name": raw}


def _fresh_development_records(bubble_env):
    return list(rentee_app._bubble_records(
        rentee_app.get_bubble_base_url(bubble_env), "condo"
    ))


def create_verified_development(canonical_name, resolved_geo, verification_url,
                                bubble_env="live", *, development_records=None):
    """Reuse or create one verified Development, with one race recovery lookup."""
    canonical = _compact(canonical_name)
    if not canonical or not resolved_geo or not resolved_geo.get("matched"):
        print(f"[DEVELOPMENT CREATE] canonical={canonical!r} action=skipped "
              "reason=geo_unresolved", flush=True)
        return None
    records = (list(development_records) if development_records is not None
               else _fresh_development_records(bubble_env))
    existing = resolve_development_name(canonical, records)
    if existing.get("matched"):
        print(f"[DEVELOPMENT CREATE] canonical={canonical!r} "
              f"action=existing_reused id={existing['id']}", flush=True)
        return existing
    payload = {
        "Name": canonical,
        "Geo": resolved_geo["id"],
        "verification_status": "Verified",
        "source": "WhatsApp Import",
        "verification_url": verification_url,
    }
    try:
        development_id = rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), "condo", payload
        )
    except Exception as error:
        try:
            raced = resolve_development_name(
                canonical, _fresh_development_records(bubble_env)
            )
        except Exception:
            raced = {"matched": False}
        if raced.get("matched"):
            print(f"[DEVELOPMENT CREATE] canonical={canonical!r} "
                  f"action=existing_reused id={raced['id']}", flush=True)
            return raced
        print(f"[DEVELOPMENT CREATE] canonical={canonical!r} action=failed "
              f"error={type(error).__name__}", flush=True)
        return None
    record = {"_id": development_id, "Name": canonical, "Geo": resolved_geo["id"],
              **{key: payload[key] for key in (
                  "verification_status", "source", "verification_url",
              )}}
    print(f"[DEVELOPMENT CREATE] canonical={canonical!r} "
          f"action=created id={development_id}", flush=True)
    return {"matched": True, "id": str(development_id), "name": canonical,
            "method": "web_verified_created", "record": record, "created": True}


def _resolve_or_verify_developments(names, development_records, geo_records,
                                    context, bubble_env):
    resolutions, created = [], []
    for raw_name in _unique_strings(names):
        resolved = resolve_development_name(raw_name, development_records)
        if resolved.get("matched"):
            resolutions.append(resolved)
            continue
        print(f"{LOG_PREFIX} development unresolved raw={raw_name!r} action=verify_web",
              flush=True)
        try:
            verification = verify_development_candidate(raw_name, context, bubble_env)
        except Exception as error:
            print(f"[DEVELOPMENT VERIFY] raw={raw_name!r} status=not_found "
                  f"error={type(error).__name__}", flush=True)
            resolutions.append(resolved)
            continue
        if verification.get("status") != "verified":
            resolutions.append(resolved)
            continue
        resolved_geo = resolve_geo_name(verification.get("geo_name"), geo_records)
        if not resolved_geo.get("matched"):
            print(f"[DEVELOPMENT CREATE] canonical={verification.get('canonical_name')!r} "
                  "action=skipped reason=geo_unresolved", flush=True)
            resolutions.append(resolved)
            continue
        # Check the caller's latest view first, then force a fresh Bubble check in
        # create_verified_development before any POST.
        canonical = resolve_development_name(
            verification["canonical_name"], development_records
        )
        if canonical.get("matched"):
            print(f"[DEVELOPMENT CREATE] canonical={verification['canonical_name']!r} "
                  f"action=existing_reused id={canonical['id']}", flush=True)
            resolutions.append(canonical)
            continue
        created_or_reused = create_verified_development(
            verification["canonical_name"], resolved_geo,
            verification["verification_url"], bubble_env,
        )
        if created_or_reused and created_or_reused.get("matched"):
            resolutions.append(created_or_reused)
            development_records.append(created_or_reused["record"])
            if created_or_reused.get("created"):
                created.append(created_or_reused)
        else:
            resolutions.append(resolved)
    return resolutions, created


def _matched_ids(resolutions: Iterable[dict]) -> list[str]:
    return list(dict.fromkeys(item["id"] for item in resolutions or [] if item.get("matched")))


def _apply_proposing_agent_payload(payload, proposing_agent):
    if not proposing_agent:
        return
    normalized = proposing_agent.get("normalized_phone")
    name = _agent_value(proposing_agent.get("name"))
    if normalized:
        payload["ProposingAgentNumber"] = normalized
        if name:
            payload["ProposingAgentName"] = name


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
    _apply_proposing_agent_payload(payload, proposing_agent)
    return payload


def build_listing_payload(parsed, resolved_geo, resolved_development,
                          proposing_agent=None) -> dict:
    payload = {}
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
    _apply_proposing_agent_payload(payload, proposing_agent)
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
        return f"Added lead: {bits}{bed_text}{suffix}."
    name = next((item["name"] for item in developments if item.get("matched")), None)
    if not name:
        name = next((item["name"] for item in geos if item.get("matched")), "Property")
    period = "/month" if "Rent/Let" in parsed.get("transaction_types", []) else ""
    price = f", {_money(parsed.get('asking_price'))}{period}" if parsed.get("asking_price") is not None else ""
    return f"Added listing: {name}{bed_text}{price}."


def process_whatsapp_import(raw_text: str, bubble_env: str = "live", *,
                            import_type: str | None = None,
                            geo_records=None, development_records=None) -> dict:
    """Parse, resolve, create directly through Bubble's Data API, and summarize."""
    parsed = parse_forwarded_message(raw_text, import_type=import_type)
    if parsed["type"] == "unknown":
        return {"status": "unknown", "type": "unknown"}
    if bubble_env not in {"live", "development"}:
        raise ValueError("bubble_env must be 'live' or 'development'.")
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
        bubble_id = rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), "lead", payload
        )
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
        bubble_id = rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), "listing", payload
        )
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
    print(f"{LOG_PREFIX} created type={parsed['type']} id={bubble_id}", flush=True)
    return result
