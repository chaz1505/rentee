"""Import forwarded WhatsApp property requirements into Bubble.

This module deliberately owns all ingestion-specific parsing, validation,
resolution and payload construction.  It reuses app.py's configured OpenAI
client and Bubble Data API helpers, but is not wired into the webhook yet.
"""

from __future__ import annotations

import datetime
import calendar
import hashlib
import json
import re
from typing import Any, Iterable

import app as rentee_app
import development_resolver


LOG_PREFIX = "[WHATSAPP IMPORT]"
TRANSACTION_TYPES = ("Rent/Let", "Buy/Sell")
PROPERTY_TYPES = ("Condo", "Landed")
IMPORT_ROUTING_THRESHOLD = 0.90
MATCH_AVAILABILITY_MONTHS = 3

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
        "location_references": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "Original meaningful wording for every place where the Lead wants property, "
                "including areas, roads, landmarks, and neighbourhood references."
            ),
        },
        "location_reference": {
            "type": ["string", "null"],
            "description": (
                "Original meaningful wording for where the Listing is located, including an "
                "area, road, landmark, or neighbourhood reference."
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
        "property_type": {"type": ["string", "null"], "enum": [*PROPERTY_TYPES, None]},
        "budget": {"type": ["number", "null"], "minimum": 0},
        "price_rent": {
            "type": ["number", "null"], "minimum": 0,
            "description": "Listing monthly rent price only; null when no rent price is stated.",
        },
        "price_sale": {
            "type": ["number", "null"], "minimum": 0,
            "description": "Listing sale price only; null when no sale price is stated.",
        },
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
                "pea": {"type": ["string", "null"]},
            },
            "required": ["name", "phone", "ren", "pea"],
            "additionalProperties": False,
        },
    },
    "required": [
        "type", "geo_names", "geo_name", "location_references", "location_reference",
        "preferred_development_names",
        "development_name", "transaction_types", "property_types",
        "property_type", "budget", "price_rent", "price_sale",
        "bedrooms_min", "beds",
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


def normalize_source_message(value: Any) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines).strip())


def source_message_hash(value: Any) -> str:
    return hashlib.sha256(normalize_source_message(value).encode("utf-8")).hexdigest()


def _location_references(values: Iterable[Any]) -> list[str]:
    result = []
    for value in _unique_strings(values):
        clean = re.sub(r"^(?:near(?:\s+to)?|close\s+to)\s+", "", value,
                       flags=re.IGNORECASE).strip(" .,;:-")
        generic = re.sub(r"[^a-z]+", " ", clean.casefold()).strip()
        if generic in {
            "work place", "working place", "workplace", "walking distance",
            "work place walking distance", "working place walking distance",
            "workplace walking distance",
        }:
            continue
        if clean:
            result.append(clean)
    return _unique_strings(result)


def _without_agent_metadata_locations(values, proposing_agent, source_agency_name):
    metadata_keys = {
        " ".join(re.sub(r"[^a-z0-9]+", " ", _compact(value).casefold()).split())
        for value in (
            (proposing_agent or {}).get("name"),
            (proposing_agent or {}).get("ren"),
            (proposing_agent or {}).get("pea"),
            source_agency_name,
        )
        if _compact(value)
    }
    return [value for value in values or [] if " ".join(re.sub(
        r"[^a-z0-9]+", " ", _compact(value).casefold()
    ).split()) not in metadata_keys]


def _canonical_property_type(value: Any) -> str | None:
    return {
        "condo": "Condo", "apartment": "Condo",
        "landed": "Landed", "house": "Landed",
    }.get(_compact(value).casefold())


def _validate_parsed(value: Any) -> dict:
    if not isinstance(value, dict) or value.get("type") not in {"lead", "listing", "unknown"}:
        return {"type": "unknown"}
    if value["type"] == "unknown":
        return {"type": "unknown"}

    transactions = _unique_strings(value.get("transaction_types") or [])
    transactions = [item for item in transactions if item in TRANSACTION_TYPES]
    property_types = _unique_strings(
        _canonical_property_type(item) for item in value.get("property_types") or []
    )
    property_type = _canonical_property_type(value.get("property_type"))
    raw_agent = value.get("proposing_agent")
    raw_agent = raw_agent if isinstance(raw_agent, dict) else {}
    proposing_agent = {
        key: (_compact(raw_agent.get(key)) or None)
        for key in ("name", "phone", "ren")
    }
    if _compact(raw_agent.get("pea")):
        proposing_agent["pea"] = _compact(raw_agent["pea"])

    if value["type"] == "lead":
        result = {
            "type": "lead",
            "geo_names": _unique_strings(value.get("geo_names") or []),
            "location_references": _location_references(
                value.get("location_references") or value.get("geo_names") or []
            ),
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
        if _compact(value.get("source_agency_name")):
            result["source_agency_name"] = _compact(value["source_agency_name"])
        result["location_references"] = _without_agent_metadata_locations(
            result["location_references"], proposing_agent,
            result.get("source_agency_name"),
        )
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
    location_references = _location_references([
        value.get("location_reference") or value.get("geo_name")
    ])
    if location_references:
        result["location_reference"] = location_references[0]
    if _compact(value.get("geo_name")):
        result["geo_name"] = _compact(value["geo_name"])
    if _compact(value.get("development_name")):
        result["development_name"] = _compact(value["development_name"])
    if property_type:
        result["property_type"] = property_type
    for key in ("price_rent", "price_sale", "beds"):
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
    filtered_location_references = _without_agent_metadata_locations(
        [result.get("location_reference")], proposing_agent,
        result.get("source_agency_name"),
    )
    if not filtered_location_references:
        result.pop("location_reference", None)
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
            "Normalize RM8k=8000, RM 8,500=8500, 1.8m=1800000, RM3.5m=3500000, "
            "and RM3.5 mil/RM3.5 million=3500000. For a Lead budget range, store the "
            "upper value as the single maximum budget: 'Budget between 5m to 6m' means "
            "budget=6000000, 'Budget RM8k-10k' means budget=10000, and 'Budget 3.5m to "
            "4m' means budget=4000000. "
            "For Listings, extract a stated rent amount only into price_rent and a stated sale "
            "amount only into price_sale. A combined WTL/WTS Listing may have both. Never copy "
            "or infer one transaction's price from the other; leave the unstated price null. "
            "Normalize bedroom forms to an integer. Property types may only be the canonical "
            "values Condo or Landed. Map apartment/condominium/condo to Condo. Map house, "
            "bungalow, semi-D, terrace, link house, detached house, and landed house to Landed. "
            "A Listing has exactly one property_type when one is stated; a Lead may have neither, "
            "one, or both canonical property_types. Never infer Condo merely from a development "
            "name. Put actual areas/neighbourhoods only in geo_names/geo_name, and "
            "put Development, condo, or project names only in preferred_development_names/"
            "development_name. A named Development is not a Geo. If no actual area is stated, "
            "geo_names must be empty and geo_name must be null; Geo can be derived later. Geo fields "
            "are only residential search areas or listing locations explicitly stated as such. "
            "Never put schools, workplaces, malls, landmarks, offices, or stations in Geo fields; "
            "they remain context only. Separately preserve the original meaningful location "
            "wording in location_references for Leads and location_reference for Listings. These "
            "location reference fields may include roads, landmarks, and other real-world location "
            "descriptions even when they are not residential Geo names. Store only the named or "
            "geographically identifiable place itself (for example 'Sultan Ismail', not 'Near to "
            "Sultan Ismail'). Generic phrases such as 'near to working place' or 'walking distance' "
            "are context, not standalone location references. Extract proposing_agent "
            "only from a credible agent/contact "
            "signature, such as a final name + registration/REN + agency + phone block, or an "
            "explicit agent/negotiator/contact/PIC association. Malaysian agent signatures often "
            "end the message and contain an individual's name, team name, 'Real Estate "
            "Negotiator', REN number, mobile number, agency/company name, agency registration, "
            "and office number. Use the individual person's name, not the team or agency. "
            "Registration forms include REN 12345, REN12345, E2265, PEA2495, PEA 2495, and "
            "PEA-2495. Put REN/E registrations only in proposing_agent.ren and PEA registrations "
            "only in proposing_agent.pea; never copy one registration type into the other. When both an "
            "individual Malaysian mobile (+601/01) and an office or landline (+603/03) appear in "
            "that signature, select the mobile as proposing_agent.phone and do not treat the "
            "office number as ambiguous. Leave phone null only when multiple plausible individual "
            "mobile numbers remain ambiguous. Do not mistake the buyer, tenant, client, owner, "
            "team, agency, or agency registration number for "
            "the proposing agent. For both Leads and Listings, put the agent signature's agency "
            "or company name in source_agency_name. Never invent agent fields. "
            "Text identified as an agent name, agency/company name, phone number, REN number, or PEA number "
            "must not also be extracted as a location or Development unless the message "
            "explicitly uses that text as part of the property requirement. Return null/empty "
            "values when evidence is weak; do not invent facts. For unknown, leave "
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
            "study/family/maid room counts, outdoor area, unit number, and owner name/contact "
            "only when explicitly stated. Put useful listing details not captured "
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


def _normalized_registration(value, registration_type):
    text = _compact(value).upper()
    prefix = r"(?:REN|E)" if registration_type == "REN" else r"PEA"
    if not re.fullmatch(rf"(?:{prefix}(?:\s|-)*)?\d+", text):
        return None
    digits = re.sub(r"\D", "", text)
    return digits or None


def _normalized_ren(value):
    return _normalized_registration(value, "REN")


def _normalized_pea(value):
    return _normalized_registration(value, "PEA")


def _normalized_agency_name(value):
    return " ".join(re.sub(
        r"[^a-z0-9]+", " ", _compact(value).casefold()
    ).split())


def _resolve_or_create_agency(source_agency_name, bubble_env):
    clean_name = _agent_value(source_agency_name)
    if not clean_name:
        return None
    base_url = rentee_app.get_bubble_base_url(bubble_env)
    key = _normalized_agency_name(clean_name)
    try:
        matches = [
            record for record in rentee_app._bubble_records(base_url, "Agency")
            if record.get("_id") and _normalized_agency_name(
                record.get("name") or record.get("Name")
            ) == key
        ]
    except Exception as error:
        print(f"[WHATSAPP IMPORT AGENCY] name={clean_name!r} "
              f"action=lookup_failed error={type(error).__name__}", flush=True)
        return None
    if len(matches) == 1:
        return str(matches[0]["_id"])
    if len(matches) > 1:
        print(f"[WHATSAPP IMPORT AGENCY] name={clean_name!r} "
              f"status=ambiguous count={len(matches)}", flush=True)
        return None
    try:
        agency_id = rentee_app._bubble_create(
            base_url, "Agency", {"name": clean_name}
        )
    except Exception as error:
        print(f"[WHATSAPP IMPORT AGENCY] name={clean_name!r} "
              f"action=create_failed error={type(error).__name__}", flush=True)
        return None
    print(f"[WHATSAPP IMPORT AGENCY] name={clean_name!r} "
          f"action=created id={agency_id}", flush=True)
    return str(agency_id)


def _enrich_existing_agent(user, name, ren, pea, normalized_phone, bubble_env,
                           agency_id=None, agency_name=None):
    updates = {}
    existing_name = _agent_value(user.get("name"))
    existing_ren = _normalized_ren(user.get("REN"))
    existing_pea = _normalized_pea(user.get("PEA"))
    if name and not existing_name:
        updates["name"] = name
    elif name and existing_name and name.casefold() != existing_name.casefold():
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} conflict=name "
              f"existing={existing_name!r} incoming={name!r}", flush=True)
    if ren and not existing_ren:
        updates["REN"] = ren
    elif ren and existing_ren and ren != existing_ren:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} conflict=REN "
              f"existing={existing_ren!r} incoming={ren!r}", flush=True)
    if pea and not existing_pea:
        updates["PEA"] = pea
    elif pea and existing_pea and pea != existing_pea:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} conflict=PEA "
              f"existing={existing_pea!r} incoming={pea!r}", flush=True)
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
    existing_agencies = _relationship_ids(user.get("Agency"))
    if agency_id and not existing_agencies:
        try:
            rentee_app._bubble_patch(
                f"{rentee_app.get_bubble_base_url(bubble_env)}/obj/user/{user['_id']}",
                {"Agency": agency_id},
            )
        except Exception as error:
            print(f"[WHATSAPP IMPORT AGENCY] name={agency_name!r} "
                  f"action=assign_failed error={type(error).__name__}", flush=True)
        else:
            user = {**user, "Agency": agency_id}
            print(f"[WHATSAPP IMPORT AGENCY] name={agency_name!r} "
                  f"action=assigned user_id={user['_id']}", flush=True)
    elif agency_id and agency_id not in existing_agencies:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized_phone!r} conflict=Agency "
              f"existing={existing_agencies!r} incoming={agency_name!r}", flush=True)
    return user


def resolve_or_create_proposing_agent(name: str | None, phone: str | None,
                                      ren: str | None,
                                      bubble_env: str = "live", *,
                                      pea: str | None = None,
                                      source_agency_name: str | None = None) -> dict:
    """Resolve one proposing agent by canonical phone, creating conservatively."""
    clean_name = _agent_value(name)
    clean_ren, clean_pea = _normalized_ren(ren), _normalized_pea(pea)
    normalized = normalize_phone_number(phone)
    print(f"[WHATSAPP IMPORT AGENT] raw_phone={_compact(phone)!r} "
          f"normalized={normalized!r}", flush=True)
    if not normalized:
        return {"status": "no_phone", "user_id": None,
                "normalized_phone": None, "name": clean_name,
                "ren": clean_ren, "pea": clean_pea}
    try:
        matches = rentee_app.find_bubble_users_by_phone(normalized, bubble_env)
    except Exception as error:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=lookup_failed "
              f"error={type(error).__name__}", flush=True)
        return {"status": "error", "user_id": None,
                "normalized_phone": normalized, "name": clean_name,
                "ren": clean_ren, "pea": clean_pea}
    if len(matches) > 1:
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} "
              f"action=duplicate_existing count={len(matches)}", flush=True)
        return {"status": "duplicate_existing", "user_id": None,
                "normalized_phone": normalized, "name": clean_name,
                "ren": clean_ren, "pea": clean_pea}
    if len(matches) == 1:
        agency_id = _resolve_or_create_agency(source_agency_name, bubble_env)
        user = _enrich_existing_agent(
            dict(matches[0]), clean_name, clean_ren, clean_pea, normalized, bubble_env,
            agency_id, _agent_value(source_agency_name),
        )
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=existing "
              f"user_id={user['_id']}", flush=True)
        return {"status": "existing", "user_id": str(user["_id"]),
                "normalized_phone": normalized,
                "name": _agent_value(user.get("name")) or clean_name,
                "ren": _normalized_ren(user.get("REN")) or clean_ren,
                "pea": _normalized_pea(user.get("PEA")) or clean_pea,
                "user": user}
    payload = {
        "phone": normalized,
        "email": build_internal_user_email(normalized),
    }
    if clean_name:
        payload["name"] = clean_name
    if clean_ren:
        payload["REN"] = clean_ren
    if clean_pea:
        payload["PEA"] = clean_pea
    agency_id = _resolve_or_create_agency(source_agency_name, bubble_env)
    if agency_id:
        payload["Agency"] = agency_id
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
                dict(raced[0]), clean_name, clean_ren, clean_pea, normalized, bubble_env,
                agency_id, _agent_value(source_agency_name),
            )
            print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=existing "
                  f"user_id={user['_id']}", flush=True)
            return {"status": "existing", "user_id": str(user["_id"]),
                    "normalized_phone": normalized,
                    "name": _agent_value(user.get("name")) or clean_name,
                    "ren": _normalized_ren(user.get("REN")) or clean_ren,
                    "pea": _normalized_pea(user.get("PEA")) or clean_pea,
                    "user": user}
        print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=create_failed "
              f"error={type(create_error).__name__}", flush=True)
        return {"status": "error", "user_id": None,
                "normalized_phone": normalized, "name": clean_name,
                "ren": clean_ren, "pea": clean_pea}
    user = {"_id": user_id, **payload}
    print(f"[WHATSAPP IMPORT AGENT] phone={normalized!r} action=created "
          f"user_id={user_id}", flush=True)
    return {"status": "created", "user_id": str(user_id),
            "normalized_phone": normalized, "name": clean_name, "ren": clean_ren,
            "pea": clean_pea,
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


def _create_proposed_geo(canonical_name, geo_records, bubble_env="live"):
    canonical = _compact(canonical_name)
    if not canonical:
        return None
    existing = resolve_geo_name(canonical, geo_records)
    if existing.get("matched"):
        existing["outcome"] = "match_existing"
        return existing
    payload = {"Name": canonical, "status": "proposed"}
    try:
        geo_id = rentee_app._bubble_create(
            rentee_app.get_bubble_base_url(bubble_env), "geo", payload
        )
    except Exception as error:
        print(f"[GEO CREATE] canonical={canonical!r} action=failed "
              f"error={type(error).__name__}", flush=True)
        return None
    record = {"_id": geo_id, **payload}
    geo_records.append(record)
    print(f"[GEO CREATE] canonical={canonical!r} action=created id={geo_id}", flush=True)
    return {"matched": True, "id": str(geo_id), "name": canonical,
            "method": "verified_created", "record": record, "created": True,
            "outcome": "create_geo"}


def verify_geo_reference(raw_reference, geo_records, context, *, single=False,
                         bubble_env="live"):
    """Resolve a location to existing Geos or create one verified major area."""
    raw = _compact(raw_reference)
    canonical_names = _unique_strings(_record_name(item) for item in geo_records or [])
    if not raw:
        print(f"[GEO VERIFY] raw={raw!r} status=unresolved", flush=True)
        return []
    focused_context = {
        key: value for key, value in (context or {}).items()
        if value not in (None, "", [], {})
    }
    request = dict(
            model="gpt-5-mini", tools=[{"type": "web_search"}],
            input=(
                "Resolve this Malaysian property location reference for Rentee. Return exactly "
                "one outcome: match_existing, create_geo, or unresolved. Use match_existing only "
                "when the requested location is actually within that canonical Geo, or is an "
                "alternative or more-specific name for the same geographic area. Do not match "
                "an existing Geo merely because it is nearby, in the same metropolitan area, or "
                "would be a reasonable alternative property-search area. For example, Bandar "
                "Puchong Jaya maps to existing Puchong and Jalan Maarof maps to existing Bangsar, "
                "but SS12, Subang Jaya must not map to Petaling Jaya. For match_existing, copy "
                "names exactly from CANONICAL GEOS. Propose "
                "create_geo only when no appropriate existing Geo exists and canonical_name is "
                "a meaningful recognised property-search area such as a city, township, or "
                "established major neighbourhood. A raw reference may itself be a road, taman, "
                "small precinct, landmark, or other granular place: canonicalise it to its "
                "recognised major parent property-search area when that parent can be confidently "
                "established. For example, Wangsa Baiduri (SS12), Subang Jaya and SS12, Subang "
                "Jaya both become create_geo with canonical_name Subang Jaya. The granularity "
                "restriction applies to canonical_name being created, not to the raw reference. "
                "Never create Wangsa Baiduri, SS12, a road, taman, small precinct, landmark, "
                "Development/condo, or another overly granular place as a Geo. Equally, never "
                "create a Geo that is too broad: an entire city or metropolitan region normally "
                "divided into multiple distinct property-search areas, a state, a country, or a "
                "vague broad region. create_geo is only appropriate for a recognised property-"
                "search area at useful Rentee search granularity. For example, KL City Area or "
                "Kuala Lumpur must not create Kuala Lumpur, and Selangor must not create Selangor. "
                "If a meaningful input identifies only an overly broad area, return unresolved "
                "so the original location reference can be preserved. Apply this symmetrically: "
                "do not create a Geo that is too granular or too broad; create only an appropriate "
                "intermediate-granularity property-search area. Return unresolved "
                "only when no suitable recognised parent at appropriate property-search "
                "granularity can be confidently established. " + (
                    "This is a Listing: match at most one existing Geo, and only with sufficient "
                    "evidence from its Development/location/context."
                    if single else
                    "This is a Lead: return every canonical Geo that satisfies the same strict "
                    "same-area rule. Do not add adjacent or nearby areas."
                ) +
                f"\n\nLOCATION REFERENCE:\n{raw}\n\nCANONICAL GEOS:\n"
                f"{json.dumps(canonical_names, ensure_ascii=False)}\n\nCONTEXT:\n"
                f"{json.dumps(focused_context, ensure_ascii=False)}"
            ),
            reasoning={"effort": "low"}, max_output_tokens=800, timeout=20,
            text={"format": {"type": "json_schema", "name": "geo_verification",
                "strict": True, "schema": {
                    "type": "object", "properties": {
                        "outcome": {"type": "string", "enum": [
                            "match_existing", "create_geo", "unresolved"]},
                        "geo_names": {"type": "array", "items": {"type": "string"}},
                        "canonical_name": {"type": ["string", "null"]},
                    },
                    "required": ["outcome", "geo_names", "canonical_name"],
                    "additionalProperties": False,
                }}},
        )
    try:
        response = rentee_app.client.responses.create(**request)
        value = development_resolver._verifier_response_value(response)
    except ValueError as error:
        retry_request = dict(request)
        retry_request["input"] += (
            "\n\nReturn only valid JSON matching the required schema. No markdown or prose."
        )
        try:
            response = rentee_app.client.responses.create(**retry_request)
            value = development_resolver._verifier_response_value(response)
        except Exception as retry_error:
            print(f"[GEO VERIFY] raw={raw!r} status=unresolved "
                  f"error={type(retry_error).__name__}: {retry_error}", flush=True)
            return []
    except Exception as error:
        print(f"[GEO VERIFY] raw={raw!r} status=unresolved "
              f"error={type(error).__name__}: {error}", flush=True)
        return []
    # Accept the former response shape for compatibility with an in-flight retry.
    outcome = value.get("outcome") or (
        "match_existing" if value.get("geo_names") else "unresolved"
    )
    requested = _unique_strings(value.get("geo_names") or [])
    if outcome == "create_geo":
        created = _create_proposed_geo(
            value.get("canonical_name"), geo_records, bubble_env
        )
        if created:
            return [created]
        outcome = "unresolved"
    if outcome != "match_existing":
        requested = []
    if single and len(requested) > 1:
        requested = []
    resolved = resolve_geo_names(requested, geo_records)
    matches = [item for item in resolved if item.get("matched")]
    if len(matches) != len(requested):
        matches = []
    if matches:
        for match in matches:
            match["outcome"] = "match_existing"
        print(f"[GEO VERIFY] raw={raw!r} resolved="
              f"{[item['name'] for item in matches]!r}", flush=True)
    else:
        print(f"[GEO VERIFY] raw={raw!r} status=unresolved", flush=True)
    return matches


def resolve_location_references(references, geo_records, context, *, single=False,
                                bubble_env="live"):
    resolutions = []
    for reference in _unique_strings(references):
        direct = resolve_geo_name(reference, geo_records)
        if direct.get("matched"):
            resolutions.append(direct)
            continue
        fallback = verify_geo_reference(
            reference, geo_records, context, single=single, bubble_env=bubble_env
        )
        if fallback:
            resolutions.extend(fallback)
        else:
            resolutions.append(direct)
    return resolutions


def _verification_context(parsed, raw_text):
    return {
        "geo_names": _unique_strings(parsed.get("geo_names") or []),
        "geo_name": _compact(parsed.get("geo_name")) or None,
        "location_references": _unique_strings(parsed.get("location_references") or []),
        "location_reference": _compact(parsed.get("location_reference")) or None,
        "property_type": parsed.get("property_type"),
        "property_types": _unique_strings(
            _canonical_property_type(item) for item in parsed.get("property_types", [])
        ),
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
                geo_verifier=verify_geo_reference,
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
        created_geo = outcome.get("geo_record")
        if created_geo and not any(
            str(item.get("_id")) == str(created_geo.get("_id"))
            for item in geo_records
        ):
            geo_records.append(created_geo)
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
    payload = {"source": "whatsapp", "exposure": "public"}
    if proposing_agent and proposing_agent.get("user_id"):
        payload["owner"] = proposing_agent["user_id"]
    geo_ids = _matched_ids(resolved_geos)
    development_ids = _matched_ids(resolved_developments)
    transactions = [v for v in parsed.get("transaction_types", []) if v in TRANSACTION_TYPES]
    property_types = _unique_strings(
        _canonical_property_type(v) for v in parsed.get("property_types", [])
    )
    if geo_ids:
        payload["Geo"] = geo_ids
    location_references = _unique_strings(
        parsed.get("location_references") or parsed.get("geo_names") or []
    )
    if location_references:
        payload["locationReferences"] = location_references
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
    payload = {"exposure": "public", "source": "whatsapp"}
    if proposing_agent and proposing_agent.get("user_id"):
        payload["owner"] = proposing_agent["user_id"]
    if resolved_geo and resolved_geo.get("matched"):
        payload["Geo"] = resolved_geo["id"]
    location_reference = _compact(
        parsed.get("location_reference") or parsed.get("geo_name")
    )
    if location_reference:
        payload["locationReference"] = location_reference
    if resolved_development and resolved_development.get("matched"):
        payload["development"] = resolved_development["id"]
    transactions = [v for v in parsed.get("transaction_types", []) if v in TRANSACTION_TYPES]
    if transactions:
        payload["TransactionType"] = list(dict.fromkeys(transactions))
    property_type = _canonical_property_type(parsed.get("property_type"))
    if property_type:
        payload["propertyType"] = property_type
    if isinstance(parsed.get("beds"), (int, float)):
        payload["beds"] = parsed["beds"]
    if ("Rent/Let" in transactions
            and isinstance(parsed.get("price_rent"), (int, float))):
        payload["priceRent"] = parsed["price_rent"]
    if ("Buy/Sell" in transactions
            and isinstance(parsed.get("price_sale"), (int, float))):
        payload["priceSale"] = parsed["price_sale"]
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
    transactions = parsed.get("transaction_types", [])
    if "Rent/Let" in transactions and parsed.get("price_rent") is not None:
        details.append(f"{_money(parsed['price_rent'])}/month")
    if "Buy/Sell" in transactions and parsed.get("price_sale") is not None:
        details.append(_money(parsed["price_sale"]))
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


def _listing_date_eligible(listing, today=None):
    raw_date = listing.get("availability_date")
    if raw_date in (None, ""):
        raw_date = listing.get("tenantExpiry")
    if raw_date in (None, ""):
        return True
    try:
        if isinstance(raw_date, datetime.datetime):
            available_date = raw_date.date()
        elif isinstance(raw_date, datetime.date):
            available_date = raw_date
        else:
            available_date = datetime.date.fromisoformat(str(raw_date)[:10])
    except (TypeError, ValueError):
        return False
    current = today or datetime.date.today()
    month_index = current.month - 1 + MATCH_AVAILABILITY_MONTHS
    year = current.year + month_index // 12
    month = month_index % 12 + 1
    window_end = datetime.date(
        year, month, min(current.day, calendar.monthrange(year, month)[1])
    )
    return available_date <= window_end


def _find_duplicate_import(object_type, owner_id, message_hash, bubble_env):
    if not owner_id:
        return None
    constraints = [
        {"key": "owner", "constraint_type": "equals", "value": owner_id},
        {"key": "sourceMessageHash", "constraint_type": "equals",
         "value": message_hash},
    ]
    for existing in rentee_app._bubble_records(
            rentee_app.get_bubble_base_url(bubble_env), object_type, constraints):
        if (existing.get("_id")
                and owner_id in _relationship_ids(existing.get("owner"))
                and existing.get("sourceMessageHash") == message_hash):
            return existing
    return None


def lead_matches_listing(lead: dict, listing: dict) -> bool:
    """Return whether one Bubble Lead and Listing satisfy deterministic match rules."""
    if lead.get("cancelled") is True:
        return False
    if not _relationship_ids(listing.get("owner")):
        return False
    if listing.get("availability") is False:
        return False
    if not _listing_date_eligible(listing):
        return False
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
    lead_property_value = lead.get("propertyTypes") or []
    if not isinstance(lead_property_value, list):
        lead_property_value = [lead_property_value]
    lead_property_types = {
        canonical for canonical in (
            _canonical_property_type(value) for value in lead_property_value
        ) if canonical
    }
    has_budget = any(
        not isinstance(applicable_budgets[transaction], bool)
        and isinstance(applicable_budgets[transaction], (int, float))
        for transaction in lead_transactions
    )
    has_bedrooms = (
        not isinstance(bedrooms_min, bool)
        and isinstance(bedrooms_min, (int, float))
    )
    if not (lead_property_types or has_bedrooms or has_budget):
        return False

    if lead_property_types:
        listing_property_type = _canonical_property_type(listing.get("propertyType"))
        if listing_property_type not in lead_property_types:
            return False

    if has_bedrooms:
        beds = listing.get("beds")
        if isinstance(beds, bool) or not isinstance(beds, (int, float)) or beds < bedrooms_min:
            return False

    price_fields = {"Rent/Let": "priceRent", "Buy/Sell": "priceSale"}
    for transaction in shared_transactions:
        budget = applicable_budgets[transaction]
        if not isinstance(budget, bool) and isinstance(budget, (int, float)):
            price = listing.get(price_fields[transaction])
            if (isinstance(price, bool) or not isinstance(price, (int, float))
                    or not budget * 0.8 <= price <= budget * 1.2):
                return False
    return True


def find_import_matches(created_type: str, record: dict, bubble_env: str) -> list[dict]:
    """Load the opposite Bubble object type and return deterministic matches."""
    other_type = "listing" if created_type == "lead" else "lead"
    records = rentee_app._bubble_records(
        rentee_app.get_bubble_base_url(bubble_env), other_type
    )
    if created_type == "lead":
        return [item for item in records if lead_matches_listing(record, item)]
    return [item for item in records if lead_matches_listing(item, record)]


def create_missing_match_records(
        created_type, created_id, matches, bubble_env, created_record=None):
    """Persist each unique deterministic Lead/Listing pair once."""
    base_url = rentee_app.get_bubble_base_url(bubble_env)
    seen = set()
    for matched in matches or []:
        matched_id = _compact(matched.get("_id"))
        lead_id, listing_id = (
            (created_id, matched_id) if created_type == "lead"
            else (matched_id, created_id)
        )
        pair = (_compact(lead_id), _compact(listing_id))
        if not all(pair) or pair in seen:
            continue
        seen.add(pair)
        constraints = [
            {"key": "lead", "constraint_type": "equals", "value": pair[0]},
            {"key": "listing", "constraint_type": "equals", "value": pair[1]},
        ]
        if next(iter(rentee_app._bubble_records(
                base_url, "match", constraints)), None):
            continue
        lead_record, listing_record = (
            (created_record, matched) if created_type == "lead"
            else (matched, created_record)
        )
        lead_owner = next(
            iter(_relationship_ids((lead_record or {}).get("owner"))),
            None,
        )
        listing_owner = next(
            iter(_relationship_ids((listing_record or {}).get("owner"))),
            None,
        )
        payload = {
            "lead": pair[0],
            "listing": pair[1],
            "match_source": "whatsapp",
        }
        if lead_owner:
            payload["lead_owner"] = lead_owner
        if listing_owner:
            payload["listing_owner"] = listing_owner
        rentee_app._bubble_create(
            base_url, "match", payload
        )


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
                          matches: list[dict], development_records=None,
                          bubble_env=None) -> str:
    """Render deterministic matches for WhatsApp without affecting match logic."""
    development_names = {
        str(item["_id"]): _record_name(item)
        for item in development_records or [] if item.get("_id")
    }
    owners = {}
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
        owner = match.get("owner")
        if isinstance(owner, dict):
            owner_record = owner
        else:
            owner_id = _compact(owner)
            if owner_id and owner_id not in owners and bubble_env:
                try:
                    owners[owner_id] = rentee_app.bubble(
                        f"{rentee_app.get_bubble_base_url(bubble_env)}/obj/user/{owner_id}"
                    )
                except Exception:
                    owners[owner_id] = {}
            owner_record = owners.get(owner_id, {})
        agent_name = _compact(owner_record.get("name"))
        agent_phone = _compact(owner_record.get("phone"))
        summary = label + (f" — {', '.join(details)}" if label and details else "")
        if not label:
            summary = ", ".join(details)
        url = f"https://www.rentee.asia/{path}/{record_id}" if record_id else ""
        blocks.append("\n".join(item for item in (
            summary,
            f"Agent: {agent_name}" if agent_name else "",
            f"Phone: {agent_phone}" if agent_phone else "",
            url,
        ) if item))
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
        bool(parsed.get("development_name") or parsed.get("geo_name")
             or parsed.get("location_reference"))
        and (
            bool(parsed.get("transaction_types") or parsed.get("property_type"))
            or parsed.get("price_rent") is not None
            or parsed.get("price_sale") is not None
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
            pea=parsed_agent.get("pea"),
            source_agency_name=parsed.get("source_agency_name"),
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
            "pea": _agent_value(parsed_agent.get("pea")),
        }
    message_hash = source_message_hash(raw_text)
    duplicate = _find_duplicate_import(
        parsed["type"], proposing_agent.get("user_id"), message_hash, bubble_env
    )
    if duplicate:
        bubble_id = str(duplicate["_id"])
        matches = find_import_matches(parsed["type"], duplicate, bubble_env)
        create_missing_match_records(
            parsed["type"], bubble_id, matches, bubble_env,
            created_record=duplicate,
        )
        duplicate_developments = development_records
        if duplicate_developments is None:
            duplicate_developments = rentee_app._property_entity_records(
                bubble_env
            )["condo"]
        confirmation = f"This {parsed['type']} already exists."
        if matches:
            confirmation += "\n\n" + format_import_matches(
                parsed["type"], duplicate, matches, duplicate_developments,
                bubble_env,
            )
        print(f"{LOG_PREFIX} duplicate type={parsed['type']} id={bubble_id}", flush=True)
        return {
            "status": "duplicate", "type": parsed["type"], "bubble_id": bubble_id,
            "parsed": parsed, "proposing_agent_user_id": proposing_agent.get("user_id"),
            "matches": matches, "confirmation": confirmation,
        }
    if geo_records is None or development_records is None:
        records = rentee_app._property_entity_records(bubble_env)
        geo_records = records["geo"] if geo_records is None else geo_records
        development_records = records["condo"] if development_records is None else development_records
    geo_records, development_records = list(geo_records or []), list(development_records or [])
    verification_context = _verification_context(parsed, raw_text)

    if parsed["type"] == "lead":
        location_references = _unique_strings(
            parsed.get("location_references") or parsed.get("geo_names") or []
        )
        geos = resolve_geo_names(location_references, geo_records)
        developments, created_developments = _resolve_or_verify_developments(
            parsed.get("preferred_development_names", []), development_records,
            geo_records, verification_context, bubble_env,
        )
        has_geo = any(item.get("matched") for item in geos)
        has_development = any(item.get("matched") for item in developments)
        if not has_geo and not has_development:
            geos = resolve_location_references(
                location_references, geo_records, verification_context,
                bubble_env=bubble_env,
            )
            has_geo = any(item.get("matched") for item in geos)
        if not has_geo:
            geos.extend(_derived_geos(developments, geo_records))
        payload = build_lead_payload(
            parsed, geos, developments, proposing_agent
        )
        payload["waMessage"] = raw_text
        payload["sourceMessageHash"] = message_hash
        bubble_id = _create_import_record("lead", payload, bubble_env)
        created_record = dict(payload, _id=bubble_id)
        result = {
            "status": "processed", "type": "lead", "bubble_id": bubble_id,
            "parsed": parsed,
            "proposing_agent_user_id": proposing_agent.get("user_id"),
            "proposing_agent": {
                key: proposing_agent.get(key) for key in (
                    "status", "user_id", "normalized_phone", "name", "ren", "pea",
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
        path = "lead" if parsed["type"] == "lead" else "listing"
        result["confirmation"] += (
            f"\n\nSee here: https://www.rentee.asia/{path}/{bubble_id}"
        )
        if not has_geo and not has_development and location_references:
            result["confirmation"] += (
                f" Couldn't resolve Geo: {', '.join(location_references)}."
            )
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
        fallback = []
        location_reference = _compact(
            parsed.get("location_reference") or parsed.get("geo_name")
        )
        if not resolved_geo and location_reference:
            fallback = resolve_location_references(
                [location_reference], geo_records, verification_context, single=True,
                bubble_env=bubble_env,
            )
            resolved_geo = next(
                (item for item in fallback if item.get("matched")), None
            )
        payload = build_listing_payload(
            parsed, resolved_geo, development, proposing_agent
        )
        payload["waMessage"] = raw_text
        payload["sourceMessageHash"] = message_hash
        bubble_id = _create_import_record("listing", payload, bubble_env)
        created_record = dict(payload, _id=bubble_id)
        geos_for_confirmation = [resolved_geo] if resolved_geo else []
        developments_for_confirmation = [development] if development else []
        result = {
            "status": "processed", "type": "listing", "bubble_id": bubble_id,
            "parsed": parsed,
            "proposing_agent_user_id": proposing_agent.get("user_id"),
            "proposing_agent": {
                key: proposing_agent.get(key) for key in (
                    "status", "user_id", "normalized_phone", "name", "ren", "pea",
                )
            },
            "resolved_geo": _public_resolution(resolved_geo),
            "resolved_development": _public_resolution(development) if development and development.get("matched") else None,
            "created_developments": [_public_resolution(item)
                                     for item in created_developments],
            "unresolved_geo_names": ([location_reference]
                                     if location_reference and not resolved_geo else []),
            "unresolved_development_names": ([development["raw_name"]]
                                             if development and not development.get("matched") else []),
            "confirmation": _confirmation(parsed, geos_for_confirmation,
                                          developments_for_confirmation),
        }
        path = "lead" if parsed["type"] == "lead" else "listing"
        result["confirmation"] += (
            f"\n\nSee here: https://www.rentee.asia/{path}/{bubble_id}"
        )
    matches = find_import_matches(
        parsed["type"], created_record, bubble_env
    )
    create_missing_match_records(
        parsed["type"], bubble_id, matches, bubble_env,
        created_record=created_record,
    )
    result["matches"] = matches
    if matches:
        result["confirmation"] += "\n\n" + format_import_matches(
            parsed["type"], created_record, matches,
            development_records, bubble_env,
        )
    print(f"{LOG_PREFIX} created type={parsed['type']} id={bubble_id}", flush=True)
    return result
