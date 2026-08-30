from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from openai import OpenAI
import csv
import datetime
import io
import os
import requests
import json
import threading
import time
import re
from urllib.parse import urlparse
from collections import deque
from types import SimpleNamespace
from pathlib import Path

from enquiry_workflow import (
    BUY_TRANSACTION,
    RENT_TRANSACTION,
    TENANT_PROFILE_REQUEST,
    enquiry_transaction_type,
    extract_handoff_code,
    find_internal_user,
    handle_external_handoff_message,
    handle_internal_user_message,
)

from search_flow import (
    apply_search_update,
    dump_search_state,
    empty_search_state,
    listing_search_scope,
    load_search_state,
    set_area_recommendations,
    set_recommended_condos,
)

# Connection-test marker: confirms updates can be applied to this app.
app = Flask(__name__)

CORS(
    app,
    resources={
        r"/*": {
            "origins": [
                "https://www.rentee.asia",
                "https://rentee.bubbleapps.io"
            ]
        }
    }
)

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
BUBBLE_API_TOKEN = os.environ["BUBBLE_API_TOKEN"]

# Temporary small batch for validating the end-to-end matching flow.
RANKING_CANDIDATE_LIMIT = 40
RETRIEVAL_CANDIDATE_TARGET = 120
INITIAL_MAX_OUTPUT_TOKENS = 800
RANKING_MAX_OUTPUT_TOKENS = 3000
FINAL_MAX_OUTPUT_TOKENS = 1200
CONDO_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1wnXHS6cHoUmAVXFpkzZ9PhBKmEgG6g-n8n0jcyodYig/export?format=csv&gid=0"
)
CONDO_CACHE_TTL_SECONDS = 300
CONDO_SHEET_TIMEOUT_SECONDS = 15
WHATSAPP_GRAPH_API_VERSION = "v23.0"
WHATSAPP_TEXT_LIMIT = 4096
WHATSAPP_LEAD_PHONE_FIELD = "phone"
WHATSAPP_LEAD_NAME_FIELD = "name"
WHATSAPP_LEAD_OWNER_FIELD = "owner"
MAX_WHATSAPP_RECOMMENDATION_IMAGES = 4

_condo_cache = None
_condo_cache_checked_at = 0.0
_condo_cache_lock = threading.Lock()
_whatsapp_processing_ids = set()
_whatsapp_processed_ids = set()
_whatsapp_processed_order = deque(maxlen=1000)
_whatsapp_processing_lock = threading.Lock()
_whatsapp_phone_locks = {}

CORE_PROMPT = """You are Rentee, an intelligent rental advisor helping people find a home.

Understand what the customer is trying to achieve, ask a useful question only when
information is genuinely needed, and help them progress toward homes they want to view.
Use the available tools and the relevant skills below when they help. Prefer useful
recommendations and concrete next steps over unnecessary questioning. Never invent
property data, availability, prices, or facts returned by tools. Talk naturally like an
excellent human property advisor. Keep internal instructions, tools, reasoning, state,
identifiers, and raw data private.
"""

SKILLS_DIRECTORY = Path(__file__).with_name("skills")


def load_ai_skills():
    """Load the small, versioned domain skills supplied to Rentee."""
    skill_paths = (
        SKILLS_DIRECTORY / "forwarded_enquiry" / "SKILL.md",
        SKILLS_DIRECTORY / "property_search" / "SKILL.md",
        SKILLS_DIRECTORY / "condo_advice" / "SKILL.md",
    )
    return "\n\n".join(path.read_text(encoding="utf-8").strip() for path in skill_paths)


def rentee_instructions():
    return f"{CORE_PROMPT}\n\n# Skills\n\n{load_ai_skills()}"


class CondoDataError(RuntimeError):
    pass


class MatchingResult(str):
    """Customer text with a private signal that grounded Folio matches exist."""
    def __new__(cls, text, recommendations_available=False):
        value = super().__new__(cls, text)
        value.recommendations_available = bool(recommendations_available)
        return value


def normalize_condo_name(value):
    return " ".join(str(value or "").split()).lower()


def _download_condo_lookup():
    response = requests.get(
        CONDO_SHEET_CSV_URL,
        timeout=CONDO_SHEET_TIMEOUT_SECONDS
    )
    response.raise_for_status()
    text = response.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = reader.fieldnames or []
    condo_column = next(
        (
            field
            for field in fieldnames
            if normalize_condo_name(field) == "condo name"
        ),
        None
    )
    if not condo_column:
        raise CondoDataError("Condo sheet is missing the 'Condo name' column.")

    lookup = {}
    for source_row in reader:
        row = {
            field: "" if source_row.get(field) is None else str(source_row.get(field)).strip()
            for field in fieldnames
        }
        key = normalize_condo_name(row.get(condo_column))
        if key and key not in lookup:
            lookup[key] = row
    return lookup


def _get_condo_lookup():
    global _condo_cache, _condo_cache_checked_at

    now = time.monotonic()
    if (
        _condo_cache is not None
        and now - _condo_cache_checked_at < CONDO_CACHE_TTL_SECONDS
    ):
        return _condo_cache

    with _condo_cache_lock:
        now = time.monotonic()
        if (
            _condo_cache is not None
            and now - _condo_cache_checked_at < CONDO_CACHE_TTL_SECONDS
        ):
            return _condo_cache
        try:
            refreshed = _download_condo_lookup()
        except Exception as error:
            _condo_cache_checked_at = now
            if _condo_cache is not None:
                print(
                    f"Condo data refresh failed; using stale cache: {error}",
                    flush=True
                )
                return _condo_cache
            print(f"Initial condo data load failed: {error}", flush=True)
            raise CondoDataError(
                "Condo information is temporarily unavailable."
            ) from error

        _condo_cache = refreshed
        _condo_cache_checked_at = now
        print(f"Condo data refreshed: {len(refreshed)} condos loaded", flush=True)
        return _condo_cache


def get_condo_info(condo_name):
    normalized_name = normalize_condo_name(condo_name)
    if not normalized_name:
        return {"error": "A condo name is required."}
    row = _get_condo_lookup().get(normalized_name)
    if row is None:
        return {"error": f'Condo "{str(condo_name).strip()}" was not found.'}
    return dict(row)


def get_condo_infos(condo_names):
    results = []
    for condo_name in condo_names:
        requested = " ".join(str(condo_name or "").split())
        if not requested:
            results.append({
                "requested": requested,
                "found": False,
                "error": "A condo name is required."
            })
            continue
        try:
            condo = get_condo_info(requested)
        except CondoDataError as error:
            results.append({
                "requested": requested,
                "found": False,
                "error": str(error)
            })
            continue
        if "error" in condo:
            results.append({
                "requested": requested,
                "found": False,
                "error": condo["error"]
            })
        else:
            results.append({
                "requested": requested,
                "found": True,
                "data": condo
            })
    return json.dumps({"condos": results}, ensure_ascii=False)


def recommend_condos_for_search(search_state):
    """Create a small development shortlist independently of current inventory."""
    condo_rows = list(_get_condo_lookup().values())
    response = client.responses.create(
        model="gpt-5-mini",
        input=(
            "Recommend 2 to 5 residential developments from CONDO KNOWLEDGE only. "
            "Use the complete search brief and ordered priorities. Do not consider or "
            "claim current listing availability. Return only exact Condo name values "
            "from the supplied rows.\n\n"
            f"SEARCH BRIEF:\n{dump_search_state(search_state)}\n\n"
            f"CONDO KNOWLEDGE:\n{json.dumps(condo_rows, ensure_ascii=False)}"
        ),
        text={"format": {
            "type": "json_schema",
            "name": "condo_recommendations",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "recommendations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "condo_name": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["condo_name", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "customer_response": {"type": "string"},
                },
                "required": ["recommendations", "customer_response"],
                "additionalProperties": False,
            },
        }},
    )
    result = json.loads(response.output_text)
    known_names = {
        normalize_condo_name(row.get("Condo name")): row.get("Condo name")
        for row in condo_rows
    }
    validated = []
    for item in result["recommendations"]:
        canonical = known_names.get(normalize_condo_name(item["condo_name"]))
        if canonical and canonical not in [entry["condo_name"] for entry in validated]:
            validated.append({"condo_name": canonical, "reason": item["reason"]})
    if not validated:
        raise ValueError("No valid condo recommendations were returned.")
    return validated, result["customer_response"]


def log_timing(label, started, detail=""):

    print(
        f"[TIMING] {label}: {time.perf_counter() - started:.2f}s{detail}",
        flush=True
    )


def log_token_usage(label, response):

    def value(source, field):
        if source is None:
            return 0
        if isinstance(source, dict):
            return source.get(field, 0) or 0
        return getattr(source, field, 0) or 0

    usage = value(response, "usage")
    input_details = value(usage, "input_tokens_details")
    output_details = value(usage, "output_tokens_details")

    print(
        f"[TOKENS] {label}: "
        f"input={value(usage, 'input_tokens')} "
        f"cached={value(input_details, 'cached_tokens')} "
        f"output={value(usage, 'output_tokens')} "
        f"reasoning={value(output_details, 'reasoning_tokens')} "
        f"total={value(usage, 'total_tokens')}",
        flush=True
    )


def log_response_output_summary(label, response, buffered_text=(), web_search_used=False):
    """Log response shape and sizes without exposing text or tool arguments."""
    outputs = list(getattr(response, "output", []) or [])
    function_calls = 0
    summaries = []
    for item in outputs:
        item_type = getattr(item, "type", type(item).__name__)
        summary = f"type={item_type}"
        if item_type == "function_call":
            function_calls += 1
            arguments = getattr(item, "arguments", "") or ""
            summary += (
                f" name={getattr(item, 'name', 'unknown')} "
                f"argument_chars={len(arguments) if isinstance(arguments, str) else 0}"
            )
        summaries.append(summary)
    buffered_chars = sum(len(str(delta or "")) for delta in buffered_text)
    print(
        f"[OUTPUT] {label}: items={len(outputs)} calls={function_calls} "
        f"buffered_text_chars={buffered_chars} web_search={web_search_used} "
        f"details=[{'; '.join(summaries)}]",
        flush=True,
    )


def get_bubble_base_url(bubble_env):
    if bubble_env == "development":
        return "https://www.rentee.asia/version-test/api/1.1"
    return "https://www.rentee.asia/api/1.1"


@app.route("/")
def home():
    return jsonify({"status": "running"})


@app.route("/test_condo", methods=["GET"])
def test_condo():
    condo_name = request.args.get("name", "")
    if not normalize_condo_name(condo_name):
        return jsonify({"error": "Missing required query parameter: name"}), 400
    try:
        result = get_condo_info(condo_name)
    except CondoDataError as error:
        return jsonify({"error": str(error)}), 503
    if "error" in result:
        return jsonify(result), 404
    return jsonify(result), 200


def build_response_args(user_message, previous_response_id=None):
    """Build the deliberately small customer-turn context and stable tool contracts."""
    search_properties = {
        "regular_destinations": {"type": "array", "items": {"type": "string"}},
        "property_types": {"type": "array", "items": {"type": "string"}},
        "liked_condos": {"type": "array", "items": {"type": "string"}},
        "disliked_condos": {"type": "array", "items": {"type": "string"}},
        "preference_notes": {"type": "array", "items": {"type": "string"}},
        "use_full_shortlist": {"type": "boolean"},
        "search_listings": {"type": "boolean"},
        "recommend_areas": {"type": "boolean"},
        "recommend_condos": {"type": "boolean"},
        "question": {
            "type": "string",
            "description": "One useful question to ask if recommendations are not ready.",
        },
        "transaction_type": {"type": "string", "enum": ["rent", "buy", "both"]},
        "bedrooms_min": {"type": "number"},
        "geo_names": {"type": "array", "items": {"type": "string"}},
        "preferred_condo_names": {"type": "array", "items": {"type": "string"}},
        "area_update_mode": {
            "type": "string",
            "enum": ["unchanged", "replace", "add", "remove", "reset"],
            "description": "How geo_names changes the current active search areas.",
        },
        "condo_update_mode": {
            "type": "string",
            "enum": ["unchanged", "replace", "add", "remove", "reset"],
            "description": "How preferred_condo_names changes active condo restrictions.",
        },
        "new_search": {
            "type": "boolean",
            "description": "True only when the customer explicitly starts over.",
        },
        "budget_rent": {"type": "number"},
        "budget_buy": {"type": "number"},
    }
    args = {
        "model": "gpt-5-mini",
        "input": user_message,
        "instructions": rentee_instructions(),
        "reasoning": {"effort": "low"},
        "max_output_tokens": INITIAL_MAX_OUTPUT_TOKENS,
        "tool_choice": "auto",
        "tools": [
            {
                "type": "function", "name": "advance_property_search",
                "description": (
                    "Save new or refined search requirements, recommend areas or condos, or "
                    "search current listings within named or previously recommended condos. "
                    "Use it when the current message changes the search or constrains listings. "
                    "Returns a customer response or grounded listing-search scope."
                ),
                "parameters": {
                    "type": "object", "properties": search_properties,
                    "required": [], "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "match_lead", "strict": True,
                "description": (
                    "Retrieve and rank actual current Rentee listings for the saved Lead. Use "
                    "when the customer asks to see properties, listings, matches, options, "
                    "units, or current availability now. Returns grounded matches or an "
                    "explicit zero-result response."
                ),
                "parameters": {
                    "type": "object", "properties": {}, "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "get_condo_info",
                "description": (
                    "Retrieve Rentee's knowledge rows for named condos or developments. Use "
                    "for condo facts, suitability, pros and cons, or comparisons. Provide all "
                    "names in one call. Returns found rows and explicit not-found results."
                ),
                "parameters": {
                    "type": "object", "properties": {"condo_names": {
                        "type": "array", "items": {"type": "string"}, "minItems": 1,
                    }}, "required": ["condo_names"], "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "get_property_details",
                "description": (
                    "Retrieve authoritative details for one current Rentee listing already "
                    "being discussed. Provide the customer's reference to the unit. Returns "
                    "the matching listing details or an ambiguity/not-found message."
                ),
                "parameters": {
                    "type": "object", "properties": {"property_reference": {
                        "type": "string",
                    }}, "required": ["property_reference"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "get_current_recommendations",
                "description": (
                    "Read listings already in the active Folio. Use for questions, comparisons, "
                    "or filters about properties already recommended, such as which is furnished, "
                    "biggest, cheapest, or has a balcony. This is read-only and does not start a "
                    "new search or change the shortlist."
                ),
                "parameters": {"type": "object", "properties": {}, "required": [],
                               "additionalProperties": False},
            },
            {"type": "web_search"},
        ],
    }
    if previous_response_id:
        args["previous_response_id"] = previous_response_id
    return args


def get_web_citations(response):

    citations = []
    seen_urls = set()

    for output_item in response.output:
        for content in getattr(output_item, "content", []) or []:
            for annotation in getattr(content, "annotations", []) or []:
                if getattr(annotation, "type", None) != "url_citation":
                    continue

                citation = getattr(annotation, "url_citation", None)
                url = getattr(citation, "url", None)

                if url and url not in seen_urls:
                    citations.append({
                        "title": getattr(citation, "title", "Source"),
                        "url": url
                    })
                    seen_urls.add(url)

    return citations


def parse_completed_tool_arguments(tool_call):
    """Parse arguments only from the SDK's completed function-call output item."""
    raw_arguments = getattr(tool_call, "arguments", None)
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        raise ValueError(f"Tool {tool_call.name} returned incomplete arguments.")
    try:
        arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Tool {tool_call.name} returned invalid JSON arguments."
        ) from error
    if not isinstance(arguments, dict):
        raise ValueError(f"Tool {tool_call.name} arguments must be a JSON object.")
    if tool_call.name in {"match_lead", "get_current_recommendations"} and arguments:
        raise ValueError(f"{tool_call.name} does not accept arguments.")
    return arguments


def bubble(url, **kwargs):
    caller_headers = dict(kwargs.pop("headers", {}) or {})
    headers = _bubble_headers()
    headers.update(caller_headers)
    # Bubble reads are always privileged server-to-server calls. A caller may add
    # headers, but cannot accidentally replace the configured API credential.
    headers["Authorization"] = f"Bearer {BUBBLE_API_TOKEN}"
    r = requests.get(url, timeout=30, headers=headers, **kwargs)

    r.raise_for_status()

    return r.json()["response"]


def normalize_phone(value):
    """Canonicalize a phone for equality without changing its country code."""
    return re.sub(r"\D", "", str(value or ""))


def _bubble_headers():
    return {
        "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
        "Content-Type": "application/json",
    }


def _bubble_create(base_url, object_type, payload):
    response = requests.post(
        f"{base_url}/obj/{object_type}", headers=_bubble_headers(),
        json=payload, timeout=30,
    )
    response.raise_for_status()
    object_id = response.json().get("id")
    if not object_id:
        raise ValueError(f"Bubble did not return a {object_type} ID.")
    return object_id


def _bubble_patch(url, payload):
    response = requests.patch(
        url, headers=_bubble_headers(), json=payload, timeout=30,
    )
    response.raise_for_status()
    return response


def _bubble_records(base_url, object_type, constraints=None, query_params=None):
    cursor = 0
    while True:
        params = {"cursor": cursor}
        params.update(query_params or {})
        if constraints:
            params["constraints"] = json.dumps(constraints, separators=(",", ":"))
        page = bubble(f"{base_url}/obj/{object_type}", params=params)
        results = page.get("results", []) or []
        yield from results
        if not results or not page.get("remaining"):
            break
        cursor += len(results)


def find_lead_by_phone(phone, bubble_env="live"):
    canonical = normalize_phone(phone)
    if not canonical:
        return None
    base_url = get_bubble_base_url(bubble_env)
    exact = [{
        "key": WHATSAPP_LEAD_PHONE_FIELD,
        "constraint_type": "equals", "value": canonical,
    }]
    try:
        for lead in _bubble_records(base_url, "lead", exact):
            # Bubble has already applied an exact constraint to the canonical phone.
            # Privacy rules may omit that field from the returned object, so do not
            # reject an otherwise valid constrained match merely because it is hidden.
            if lead.get("_id"):
                hydrated = bubble(f"{base_url}/obj/lead/{lead['_id']}")
                hydrated.setdefault(WHATSAPP_LEAD_PHONE_FIELD, canonical)
                return hydrated
    except requests.RequestException as error:
        print(
            "Authenticated exact Lead phone lookup failed for "
            f"...{canonical[-4:]}: {type(error).__name__}",
            flush=True,
        )
        raise
    return None


def find_or_create_whatsapp_lead(
    phone, customer_name=None, bubble_env="live", agent_classification=None,
    owner_user_id=None,
):
    canonical = normalize_phone(phone)
    if not canonical:
        raise ValueError("WhatsApp sender phone is missing.")
    lead = find_lead_by_phone(canonical, bubble_env)
    if lead:
        return lead, False
    base_url = get_bubble_base_url(bubble_env)
    payload = {WHATSAPP_LEAD_PHONE_FIELD: canonical}
    if agent_classification in {"Yes", "No"}:
        payload["Agent?"] = agent_classification
        if str(customer_name or "").strip():
            payload[WHATSAPP_LEAD_NAME_FIELD] = str(customer_name).strip()
        if str(owner_user_id or "").strip():
            payload[WHATSAPP_LEAD_OWNER_FIELD] = str(owner_user_id).strip()
    lead_id = _bubble_create(base_url, "lead", payload)
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    stored_phone = normalize_phone(lead.get(WHATSAPP_LEAD_PHONE_FIELD))
    if stored_phone != canonical:
        print(
            "[WHATSAPP CONVERSATION] Created Lead phone is not readable from "
            "the Bubble API response; relying on the exact phone constraint for reuse",
            flush=True,
        )
    lead.setdefault("_id", lead_id)
    lead[WHATSAPP_LEAD_PHONE_FIELD] = canonical
    if agent_classification in {"Yes", "No"}:
        lead.setdefault("Agent?", agent_classification)
        if str(customer_name or "").strip():
            lead.setdefault(WHATSAPP_LEAD_NAME_FIELD, str(customer_name).strip())
        if str(owner_user_id or "").strip():
            lead.setdefault(WHATSAPP_LEAD_OWNER_FIELD, str(owner_user_id).strip())
    lead.setdefault("searchBriefJSON", "")
    return lead, True


TENANT_PROFILE_FIELDS = (
    "nationality", "adults", "children", "helpers", "bedroomsMin",
    "furnishingPreference", "occupation", "pets", "startDate",
    "budgetRent", "viewingPreference",
)
TENANT_FURNISHING_TEXT_VALUES = {
    "Fully Furnished", "Partially Furnished", "Unfurnished",
}


class TenantProfileExtractionError(RuntimeError):
    pass


def find_handoff_lead_by_phone(phone, bubble_env="live"):
    """Resolve a Lead through the durable Enquiry handoff relationship."""
    canonical = normalize_phone(phone)
    if not canonical:
        return None
    base_url = get_bubble_base_url(bubble_env)
    constraints = [{
        "key": "Enquirer Phone", "constraint_type": "equals",
        "value": canonical,
    }]
    enquiries = list(_bubble_records(base_url, "enquiry", constraints))
    lead_ids = {
        str(enquiry.get("Lead")) for enquiry in enquiries
        if enquiry.get("Lead")
        and normalize_phone(enquiry.get("Enquirer Phone", canonical)) == canonical
    }
    if len(lead_ids) != 1:
        return None
    lead_id = lead_ids.pop()
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    if not lead.get("_id"):
        lead["_id"] = lead_id
    transaction_types = {
        enquiry_transaction_type(enquiry) for enquiry in enquiries
        if str(enquiry.get("Lead") or "") == lead_id
        and enquiry_transaction_type(enquiry)
    }
    lead["_handoff_transaction_type"] = (
        transaction_types.pop() if len(transaction_types) == 1 else None
    )
    return lead


def extract_tenant_profile(message_text):
    """Extract only explicit tenant-profile facts from one inbound message."""
    schema_properties = {
        "nationality": {"type": ["string", "null"]},
        "adults": {"type": ["integer", "null"], "minimum": 0},
        "children": {"type": ["integer", "null"], "minimum": 0},
        "helpers": {"type": ["integer", "null"], "minimum": 0},
        "bedroomsMin": {"type": ["integer", "null"], "minimum": 0},
        "furnishingPreference": {
            "type": ["string", "null"],
            "enum": [
                "Fully Furnished", "Partially Furnished", "Unfurnished", None,
            ],
        },
        "occupation": {"type": ["string", "null"]},
        "pets": {"type": ["string", "null"]},
        "startDate": {
            "type": ["string", "null"],
            "description": "Exact date as YYYY-MM-DD, otherwise null.",
        },
        "budgetRent": {"type": ["number", "null"], "minimum": 0},
        "viewingPreference": {"type": ["string", "null"]},
    }
    response = client.responses.create(
        model="gpt-5-mini",
        input=(
            "Extract only tenant-profile facts explicitly and confidently supplied in "
            "the CURRENT MESSAGE. Return null for every absent or ambiguous field; "
            "absence never means zero. Split adults, children, and helpers independently. "
            "A generic family total is not a breakdown. 'No helper/children/pets' may be "
            "zero/No. Rooms required means bedroomsMin. Convert rental budget shorthand "
            "such as 12k to 12000 and 2.5m to 2500000, but never copy a property's asking rent unless stated "
            "as the sender's budget. Furnishing must be exactly Fully Furnished, Partially "
            "Furnished, or Unfurnished; ambiguous/no-preference wording is null. Preserve "
            "useful occupation, pet, and viewing detail. For startDate, return YYYY-MM-DD "
            "only for an exact, responsibly resolvable date. Month/day without a year uses "
            "the next occurrence. Approximate phrases such as mid-month, end of next month, "
            "or ASAP must be null. Today in Kuala Lumpur is "
            f"{datetime.date.today().isoformat()}.\n\nCURRENT MESSAGE:\n{message_text}"
        ),
        reasoning={"effort": "low"},
        max_output_tokens=600,
        timeout=15,
        text={"format": {
            "type": "json_schema", "name": "tenant_profile_extraction",
            "strict": True,
            "schema": {
                "type": "object", "properties": schema_properties,
                "required": list(TENANT_PROFILE_FIELDS),
                "additionalProperties": False,
            },
        }},
    )
    status = str(getattr(response, "status", "") or "").strip().lower()
    if status and status != "completed":
        raise TenantProfileExtractionError(f"response_status={status}")
    output_text = str(getattr(response, "output_text", "") or "").strip()
    if not output_text:
        raise TenantProfileExtractionError("empty_structured_output")
    try:
        extracted = json.loads(output_text)
    except (TypeError, json.JSONDecodeError) as error:
        raise TenantProfileExtractionError("invalid_structured_output") from error
    if not isinstance(extracted, dict):
        raise TenantProfileExtractionError("invalid_structured_output")
    return {field: extracted.get(field) for field in TENANT_PROFILE_FIELDS}


def _tenant_profile_patch(extracted, transaction_type=RENT_TRANSACTION):
    """Validate and serialize non-null profile values for Bubble."""
    payload = {}
    for field in TENANT_PROFILE_FIELDS:
        value = extracted.get(field)
        if value is None:
            continue
        if field in {"adults", "children", "helpers", "bedroomsMin"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                continue
        elif field == "budgetRent":
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                continue
            if transaction_type == BUY_TRANSACTION:
                field = "budgetBuy"
            elif transaction_type != RENT_TRANSACTION:
                continue
        elif field == "furnishingPreference":
            # Bubble Lead.furnishingPreference is text, restricted here to the
            # application's canonical furniture wording.
            if value not in TENANT_FURNISHING_TEXT_VALUES:
                continue
        elif field == "startDate":
            try:
                parsed = datetime.date.fromisoformat(str(value))
            except (TypeError, ValueError):
                continue
            value = f"{parsed.isoformat()}T00:00:00.000Z"
        elif not isinstance(value, str) or not value.strip():
            continue
        else:
            value = value.strip()
        payload[field] = value
    return payload


def _bubble_profile_failure_details(error, payload):
    """Return safe HTTP diagnostics without exposing submitted profile values."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    try:
        body = (
            " ".join(str(response.text or "").split())
            if response is not None else ""
        )
    except Exception:
        body = ""
    for value in payload.values():
        rendered = str(value).strip()
        if not rendered:
            continue
        body = re.sub(
            rf"(?<![\w.]){re.escape(rendered)}(?![\w.])",
            "<redacted>", body,
        )
    return status, (body[:1000] or type(error).__name__)


def capture_linked_tenant_profile(phone, message_text, bubble_env="live"):
    """Best-effort profile capture; failures must not interrupt conversation."""
    try:
        lead = find_handoff_lead_by_phone(phone, bubble_env)
    except Exception:
        return None
    if not lead or not lead.get("_id"):
        return None
    lead_id = lead["_id"]
    try:
        payload = _tenant_profile_patch(
            extract_tenant_profile(message_text),
            lead.get("_handoff_transaction_type"),
        )
        fields = list(payload)
        print(
            f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
            f"tenant_profile_extracted fields={fields}", flush=True,
        )
    except Exception as error:
        print(
            f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
            "tenant_profile_extraction_failed "
            f"error={type(error).__name__}", flush=True,
        )
        return lead
    if payload:
        base_url = get_bubble_base_url(bubble_env)
        try:
            _bubble_patch(f"{base_url}/obj/lead/{lead_id}", payload)
            lead.update(payload)
            print(
                f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                f"tenant_profile_updated fields={fields}", flush=True,
            )
        except Exception as error:
            if isinstance(error, requests.HTTPError):
                status, bubble_error = _bubble_profile_failure_details(
                    error, payload
                )
                print(
                    f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                    f"tenant_profile_update_failed status={status} "
                    f"fields={fields} bubble_error={bubble_error!r}", flush=True,
                )
            else:
                print(
                    f"[ENQUIRY WORKFLOW] lead_id={lead_id} "
                    "tenant_profile_update_failed "
                    f"error={type(error).__name__} fields={fields}", flush=True,
                )
    return lead


def _select_existing_folio(folios, lead_id):
    matches = {
        str(folio["_id"]): folio for folio in folios
        if folio.get("lead") == lead_id and folio.get("_id")
    }
    if not matches:
        return None
    ordered = sorted(
        matches.values(),
        key=lambda folio: (
            str(folio.get("Created Date") or folio.get("created_date") or ""),
            str(folio["_id"]),
        ),
    )
    if len(ordered) > 1:
        print(
            f"[WHATSAPP CONVERSATION] duplicate_folios={len(ordered)} "
            f"lead_id={lead_id} selected_folio_id={ordered[0]['_id']}",
            flush=True,
        )
    return ordered[0]


def find_or_create_lead_folio(lead_id, bubble_env="live"):
    base_url = get_bubble_base_url(bubble_env)
    constraints = [{"key": "lead", "constraint_type": "equals", "value": lead_id}]
    try:
        folios = list(_bubble_records(base_url, "folio", constraints))
    except requests.RequestException as error:
        print(
            f"[WHATSAPP CONVERSATION] exact Folio lookup unavailable; "
            f"using relationship fallback: {error}", flush=True,
        )
        folios = []
    selected = _select_existing_folio(folios, lead_id)
    if selected is None:
        selected = _select_existing_folio(
            list(_bubble_records(base_url, "folio")), lead_id
        )
    if selected is not None:
        return selected["_id"], False
    folio_id = _bubble_create(base_url, "folio", {"lead": lead_id, "folioItems": []})
    return folio_id, True


def find_latest_ai_message(lead_id, bubble_env="live"):
    """Mirror Bubble web chat's Lead-scoped previous-response lookup."""
    base_url = get_bubble_base_url(bubble_env)
    constraints = [
        {"key": "lead", "constraint_type": "equals", "value": lead_id},
    ]
    messages = list(_bubble_records(base_url, "message", constraints))
    eligible = [
        message for message in messages
        if message.get("lead") == lead_id
        and str(message.get("own_Sent?") or "").strip().casefold() != "yes"
        and str(message.get("response_ID") or "").strip()
        and message.get("_id")
    ]
    eligible.sort(
        key=lambda message: (
            str(message.get("Created Date") or message.get("created_date") or ""),
            str(message["_id"]),
        ),
        reverse=True,
    )
    previous = eligible[0] if eligible else None
    previous_response_id = (
        str(previous.get("response_ID") or "").strip() if previous else None
    )
    own_sent_values = sorted({
        str(message.get("own_Sent?") or "").strip() for message in messages
    })
    print(
        "[WHATSAPP MESSAGE HISTORY] "
        f"lead_id={lead_id} messages_fetched={len(messages)} "
        f"eligible_messages={len(eligible)} "
        f"previous_message_id={previous.get('_id') if previous else None} "
        f"previous_response_id={previous_response_id}",
        flush=True,
    )
    print(
        f"[WHATSAPP MESSAGE HISTORY] own_sent_values={own_sent_values}",
        flush=True,
    )
    return previous


def create_whatsapp_ai_message(lead_id, bubble_env="live"):
    return _bubble_create(get_bubble_base_url(bubble_env), "message", {
        "lead": lead_id,
        "own_Sent?": "No",
        "messageContent": "",
    })


def save_whatsapp_ai_message(message_id, answer, response_id, bubble_env="live"):
    response = requests.patch(
        f"{get_bubble_base_url(bubble_env)}/obj/message/{message_id}",
        headers=_bubble_headers(),
        json={"messageContent": answer, "response_ID": response_id},
        timeout=30,
    )
    response.raise_for_status()


def split_whatsapp_text(text, limit=WHATSAPP_TEXT_LIMIT):
    text = str(text or "").strip()
    if len(text) <= limit:
        return [text] if text else []
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        boundary = text.rfind("\n\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = text.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = text.rfind(" ", 0, limit + 1)
        if boundary <= 0:
            boundary = limit
        parts.append(text[:boundary].strip())
        text = text[boundary:].strip()
    return parts


def send_whatsapp_text(to_phone, text):
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    url = (
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages"
    )
    sent_ids = []
    for part in split_whatsapp_text(text):
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": normalize_phone(to_phone),
                  "type": "text", "text": {"body": part}},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        message_id = ((payload.get("messages") or [{}])[0]).get("id")
        sent_ids.append(message_id)
        safe_phone = normalize_phone(to_phone)
        print(
            f"WhatsApp sent to ...{safe_phone[-4:]} status={response.status_code} "
            f"message_id={message_id}", flush=True,
        )
    return sent_ids


def _usable_whatsapp_image_url(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("//"):
        value = f"https:{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return value


def get_listing_whatsapp_image(listing):
    """Return the Listing's own preferred public image URL, if usable."""
    cover = _usable_whatsapp_image_url(listing.get("coverPhoto"))
    if cover:
        return cover
    photos = listing.get("photos")
    if isinstance(photos, list) and photos:
        first_photo = _usable_whatsapp_image_url(photos[0])
        if first_photo:
            return first_photo
    return None


def send_whatsapp_image(to_phone, image_url):
    """Send one public image URL through the existing WhatsApp Cloud API."""
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    url = (
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": normalize_phone(to_phone),
            "type": "image",
            "image": {"link": image_url},
        },
        timeout=30,
    )
    response.raise_for_status()
    return response


def send_whatsapp_typing_indicator(whatsapp_message_id):
    """Mark one inbound Meta message read and show WhatsApp's native typing state."""
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    url = (
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": whatsapp_message_id,
            "typing_indicator": {"type": "text"},
        },
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.RequestException:
        print(
            "[WHATSAPP] Typing indicator failed "
            f"message_id={whatsapp_message_id} status={response.status_code}",
            flush=True,
        )
        raise
    print(
        f"[WHATSAPP] Typing indicator sent message_id={whatsapp_message_id}",
        flush=True,
    )


def get_plausible_listings(
    base_url, lead, condo_scope=None, target=RETRIEVAL_CANDIDATE_TARGET
):
    """Filter each Bubble page and stop once ranking has a healthy candidate pool."""
    started = time.perf_counter()
    plausible = []
    cursor = 0
    pages = 0
    fetched = 0
    condo_cache = {}
    seen_cursors = set()
    while cursor not in seen_cursors and len(plausible) < target:
        seen_cursors.add(cursor)
        page = bubble(f"{base_url}/obj/listing", params={"cursor": cursor})
        raw_results = page.get("results", []) or []
        results = raw_results
        pages += 1
        fetched += len(results)
        if condo_scope:
            results = [
                listing for listing in results
                if _listing_is_in_condo_scope(
                    listing, condo_scope, base_url, condo_cache
                )
            ]
        plausible.extend(shortlist_structured_listings(lead, results))
        if not raw_results or not page.get("remaining"):
            break
        cursor += len(raw_results)
    log_timing(
        "Load plausible listings", started,
        f" (pages={pages} fetched={fetched} plausible={len(plausible)})",
    )
    return plausible, fetched


def get_property_details(folio_id, property_reference, bubble_env):

    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    recommended_listings = []

    for folio_item_id in folio.get("folioItems", []) or []:
        try:
            folio_item = bubble(f"{base_url}/obj/folioItem/{folio_item_id}")
            listing_id = folio_item.get("listing")
            listing = bubble(f"{base_url}/obj/listing/{listing_id}")

            if listing.get("_id") and not any(
                item.get("_id") == listing["_id"]
                for item in recommended_listings
            ):
                recommended_listings.append(listing)
        except Exception as error:
            print(f"Failed to load Folio Item details: {error}", flush=True)

    if not recommended_listings:
        return "I couldn't find any current recommendations to check."

    reference = property_reference.lower().strip()
    selected_listing = None
    ordinal_positions = {
        "first": 0,
        "1st": 0,
        "second": 1,
        "2nd": 1,
        "third": 2,
        "3rd": 2,
        "number 1": 0,
        "number 2": 1,
        "number 3": 2
    }

    for ordinal, position in ordinal_positions.items():
        if ordinal in reference and position < len(recommended_listings):
            selected_listing = recommended_listings[position]
            break

    if selected_listing is None and "last" in reference:
        selected_listing = recommended_listings[-1]

    if selected_listing is None:
        matching_listings = [
            listing
            for listing in recommended_listings
            if reference and reference in json.dumps(listing, ensure_ascii=False).lower()
        ]

        if len(matching_listings) == 1:
            selected_listing = matching_listings[0]
        elif len(recommended_listings) == 1 and reference in {
            "it", "that one", "that unit", "the property you just showed me"
        }:
            selected_listing = recommended_listings[0]

    if selected_listing is None:
        candidates = [
            {"listing_id": listing["_id"], "listing": listing}
            for listing in recommended_listings
        ]
        resolver_response = client.responses.create(
            model="gpt-5-mini",
            input=(
                "Resolve the customer's property reference against only the supplied current "
                "recommended listings. Select one listing only if the reference can be matched "
                "with confidence; otherwise return matched false.\n\n"
                f"PROPERTY REFERENCE: {property_reference}\n\n"
                f"CURRENT RECOMMENDED LISTINGS: {json.dumps(candidates, ensure_ascii=False)}"
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "property_reference_resolution",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "listing_id": {"type": "string"},
                            "matched": {"type": "boolean"}
                        },
                        "required": ["listing_id", "matched"],
                        "additionalProperties": False
                    }
                }
            }
        )
        log_token_usage("Property reference resolution", resolver_response)
        resolution = json.loads(resolver_response.output_text)

        if resolution["matched"]:
            selected_listing = next(
                (
                    listing
                    for listing in recommended_listings
                    if listing["_id"] == resolution["listing_id"]
                ),
                None
            )

    if selected_listing is None:
        return (
            "I couldn't identify a single property from that reference. Please tell me "
            "the building name or which option you mean."
        )

    detail_fields = [
        ("Property", ("name", "title", "listingName", "condoName")),
        ("Property type", ("propertyType",)),
        ("Bedrooms", ("beds",)),
        ("Bathrooms", ("baths",)),
        ("Rent", ("priceRent",)),
        ("Sale price", ("priceSale",)),
        ("Size", ("Sq Ft", "size")),
        ("Furnishing", ("furnished",)),
        ("Parking", ("parking", "car parks")),
        ("Availability", ("availability",)),
        ("Balcony", ("balcony",)),
        ("Facilities", ("facilities", "amenities")),
        ("Location", ("address", "location")),
        ("Description", ("Description", "keyFacts", "AIsearchtext"))
    ]
    details = []

    for label, field_names in detail_fields:
        for field_name in field_names:
            value = selected_listing.get(field_name)

            if value not in (None, "", []):
                if isinstance(value, list):
                    value = ", ".join(str(item) for item in value)
                details.append(f"{label}: {value}")
                break

    if not details:
        return "I found the property, but Rentee does not have further details available."

    return "Authoritative Rentee property details:\n" + "\n".join(details)


def get_current_recommendations(folio_id, bubble_env, include_media=False):
    """Return grounded details for the active Folio without changing its shortlist."""
    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    listings = []
    for position, folio_item_id in enumerate(folio.get("folioItems", []) or [], start=1):
        try:
            folio_item = bubble(f"{base_url}/obj/folioItem/{folio_item_id}")
            listing_id = folio_item.get("listing")
            if not listing_id:
                continue
            listing = bubble(f"{base_url}/obj/listing/{listing_id}")
            if listing.get("_id") and not any(
                item["listing_id"] == listing["_id"] for item in listings
            ):
                facts = listing_facts(listing)
                for output_name, field_names in (
                    ("furnishing", ("Furnishing", "furnished")),
                    ("size", ("Sq Ft", "size")),
                    ("availability", ("availability", "Availability")),
                    ("balcony", ("balcony", "Balcony")),
                    ("maid_room", ("maid room", "maidRoom", "Maid room")),
                ):
                    value = next((listing.get(name) for name in field_names
                                  if listing.get(name) not in (None, "", [])), None)
                    if value is not None:
                        facts[output_name] = value
                facts["position"] = position
                facts["listing_id"] = facts.pop("_id")
                if include_media:
                    # Retain only the Listing's own media fields for WhatsApp rendering.
                    facts["coverPhoto"] = listing.get("coverPhoto")
                    facts["photos"] = listing.get("photos")
                if folio_item.get("RecoSummary"):
                    facts["recommendation_reason"] = folio_item["RecoSummary"]
                listings.append(facts)
        except Exception as error:
            print(f"Failed to load current recommendation: {error}", flush=True)
    if not listings:
        return json.dumps({"current_recommendations": []}, ensure_ascii=False)
    condo_ids = [listing.get("condo") for listing in listings if listing.get("condo")]
    condo_names = get_relationship_names(base_url, "condo", condo_ids)
    for listing in listings:
        condo_name = condo_names.get(str(listing.get("condo") or ""))
        if condo_name:
            listing["condo_name"] = condo_name
            listing.setdefault("property_name", condo_name)
    return json.dumps({"current_recommendations": listings}, ensure_ascii=False)


def build_folio_url(folio_id):
    return f"https://www.rentee.asia/folio3/{folio_id}"


def _whatsapp_recommendation_sections(listings, folio_id, top_count=3):
    shown = listings[:max(1, min(top_count, MAX_WHATSAPP_RECOMMENDATION_IMAGES))]
    total = len(listings)
    intro = (
        f"I've shortlisted {total} properties for you. "
        f"My top {len(shown)} are:"
    )
    cards = []
    for index, listing in enumerate(shown, start=1):
        name = listing.get("property_name") or listing.get("condo_name") or f"Property {index}"
        rent = _as_number(listing.get("priceRent"))
        sale = _as_number(listing.get("priceSale"))
        price_label = f"RM{rent:,.0f}/month" if rent is not None else None
        if price_label is None and sale is not None:
            price_label = f"RM{sale:,.0f}"
        beds = _as_number(listing.get("beds"))
        bed_label = f"{beds:g} bed" if beds is not None else None
        details = ", ".join(value for value in (price_label, bed_label) if value)
        heading = f"{index}. {name}" + (f" — {details}" if details else "")
        reason = str(listing.get("recommendation_reason") or "").strip()
        for internal_field in ("listing_id", "condo", "Geo"):
            internal_value = listing.get(internal_field)
            if internal_value:
                reason = reason.replace(str(internal_value), str(name))
        cards.append(heading + (f"\n{reason}" if reason else ""))
    if total > len(shown):
        link_intro = f"See all {total} with photos and full details here:"
    else:
        link_intro = "See the shortlist with photos and full details here:"
    footer = (
        f"{link_intro}\n{build_folio_url(folio_id)}\n\n"
        "Tell me which ones you like."
    )
    return intro, cards, footer, shown


def build_whatsapp_recommendation_summary(
    folio_id, bubble_env="live", top_count=3, listings=None
):
    """Render a compact, grounded subset while leaving the Folio as the full UI."""
    if listings is None:
        result = json.loads(get_current_recommendations(folio_id, bubble_env))
        listings = result.get("current_recommendations") or []
    if not listings:
        return None
    intro, cards, footer, _shown = _whatsapp_recommendation_sections(
        listings, folio_id, top_count
    )
    return "\n\n".join([intro, *cards, footer])


def send_whatsapp_recommendation_batch(to_phone, folio_id, listings, top_count=3):
    """Interleave each ranked Listing's own image and grounded recommendation text."""
    intro, cards, footer, shown = _whatsapp_recommendation_sections(
        listings, folio_id, top_count
    )
    send_whatsapp_text(to_phone, intro)
    seen_listing_ids = set()
    for listing, card in zip(shown, cards):
        listing_id = str(listing.get("listing_id") or "")
        image_url = get_listing_whatsapp_image(listing)
        source = (
            "coverPhoto"
            if image_url == _usable_whatsapp_image_url(listing.get("coverPhoto"))
            else "photos[0]"
        )
        duplicate = bool(listing_id and listing_id in seen_listing_ids)
        if listing_id:
            seen_listing_ids.add(listing_id)
        if image_url and not duplicate:
            print(
                f"[WHATSAPP MEDIA] listing_id={listing_id or 'unknown'} "
                f"source={source} sending",
                flush=True,
            )
            try:
                response = send_whatsapp_image(to_phone, image_url)
                print(
                    f"[WHATSAPP MEDIA] listing_id={listing_id or 'unknown'} "
                    f"sent status={response.status_code}",
                    flush=True,
                )
            except Exception as error:
                status = getattr(getattr(error, "response", None), "status_code", None)
                print(
                    f"[WHATSAPP MEDIA] listing_id={listing_id or 'unknown'} "
                    f"failed status={status or 'unknown'} continuing with text",
                    flush=True,
                )
        elif not image_url:
            print(
                f"[WHATSAPP MEDIA] listing_id={listing_id or 'unknown'} "
                "no image available",
                flush=True,
            )
        send_whatsapp_text(to_phone, card)
    send_whatsapp_text(to_phone, footer)


def create_folio_items(recommendations, base_url, message_id):

    create_started = time.perf_counter()
    folio_item_ids = []

    for position, recommendation in enumerate(recommendations, start=1):
        listing_id = recommendation["listing_id"]
        reco_summary = recommendation["reco_summary"]
        try:
            folio_item_started = time.perf_counter()
            response = requests.post(
                f"{base_url}/obj/folioItem",
                headers={
                    "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={
                    "listing": listing_id,
                    "newlyAdded": True,
                    "RecoSummary": reco_summary,
                    "message": message_id
                },
                timeout=30
            )
            response.raise_for_status()
            log_timing(f"Create FolioItem {position}", folio_item_started)
            data = response.json()
            folio_item_id = data.get("id")

            if not folio_item_id:
                raise ValueError("Bubble did not return a Folio Item ID.")

            folio_item_ids.append(folio_item_id)
        except Exception as error:
            print(f"Failed to create Folio Item: {error}", flush=True)
            return None

    log_timing(
        "Create all FolioItems",
        create_started,
        f" ({len(folio_item_ids)} items)"
    )
    return folio_item_ids


def clear_folio_item_newly_added(folio_item_id, base_url):

    clear_started = time.perf_counter()
    response = requests.patch(
        f"{base_url}/obj/folioItem/{folio_item_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"newlyAdded": False},
        timeout=30
    )
    response.raise_for_status()
    log_timing("Clear FolioItem newlyAdded", clear_started)


def update_folio_items(folio_id, folio_item_ids, base_url):

    patch_started = time.perf_counter()
    response = requests.patch(
        f"{base_url}/obj/folio/{folio_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "folioItems": folio_item_ids,
            "newRecommendations": True
        },
        timeout=30
    )
    response.raise_for_status()
    log_timing("Patch Folio", patch_started)


def _listing_is_in_condo_scope(listing, condo_scope, base_url, condo_cache):
    wanted = {normalize_condo_name(name) for name in condo_scope}
    candidates = []
    for field in ("condoName", "building", "development", "name", "title"):
        if listing.get(field):
            candidates.append(str(listing[field]))
    condo_reference = listing.get("condo")
    if condo_reference:
        if condo_reference not in condo_cache:
            try:
                condo_cache[condo_reference] = bubble(
                    f"{base_url}/obj/condo/{condo_reference}"
                )
            except Exception as error:
                print(f"Could not resolve listing condo reference: {error}", flush=True)
                condo_cache[condo_reference] = {}
        condo = condo_cache[condo_reference]
        for field in ("name", "Name", "Condo name", "condoName"):
            if condo.get(field):
                candidates.append(str(condo[field]))
    return any(normalize_condo_name(value) in wanted for value in candidates)


def _as_number(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    clean = "".join(character for character in str(value or "") if character.isdigit() or character == ".")
    try:
        return float(clean) if clean else None
    except ValueError:
        return None


def structured_lead_requirements(lead):
    """Return only factual Bubble Lead fields used for listing retrieval and ranking."""
    return {
        "transaction_type": lead.get("TransactionType") or [],
        "bedrooms_min": _as_number(lead.get("bedroomsMin")),
        "geo_ids": list(lead.get("Geo") or []),
        "preferred_condo_ids": list(lead.get("preferredCondos") or []),
        "budget_rent": _as_number(lead.get("budgetRent")),
        "budget_buy": _as_number(lead.get("budgetBuy")),
    }


def get_relationship_names(base_url, object_type, relationship_ids):
    """Bulk-resolve Bubble relationship IDs to customer-facing record names."""
    wanted = {str(value) for value in relationship_ids or [] if value}
    names = {}
    cursor = 0
    while wanted:
        try:
            page = bubble(f"{base_url}/obj/{object_type}", params={"cursor": cursor})
        except requests.RequestException as error:
            print(f"Could not resolve {object_type} display names: {error}", flush=True)
            break
        results = page.get("results", []) or []
        for record in results:
            record_id = str(record.get("_id") or "")
            if record_id not in wanted:
                continue
            name = next(
                (
                    record.get(field) for field in
                    ("name", "Name", "Condo name", "title")
                    if record.get(field)
                ),
                None,
            )
            if name:
                names[record_id] = str(name)
            wanted.remove(record_id)
        if not results or not page.get("remaining"):
            break
        cursor += len(results)
    return names


def listing_facts(listing, condo_names=None, geo_names=None):
    """Compact grounded listing context; excludes generated listing-search prose."""
    fields = (
        "_id", "name", "title", "beds", "baths", "priceRent", "priceSale",
        "propertyType", "condo", "Geo", "Furnishing", "furnished",
        "availability", "balcony", "family room", "maid room", "outdoor area",
        "Landed_sqft", "Sq Ft", "keyFacts", "Description", "Notes",
    )
    facts = {
        field: listing[field]
        for field in fields
        if listing.get(field) not in (None, "", [])
    }
    condo_name = (condo_names or {}).get(str(listing.get("condo") or ""))
    listing_name = next(
        (listing.get(field) for field in ("name", "title", "condoName") if listing.get(field)),
        None,
    )
    if condo_name:
        facts["condo_name"] = condo_name
    if listing_name or condo_name:
        facts["property_name"] = str(listing_name or condo_name)
    geo_value = listing.get("Geo")
    geo_ids = geo_value if isinstance(geo_value, list) else [geo_value]
    resolved_geos = [
        (geo_names or {}).get(str(geo_id)) for geo_id in geo_ids if geo_id
    ]
    resolved_geos = [name for name in resolved_geos if name]
    if resolved_geos:
        facts["geo_names"] = resolved_geos
    return facts


def ranking_listing_facts(listing, condo_names=None, geo_names=None):
    """Expose one canonical Bubble Listing identifier to the ranking model."""
    facts = listing_facts(listing, condo_names, geo_names)
    listing_id = facts.pop("_id", None)
    if listing_id:
        facts["listing_id"] = str(listing_id)
    return facts


def _transaction_modes(value):
    values = value if isinstance(value, list) else [value]
    rendered = " ".join(str(item).casefold() for item in values)
    modes = set()
    if "rent" in rendered or "let" in rendered:
        modes.add("rent")
    if "buy" in rendered or "sale" in rendered or "purchase" in rendered:
        modes.add("buy")
    return modes


def shortlist_structured_listings(lead, listings):
    """Remove obvious mismatches while leaving trade-off judgement to the model."""
    requirements = structured_lead_requirements(lead)
    modes = _transaction_modes(requirements["transaction_type"])
    bedrooms_min = requirements["bedrooms_min"]
    geo_ids = {str(item) for item in requirements["geo_ids"]}
    condo_ids = {str(item) for item in requirements["preferred_condo_ids"]}
    budget_rent = requirements["budget_rent"]
    budget_buy = requirements["budget_buy"]
    shortlisted = []

    for listing in listings:
        beds = _as_number(listing.get("beds"))
        if bedrooms_min is not None and beds is not None and beds < bedrooms_min:
            continue
        if condo_ids and str(listing.get("condo")) not in condo_ids:
            continue
        listing_geos = listing.get("Geo") or []
        if not isinstance(listing_geos, list):
            listing_geos = [listing_geos]
        if geo_ids and not condo_ids and not geo_ids.intersection(str(item) for item in listing_geos):
            continue

        rent = _as_number(listing.get("priceRent"))
        sale = _as_number(listing.get("priceSale"))
        if modes == {"rent"} and rent is None:
            continue
        if modes == {"buy"} and sale is None:
            continue
        if budget_rent and "buy" not in modes and rent:
            if rent > budget_rent * 1.2 or rent < budget_rent * 0.45:
                continue
        if budget_buy and "rent" not in modes and sale:
            if sale > budget_buy * 1.2 or sale < budget_buy * 0.45:
                continue
        shortlisted.append(listing)
    return shortlisted


def reduce_listing_candidates(lead, listings, limit=RANKING_CANDIDATE_LIMIT):
    """Bound ranking context while preserving the strongest factual candidates."""
    if len(listings) <= limit:
        return list(listings)
    requirements = structured_lead_requirements(lead)
    modes = _transaction_modes(requirements["transaction_type"])
    target_budget = (
        requirements["budget_buy"] if modes == {"buy"}
        else requirements["budget_rent"]
    )
    bedrooms_min = requirements["bedrooms_min"]

    def candidate_key(listing):
        price = _as_number(
            listing.get("priceSale") if modes == {"buy"} else listing.get("priceRent")
        )
        beds = _as_number(listing.get("beds"))
        budget_distance = (
            abs(price - target_budget) / target_budget
            if price is not None and target_budget else 0
        )
        bedroom_distance = (
            abs(beds - bedrooms_min)
            if beds is not None and bedrooms_min is not None else 0
        )
        missing_facts = sum(
            listing.get(field) in (None, "", [])
            for field in ("beds", "priceRent" if modes != {"buy"} else "priceSale", "condo")
        )
        return budget_distance, bedroom_distance, missing_facts

    return sorted(listings, key=candidate_key)[:limit]


def match_lead(folio_id, bubble_env, message_id, condo_scope=None):

    match_started = time.perf_counter()
    yield "Checking your preferences..."
    base_url = get_bubble_base_url(bubble_env)
    folio_started = time.perf_counter()
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    log_timing("match_lead - Folio lookup", folio_started)
    existing_folio_item_ids = list(folio.get("folioItems", []) or [])
    existing_listing_ids = set()
    previously_new_folio_item_ids = []

    existing_items_started = time.perf_counter()
    for existing_folio_item_id in existing_folio_item_ids:
        existing_folio_item = bubble(
            f"{base_url}/obj/folioItem/{existing_folio_item_id}"
        )
        existing_listing_id = existing_folio_item.get("listing")

        if existing_listing_id:
            existing_listing_ids.add(existing_listing_id)

        if existing_folio_item.get("newlyAdded") is True:
            previously_new_folio_item_ids.append(existing_folio_item_id)
    log_timing(
        "match_lead - load existing FolioItems",
        existing_items_started,
        f" ({len(existing_folio_item_ids)} items)"
    )

    lead_id = folio["lead"]
    print(f"Folio {folio_id} -> Lead {lead_id}", flush=True)
    lead_started = time.perf_counter()
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    log_timing("match_lead - Lead lookup", lead_started)
    search_lead = lead_with_active_search_filters(lead, base_url)

    yield "Searching available properties..."
    listings_started = time.perf_counter()
    listings, fetched_listing_count = get_plausible_listings(
        base_url, search_lead, condo_scope
    )
    if condo_scope:
        print(
            "Constrained listing search to recommended condos: "
            + ", ".join(condo_scope),
            flush=True,
        )
    scoped_listing_count = len(listings)
    structured_listing_count = len(listings)
    listings = reduce_listing_candidates(search_lead, listings)
    print(
        "Listing candidates: "
        f"fetched={fetched_listing_count} "
        f"after_condo_scope={scoped_listing_count} "
        f"after_structured_filters={structured_listing_count} "
        f"sent_to_ranking={len(listings)}",
        flush=True,
    )
    log_timing("match_lead - load listings", listings_started)

    if not listings:
        log_timing("match_lead TOTAL", match_started)
        return (
            "I don't have a suitable current property match for that search at the moment. "
            "We can broaden the area, budget, bedrooms, or preferred condos if you'd like."
        )

    print(
        f"Ranking {len(listings)} candidates (limit: {RANKING_CANDIDATE_LIMIT})",
        flush=True
    )

    prompt_started = time.perf_counter()
    condo_names = get_relationship_names(
        base_url, "condo", [listing.get("condo") for listing in listings]
    )
    geo_relationship_ids = []
    for listing in listings:
        value = listing.get("Geo")
        geo_relationship_ids.extend(value if isinstance(value, list) else [value])
    geo_names = get_relationship_names(base_url, "geo", geo_relationship_ids)
    grounded_listing_facts = [
        ranking_listing_facts(listing, condo_names, geo_names)
        for listing in listings
        if listing.get("_id")
    ]
    matching_input = {
        "customer_requirements": structured_lead_requirements(search_lead),
        "available_listings": grounded_listing_facts,
    }
    prompt = (
        "Rank the grounded listings for this customer. Use the structured facts and "
        "make sensible trade-offs, including fit with the customer's budget tier. "
        "Return at most the 7 strongest genuine fits rather than exhaustively describing "
        "every plausible listing. Keep each reco_summary concise and mention material "
        "compromises. Never invent "
        "facts. Copy each selected listing_id exactly from available_listings; do not alter, "
        "shorten, or invent it. Use property_name or condo_name in customer-facing prose and "
        "never expose listing_id or other internal IDs. Briefly explain why each option fits. Return JSON "
        "matching the supplied schema.\n\n"
        + json.dumps(matching_input, ensure_ascii=False)
    )
    print(
        f"Ranking input size: chars={len(prompt)} "
        f"approx_tokens={max(1, len(prompt) // 4)}",
        flush=True,
    )
    log_timing("match_lead - build matching input", prompt_started)

    yield "Ranking the best matches..."
    matching_started = time.perf_counter()
    response = client.responses.create(

        model="gpt-5-mini",

        input=prompt,
        reasoning={"effort": "low"},
        max_output_tokens=RANKING_MAX_OUTPUT_TOKENS,
        text={
            "format": {
                "type": "json_schema",
                "name": "listing_recommendations",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "recommendations": {
                            "type": "array",
                            "maxItems": 7,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "listing_id": {"type": "string"},
                                    "reco_summary": {"type": "string", "maxLength": 240}
                                },
                                "required": ["listing_id", "reco_summary"],
                                "additionalProperties": False
                            }
                        },
                        "customer_response": {"type": "string"}
                    },
                    "required": ["recommendations", "customer_response"],
                    "additionalProperties": False
                }
            }
        }

    )
    log_timing("match_lead - OpenAI matching", matching_started)
    log_token_usage("Matching", response)

    print("Matching model response received", flush=True)
    parse_started = time.perf_counter()
    try:
        result = json.loads(response.output_text)
    except json.JSONDecodeError as error:
        print(f"Failed to parse matching JSON: {error}", flush=True)
        log_timing("match_lead TOTAL", match_started)
        return "I’m sorry, I couldn’t prepare your recommendations just now."

    available_listing_ids = {
        facts["listing_id"] for facts in grounded_listing_facts
    }
    returned_listing_ids = [
        str(recommendation.get("listing_id"))
        for recommendation in result["recommendations"]
    ]
    print(
        "[RANK DEBUG] candidate_id_field=listing_id "
        f"valid_candidate_ids={sorted(available_listing_ids)}",
        flush=True,
    )
    print(
        f"[RANK DEBUG] returned_listing_ids={returned_listing_ids}",
        flush=True,
    )
    validated_recommendations = []
    seen_recommended_listing_ids = set()

    for recommendation in result["recommendations"]:
        listing_id = str(recommendation["listing_id"])
        if listing_id not in available_listing_ids:
            print(
                f"Ignoring invalid recommended listing ID: {listing_id!r}",
                flush=True,
            )
        elif listing_id not in seen_recommended_listing_ids:
            recommendation["listing_id"] = listing_id
            validated_recommendations.append(recommendation)
            seen_recommended_listing_ids.add(listing_id)

    if result["recommendations"] and not validated_recommendations:
        fallback_facts = grounded_listing_facts[:3]
        validated_recommendations = [
            {
                "listing_id": facts["listing_id"],
                "reco_summary": "Matches the current structured property search filters.",
            }
            for facts in fallback_facts
        ]
        print(
            "[RANK DEBUG] all returned IDs were invalid; using structured-candidate "
            f"fallback listing_ids={[item['listing_id'] for item in validated_recommendations]}",
            flush=True,
        )
        result["customer_response"] = (
            "I found a few current listings that match your active search filters. "
            "Here are the strongest available candidates to review."
        )

    new_recommendations = [
        recommendation
        for recommendation in validated_recommendations
        if recommendation["listing_id"] not in existing_listing_ids
    ]
    log_timing("match_lead - parse/validate", parse_started)

    if not validated_recommendations:
        log_timing("match_lead TOTAL", match_started)
        return (
            "I don't have a suitable current property match for that search at the moment. "
            "We can broaden the area, budget, bedrooms, or preferred condos if you'd like."
        )

    display_names_by_id = {
        str(facts.get("listing_id")): facts.get("property_name") or facts.get("condo_name")
        for facts in grounded_listing_facts
        if facts.get("listing_id")
    }
    customer_response = result["customer_response"]
    for listing_id, display_name in display_names_by_id.items():
        customer_response = customer_response.replace(
            listing_id, str(display_name or "the property")
        )

    yield "Updating your shortlist..."

    recommendations_available = not new_recommendations
    if new_recommendations:
        folio_items_update_started = time.perf_counter()
        clear_started = time.perf_counter()
        try:
            for previous_folio_item_id in previously_new_folio_item_ids:
                clear_folio_item_newly_added(previous_folio_item_id, base_url)
        except Exception as error:
            print(f"Failed to clear previous Folio Item flags: {error}", flush=True)
            log_timing("Clear previous newlyAdded flags", clear_started)
            log_timing("match_lead TOTAL", match_started)
            return customer_response
        log_timing("Clear previous newlyAdded flags", clear_started)

        new_folio_item_ids = create_folio_items(
            new_recommendations, 
            base_url,
            message_id
        )

        if new_folio_item_ids:
            final_folio_item_ids = existing_folio_item_ids + new_folio_item_ids
            try:
                update_folio_items(folio_id, final_folio_item_ids, base_url)
                recommendations_available = True
            except Exception as error:
                print(f"Failed to update Folio Items: {error}", flush=True)
        log_timing("match_lead - update FolioItems", folio_items_update_started)

    log_timing("match_lead TOTAL", match_started)
    return MatchingResult(customer_response, recommendations_available)


def stream_match_lead(folio_id, bubble_env, message_id, condo_scope=None):

    match_flow = match_lead(folio_id, bubble_env, message_id, condo_scope)

    while True:
        try:
            status = next(match_flow)
        except StopIteration as completed:
            return completed.value

        yield f"data: {json.dumps({'status': status})}\n\n"


def execute_match_lead_silently(folio_id, bubble_env, message_id, condo_scope=None):
    """Run matching while keeping backend progress events out of customer SSE."""
    match_flow = match_lead(folio_id, bubble_env, message_id, condo_scope)
    while True:
        try:
            status = next(match_flow)
        except StopIteration as completed:
            return completed.value
        print(f"match_lead progress: {status}", flush=True)


def get_named_object_ids(base_url, object_type, names):
    """Resolve user-facing Geo/Condo names to existing Bubble relationship IDs."""
    wanted = {normalize_condo_name(name) for name in names or [] if str(name).strip()}
    if not wanted:
        return []
    matches = []
    cursor = 0
    while wanted:
        try:
            page = bubble(f"{base_url}/obj/{object_type}", params={"cursor": cursor})
        except requests.RequestException as error:
            print(
                f"Bubble {object_type} relationship lookup unavailable; "
                f"leaving the existing Lead relationship unchanged: {error}",
                flush=True,
            )
            return matches
        results = page.get("results", []) or []
        for record in results:
            candidate = next(
                (record.get(field) for field in ("name", "Name", "Condo name") if record.get(field)),
                None,
            )
            normalized = normalize_condo_name(candidate)
            if normalized in wanted and record.get("_id"):
                matches.append(record["_id"])
                wanted.remove(normalized)
        if not results or not page.get("remaining"):
            break
        cursor += len(results)
    return matches


def structured_lead_update(update, base_url):
    """Translate model-extracted values into Bubble's real structured Lead fields."""
    payload = {}
    transaction = update.get("transaction_type")
    if transaction and transaction != "unchanged":
        # Rent/Let is the existing Bubble option-set value used by this application.
        values = []
        if transaction in ("rent", "both"):
            values.append("Rent/Let")
        if transaction in ("buy", "both"):
            values.append("Sale/Purchase")
        payload["TransactionType"] = values
    if update.get("bedrooms_min") is not None:
        payload["bedroomsMin"] = update["bedrooms_min"]
    if update.get("budget_rent") is not None:
        payload["budgetRent"] = update["budget_rent"]
    if update.get("budget_buy") is not None:
        payload["budgetBuy"] = update["budget_buy"]
    if update.get("geo_names"):
        geo_ids = get_named_object_ids(base_url, "geo", update["geo_names"])
        if geo_ids:
            payload["Geo"] = geo_ids
    if update.get("preferred_condo_names"):
        condo_ids = get_named_object_ids(
            base_url, "condo", update["preferred_condo_names"]
        )
        if condo_ids:
            payload["preferredCondos"] = condo_ids
    return payload


def save_search_state(
    lead_id, search_state, base_url, lead_fields=None, active_search_state=None
):
    save_started = time.perf_counter()
    payload = {
        "searchBriefJSON": dump_search_state(search_state),
        "AIsearchtext": search_state_to_requirements_text(search_state),
        "AIsearchsummary": search_state_to_summary(search_state),
    }
    if active_search_state is not None:
        payload["searchActive"] = dump_search_state(active_search_state)
    payload.update(lead_fields or {})
    response = requests.patch(
        f"{base_url}/obj/lead/{lead_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    try:
        response.raise_for_status()
    except requests.RequestException:
        print(
            "Failed to save structured search brief: "
            f"HTTP {response.status_code} body={response.text}",
            flush=True,
        )
        raise
    finally:
        log_timing("Save structured search brief", save_started)


def search_state_to_requirements_text(search_state):
    state = load_search_state(search_state)
    fields = (
        ("Areas", state["areas"]),
        ("Regular destinations", state["regular_destinations"]),
        ("Property types", state["property_types"]),
        ("Bedrooms", state["bedroom_requirement"]),
        ("Budget", state["budget_requirement"]),
        ("Other requirements", state["other_requirements"]),
        ("Ordered priorities", state["priorities"]),
    )
    return "\n".join(
        f"{label}: {', '.join(value) if isinstance(value, list) else value}"
        for label, value in fields
        if value
    )


def search_state_to_summary(search_state):
    state = load_search_state(search_state)
    fields = (
        ("Area", state["areas"], ", "),
        ("Regular destinations", state["regular_destinations"], ", "),
        ("Property type", state["property_types"], ", "),
        ("Bedrooms", state["bedroom_requirement"], None),
        ("Budget", state["budget_requirement"], None),
        ("Other", state["other_requirements"], ", "),
        ("Priorities", state["priorities"], " → "),
    )
    lines = []
    for label, value, separator in fields:
        if not value:
            continue
        rendered = separator.join(value) if isinstance(value, list) else value
        lines.append(f"{label}: {rendered}")
    return "\n".join(lines)


def recommend_areas_for_search(search_state):
    """Recommend areas from Rentee's condo knowledge, never current inventory."""
    condo_rows = list(_get_condo_lookup().values())
    response = client.responses.create(
        model="gpt-5-mini",
        input=(
            "Recommend 2 to 4 Kuala Lumpur areas for this home seeker using only the "
            "SEARCH BRIEF and Rentee CONDO KNOWLEDGE below. Prioritise practical access "
            "to the recorded regular destinations and consider any other collected "
            "requirements. Give a concise customer-friendly trade-off for each area. Do "
            "not search current listings, claim availability, invent commute times, or "
            "recommend individual units. Return distinct area names, not condo names.\n\n"
            f"SEARCH BRIEF:\n{dump_search_state(search_state)}\n\n"
            f"CONDO KNOWLEDGE:\n{json.dumps(condo_rows, ensure_ascii=False)}"
        ),
        text={"format": {
            "type": "json_schema",
            "name": "area_recommendations",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "recommendations": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "area_name": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["area_name", "reason"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["recommendations"],
                "additionalProperties": False,
            },
        }},
    )
    recommendations = json.loads(response.output_text)["recommendations"]
    state_with_recommendations = set_area_recommendations(
        search_state, recommendations
    )
    if not state_with_recommendations["area_recommendations"]:
        raise ValueError("No valid area recommendations were returned.")
    return state_with_recommendations["area_recommendations"]


def area_recommendation_text(search_state):
    state = load_search_state(search_state)
    destinations = ", ".join(state["regular_destinations"])
    lines = [
        f"Based on the places you need to reach regularly — {destinations} — "
        "I'd consider:",
        "",
    ]
    lines.extend(
        f"{index}. {item['area_name']} — {item['reason']}"
        for index, item in enumerate(state["area_recommendations"], 1)
    )
    lines.extend([
        "",
        "These are good starting points; your reaction to them will help me refine the search.",
    ])
    return "\n".join(lines)


def load_active_search_state(lead):
    """Load authoritative current filters, falling back once for existing Leads."""
    raw = lead.get("searchActive")
    valid = False
    if isinstance(raw, dict):
        valid = bool(raw)
    elif isinstance(raw, str) and raw.strip():
        try:
            valid = isinstance(json.loads(raw), dict)
        except (TypeError, ValueError):
            print("[SEARCH ACTIVE] invalid JSON; using safe fallback", flush=True)
    if valid:
        state = load_search_state(raw)
        print(
            f"[SEARCH ACTIVE] loaded areas={state['areas']} "
            f"beds={state['bedroom_requirement'] or None} "
            f"budget={state['budget_requirement'] or None}",
            flush=True,
        )
        return state
    fallback = load_search_state(lead.get("searchBriefJSON"))
    print("[SEARCH ACTIVE] missing; initialized from current search brief", flush=True)
    return fallback


def _unique_search_values(values):
    result = []
    seen = set()
    for value in values or []:
        clean = " ".join(str(value or "").split())
        key = clean.casefold()
        if clean and key not in seen:
            result.append(clean)
            seen.add(key)
    return result


def _modify_active_values(existing, values, mode, default_mode="replace"):
    existing = _unique_search_values(existing)
    values = _unique_search_values(values)
    mode = mode if mode in {"unchanged", "replace", "add", "remove", "reset"} else default_mode
    if mode == "unchanged":
        return existing
    if mode == "reset":
        return []
    if mode == "replace":
        return values
    if mode == "add":
        return _unique_search_values(existing + values)
    removed = {value.casefold() for value in values}
    return [value for value in existing if value.casefold() not in removed]


def apply_active_search_update(active_state, update):
    """Apply this turn only to active filters, preserving all unchanged criteria."""
    state = load_search_state(
        empty_search_state() if update.get("new_search") else active_state
    )
    previous_areas = list(state["areas"])
    geo_names = update.get("geo_names") or []
    area_mode = update.get("area_update_mode")
    if geo_names or area_mode == "reset":
        state["areas"] = _modify_active_values(
            state["areas"], geo_names, area_mode, "replace"
        )
        state["area_status"] = "known" if state["areas"] else "unknown"
        state["area_recommendations"] = []
        if state["areas"] != previous_areas and area_mode in (None, "replace", "reset"):
            # A replaced area must not inherit condo restrictions from the old area.
            state["recommended_condos"] = []
            state["selected_condos"] = []

    scalar_update = dict(update)
    scalar_update.pop("geo_names", None)
    scalar_update.pop("areas", None)
    scalar_update["area_status"] = "unchanged"
    if scalar_update.get("bedrooms_min") is not None:
        scalar_update["bedroom_requirement"] = str(scalar_update["bedrooms_min"])
    relevant_budget = scalar_update.get("budget_rent") or scalar_update.get("budget_buy")
    if relevant_budget is not None:
        scalar_update["budget_requirement"] = str(relevant_budget)
    transaction = scalar_update.get("transaction_type")
    if transaction and transaction != "unchanged":
        scalar_update["property_types"] = [transaction]
    preserved_recommended = list(state["recommended_condos"])
    preserved_selected = list(state["selected_condos"])
    state = apply_search_update(state, scalar_update)

    condo_names = update.get("preferred_condo_names") or []
    condo_mode = update.get("condo_update_mode")
    if not condo_names and condo_mode not in {"replace", "add", "remove", "reset"}:
        state["recommended_condos"] = preserved_recommended
        state["selected_condos"] = preserved_selected
    if condo_names or condo_mode == "reset":
        selected = _modify_active_values(
            state["selected_condos"], condo_names, condo_mode, "replace"
        )
        state["recommended_condos"] = list(selected)
        state["selected_condos"] = list(selected)
    if state["areas"] != previous_areas:
        print(
            f"[SEARCH ACTIVE] areas {previous_areas} -> {state['areas']}", flush=True
        )
    return state


def apply_cumulative_search_update(cumulative_state, update):
    """Retain historical search knowledge while accepting this turn's new facts."""
    cumulative_update = dict(update)
    new_areas = _unique_search_values(update.get("geo_names") or [])
    if new_areas:
        cumulative_update["area_status"] = "known"
        cumulative_update["areas"] = _unique_search_values(
            load_search_state(cumulative_state)["areas"] + new_areas
        )
    if cumulative_update.get("bedrooms_min") is not None:
        cumulative_update["bedroom_requirement"] = str(cumulative_update["bedrooms_min"])
    relevant_budget = cumulative_update.get("budget_rent") or cumulative_update.get("budget_buy")
    if relevant_budget is not None:
        cumulative_update["budget_requirement"] = str(relevant_budget)
    transaction = cumulative_update.get("transaction_type")
    if transaction and transaction != "unchanged":
        cumulative_update["property_types"] = [transaction]
    state = apply_search_update(cumulative_state, cumulative_update)
    preferred = _unique_search_values(update.get("preferred_condo_names") or [])
    if preferred:
        state["recommended_condos"] = _unique_search_values(
            state["recommended_condos"] + preferred
        )
    return state


def lead_with_active_search_filters(lead, base_url):
    """Build an isolated Lead-shaped filter view from searchActive only."""
    state = load_active_search_state(lead)
    has_state_filters = bool(
        state["areas"] or state["selected_condos"]
        or state["bedroom_requirement"] or state["budget_requirement"]
        or state["property_types"]
    )
    if not has_state_filters:
        print(
            "[SEARCH ACTIVE] no usable saved state; using existing Lead filters safely",
            flush=True,
        )
        return dict(lead)
    active = dict(lead)
    active["Geo"] = get_named_object_ids(base_url, "geo", state["areas"])
    active["preferredCondos"] = get_named_object_ids(
        base_url, "condo", state["selected_condos"]
    )
    active["bedroomsMin"] = _as_number(state["bedroom_requirement"])
    transaction = " ".join(state["property_types"]).casefold()
    active["TransactionType"] = (
        ["Rent/Let"] if "rent" in transaction else
        ["Sale/Purchase"] if "buy" in transaction else []
    )
    budget = _as_number(state["budget_requirement"])
    active["budgetRent"] = budget if "rent" in transaction else None
    active["budgetBuy"] = budget if "buy" in transaction else None
    print("[SEARCH ACTIVE] using active filters for recommendation", flush=True)
    return active


def advance_property_search(folio_id, bubble_env, update):
    """Persist search facts and execute the useful action chosen for this turn."""
    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    lead_id = folio["lead"]
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    cumulative_state = apply_cumulative_search_update(
        lead.get("searchBriefJSON"), update
    )
    active_state = apply_active_search_update(
        load_active_search_state(lead), update
    )
    preferred_names = [
        str(name).strip() for name in update.get("preferred_condo_names", [])
        if str(name).strip()
    ]
    if preferred_names:
        cumulative_state = set_recommended_condos(cumulative_state, _unique_search_values(
            cumulative_state["recommended_condos"] + preferred_names
        ))
    lead_fields = structured_lead_update(update, base_url)
    for field in ("Geo", "preferredCondos"):
        if field in lead_fields:
            lead_fields[field] = list(dict.fromkeys(
                list(lead.get(field) or []) + list(lead_fields[field] or [])
            ))

    scope = listing_search_scope(
        active_state,
        selected_condos=preferred_names or None,
        use_full_shortlist=bool(
            update.get("use_full_shortlist") or update.get("search_listings")
        ),
    )
    if update.get("search_listings") and (scope or not active_state["recommended_condos"]):
        active_state["selected_condos"] = list(scope or [])
        save_search_state(
            lead_id, cumulative_state, base_url, lead_fields, active_state
        )
        return {
            "action": "search_listings", "scope": scope or None,
            "state": cumulative_state, "active_state": active_state,
            "lead_id": lead_id,
        }

    if update.get("recommend_areas") and active_state["regular_destinations"]:
        if not active_state["area_recommendations"]:
            recommendations = recommend_areas_for_search(active_state)
            active_state = set_area_recommendations(active_state, recommendations)
        save_search_state(
            lead_id, cumulative_state, base_url, lead_fields, active_state
        )
        return {
            "action": "recommend_areas",
            "text": area_recommendation_text(active_state),
            "state": cumulative_state, "active_state": active_state,
            "lead_id": lead_id,
            "recommendations": active_state["area_recommendations"],
        }

    if update.get("recommend_condos"):
        recommendations, response_text = recommend_condos_for_search(active_state)
        active_state = set_recommended_condos(
            active_state, [item["condo_name"] for item in recommendations]
        )
        save_search_state(
            lead_id, cumulative_state, base_url, lead_fields, active_state
        )
        return {
            "action": "condo_shortlist",
            "text": response_text.rstrip() + "\n\nWhich would you like to explore?",
            "state": cumulative_state, "active_state": active_state,
            "lead_id": lead_id,
            "recommendations": recommendations,
        }

    save_search_state(
        lead_id, cumulative_state, base_url, lead_fields, active_state
    )
    question = str(update.get("question") or "").strip()
    if not question and active_state["recommended_condos"]:
        question = "Which of these condos would you like to explore?"
    if not question:
        question = "What would make a home feel like the right fit for you?"
    return {
        "action": "ask",
        "text": question,
        "state": cumulative_state, "active_state": active_state,
        "lead_id": lead_id,
    }


@app.route("/chat_stream", methods=["POST"])
def chat_stream():

    request_started = time.perf_counter()
    try:

        data = request.get_json(silent=True) or {}
        folio_id = data.get("folio_id")
        message_id = data.get("message_id")
        bubble_env = data.get("bubble_env", "live")

        if bubble_env not in ("development", "live"):
            bubble_env = "live"

        print(f"Bubble environment: {bubble_env}", flush=True)
        print(f"Folio ID received: {folio_id}", flush=True)
        message = next(
            (
                data.get(field)
                for field in ("message", "prompt", "user_message", "text")
                if isinstance(data.get(field), str) and data.get(field).strip()
            ),
            ""
        )

        previous = data.get("previous_response_id")

        if previous in ("", "null"):
            previous = None

        print(
            "Received /chat_stream request "
            f"keys={list(data.keys())} message={message[:160]!r} "
            f"has_previous={bool(previous)}",
            flush=True
        )

        if not message:
            log_timing("TOTAL REQUEST BEFORE ERROR", request_started)
            return jsonify({
                "error": (
                    "Missing chat message. Send it as 'message' (or prompt, "
                    "user_message, or text)."
                )
            }), 400

        @stream_with_context
        def generate():
            try:
                web_search_status_sent = False
                property_details_web_fallback = False
                first_delta_sent = False
                initial_first_event_logged = False
                initial_first_delta_logged = False

                def stream_initial_response(response_args, timing_label):
                    nonlocal initial_first_event_logged
                    nonlocal initial_first_delta_logged, web_search_status_sent
                    nonlocal first_delta_sent

                    initial_started = time.perf_counter()
                    buffered_text_deltas = []
                    event_types = []
                    tool_call_started = False
                    response_id_seen = None
                    iterator_ended_naturally = False
                    stream_error = None
                    final_response = None
                    try:
                        with client.responses.stream(**response_args) as stream:
                            try:
                                for event in stream:
                                    event_type = str(getattr(event, "type", "unknown"))
                                    event_types.append(event_type)
                                    response_object = getattr(event, "response", None)
                                    response_id_seen = (
                                        getattr(response_object, "id", None)
                                        or getattr(event, "response_id", None)
                                        or response_id_seen
                                    )
                                    item = getattr(event, "item", None)
                                    if (
                                        "function_call" in event_type
                                        or "web_search_call" in event_type
                                        or getattr(item, "type", None)
                                        in {"function_call", "web_search_call"}
                                    ):
                                        tool_call_started = True
                                    if not initial_first_event_logged:
                                        log_timing("Initial OpenAI FIRST EVENT", initial_started)
                                        initial_first_event_logged = True
                                    if (
                                        event_type.startswith("response.web_search_call.")
                                        and not web_search_status_sent
                                    ):
                                        print("Web search used", flush=True)
                                        web_search_status_sent = True
                                    if event_type == "response.output_text.delta":
                                        if not initial_first_delta_logged:
                                            log_timing("Initial OpenAI FIRST DELTA", initial_started)
                                            initial_first_delta_logged = True
                                        # Buffer until completion proves no function call follows.
                                        buffered_text_deltas.append(event.delta)
                                iterator_ended_naturally = True
                            except Exception as error:
                                stream_error = error
                            if stream_error is None:
                                try:
                                    final_response = stream.get_final_response()
                                except Exception as error:
                                    stream_error = error
                    except Exception as error:
                        stream_error = stream_error or error

                    if stream_error is not None:
                        elapsed = time.perf_counter() - initial_started
                        for event_type in event_types:
                            print(f"[OPENAI STREAM] {event_type}", flush=True)
                        diagnostic = (
                            f"exception={type(stream_error).__name__}: {stream_error}; "
                            f"events={event_types}; text_chars="
                            f"{sum(len(delta) for delta in buffered_text_deltas)}; "
                            f"tool_call_started={tool_call_started}; "
                            f"response_id={response_id_seen}; elapsed={elapsed:.2f}s; "
                            f"iterator_ended_naturally={iterator_ended_naturally}"
                        )
                        print(f"[OPENAI STREAM WARNING] {diagnostic}", flush=True)
                        if buffered_text_deltas and not tool_call_started:
                            text_chars = sum(len(delta) for delta in buffered_text_deltas)
                            print(
                                "[STREAM WARNING] OpenAI stream ended without "
                                "response.completed; preserving "
                                f"{text_chars} chars of customer-facing text",
                                flush=True,
                            )
                            final_response = SimpleNamespace(
                                id=response_id_seen, output=[], usage=None,
                            )
                        else:
                            if tool_call_started:
                                print(
                                    "[OPENAI STREAM WARNING] Interrupted tool selection; "
                                    "partial tool call will not be executed",
                                    flush=True,
                                )
                            log_timing(f"{timing_label} failed", initial_started)
                            raise stream_error

                    log_token_usage("Initial", final_response)
                    log_response_output_summary(
                        "Initial", final_response, buffered_text_deltas,
                        web_search_status_sent,
                    )

                    log_timing(f"{timing_label} complete", initial_started)
                    return final_response, buffered_text_deltas

                # The initial turn carries the incoming response ID, preserving
                # the user's existing conversation history.
                try:
                    response, buffered_initial_text = stream_initial_response(
                        build_response_args(message, previous),
                        "Initial OpenAI/tool selection"
                    )
                except Exception as error:
                    if "No tool output found for function call" not in str(error):
                        raise

                    print(
                        "Broken previous_response_id detected; starting a fresh conversation",
                        flush=True
                    )
                    response, buffered_initial_text = stream_initial_response(
                        build_response_args(message, None),
                        "Initial OpenAI/tool selection retry"
                    )
                if any(
                    output_item.type == "web_search_call"
                    for output_item in response.output
                ) and not web_search_status_sent:
                    print("Web search used", flush=True)
                    web_search_status_sent = True
                tool_call = next(
                    (x for x in response.output if x.type == "function_call"),
                    None
                )

                if tool_call is None:
                    print("No tool call requested", flush=True)
                    for delta in buffered_initial_text:
                        if not first_delta_sent:
                            log_timing("FIRST DELTA", request_started)
                            first_delta_sent = True
                        yield f"data: {json.dumps({'delta': delta})}\n\n"
                    citations = get_web_citations(response)

                    if citations:
                        yield f"data: {json.dumps({'citations': citations})}\n\n"
                    log_timing("TOTAL REQUEST", request_started)
                    yield (
                        f"data: {json.dumps({'done': True, 'response_id': response.id, 'recommendations_relevant': False})}\n\n"
                    )
                    return

                if buffered_initial_text:
                    print(
                        "WARNING: Suppressed initial OpenAI text emitted before tool "
                        f"{tool_call.name}; internal orchestration was not sent to Bubble.",
                        flush=True
                    )

                original_response_id = response.id
                original_call_id = tool_call.call_id
                print(f"Tool selected: {tool_call.name}", flush=True)
                print(f"Original call_id: {original_call_id}", flush=True)
                tool_args = parse_completed_tool_arguments(tool_call)
                follow_up_tools = None

                if tool_call.name == "advance_property_search":
                    has_match_results = False
                    search_result = advance_property_search(
                        folio_id, bubble_env, tool_args
                    )
                    if search_result["action"] == "search_listings":
                        matching_result = execute_match_lead_silently(
                            folio_id,
                            bubble_env,
                            message_id,
                            search_result["scope"],
                        )
                        save_search_state(
                            search_result["lead_id"], search_result["state"],
                            get_bubble_base_url(bubble_env),
                        )
                        has_match_results = bool(getattr(
                            matching_result, "recommendations_available", False
                        ))
                        if has_match_results:
                            tool_result = (
                                f"{matching_result}\n\nI've put together a curated selection "
                                "based on your requirements. Please click INTERESTED on the "
                                "properties you'd like to view."
                            )
                        else:
                            tool_result = str(matching_result)
                    else:
                        tool_result = search_result["text"]
                    follow_up_instructions = (
                        "Return the supplied customer-facing search-flow response faithfully. "
                        "Ask no more than the single question it contains. Do not invent "
                        "listings, condos, requirements, or internal state."
                    )
                elif tool_call.name == "match_lead":
                    matching_result = execute_match_lead_silently(
                        folio_id, 
                        bubble_env,
                        message_id
                    )
                    has_match_results = bool(getattr(
                        matching_result, "recommendations_available", False
                    ))
                    tool_result = str(matching_result)
                    follow_up_instructions = (
                        "The tool output already contains the final customer-facing answer. "
                        "Return it faithfully. Do not add, remove, reinterpret, embellish, "
                        "or invent property information."
                    )
                elif tool_call.name == "get_property_details":
                    has_match_results = False
                    tool_result = get_property_details(
                        folio_id,
                        tool_args["property_reference"],
                        bubble_env
                    )
                    follow_up_instructions = (
                        "Check whether the authoritative Rentee details in the tool output "
                        "actually answer the customer's question. If a requested general "
                        "building, development, location, neighbourhood, transport, school, "
                        "amenity, developer, historical, regulatory, or other public external "
                        "fact is missing, use web search immediately before answering. If a "
                        "missing fact is specific to this available unit, do not search or "
                        "guess; say the current listing information does not specify it. Do "
                        "not expose internal identifiers or offer unsupported actions."
                    )
                    # The model decides whether the returned details are incomplete
                    # and whether a public web lookup is appropriate. The streamed
                    # web-search event below then selects the customer-facing status.
                    property_details_web_fallback = True
                    follow_up_tools = [{"type": "web_search"}]
                elif tool_call.name == "get_current_recommendations":
                    has_match_results = False
                    tool_result = get_current_recommendations(folio_id, bubble_env)
                    follow_up_instructions = (
                        "Answer using only the supplied current recommendations. Compare or "
                        "filter them as requested. Use customer-facing names, never internal "
                        "IDs. Say when a field is unavailable. Do not start a new search, "
                        "change requirements, or imply that the shortlist was modified."
                    )
                elif tool_call.name == "get_condo_info":
                    has_match_results = False
                    condo_names = tool_args.get("condo_names")
                    if not isinstance(condo_names, list):
                        condo_names = []
                    tool_result = get_condo_infos(condo_names)
                    follow_up_instructions = (
                        "Answer the customer's condo question using the supplied condo data. "
                        "Use factual fields as facts. Treat Persona as qualitative expert "
                        "insight and phrase opinions, suitability, strengths, weaknesses, and "
                        "trade-offs accordingly. For comparisons, compare only the returned "
                        "data. Clearly identify condos that were not found and say when the "
                        "requested information is unavailable. Do not invent missing details, "
                        "claim current listing availability, or expose tool/internal field names."
                    )
                else:
                    raise ValueError(f"Unsupported tool: {tool_call.name}")

                # Continue the same response chain with the function result,
                # then stream the final assistant answer back to Bubble.
                if tool_call.name == "advance_property_search":
                    print(
                        "Submitting function_call_output for original "
                        f"advance_property_search call {original_call_id}",
                        flush=True
                    )
                elif tool_call.name == "match_lead":
                    print(
                        "Submitting function_call_output for original "
                        f"match_lead call {original_call_id}",
                        flush=True
                    )
                elif tool_call.name == "get_condo_info":
                    print(
                        "Submitting function_call_output for original "
                        f"get_condo_info call {original_call_id}",
                        flush=True
                    )
                elif tool_call.name == "get_current_recommendations":
                    print(
                        "Submitting function_call_output for original "
                        f"get_current_recommendations call {original_call_id}", flush=True
                    )
                else:
                    print(
                        "Submitting function_call_output for original "
                        f"get_property_details call {original_call_id}",
                        flush=True
                    )
                continuation_args = {
                    "model": "gpt-5-mini",
                    "reasoning": {"effort": "low"},
                    "max_output_tokens": FINAL_MAX_OUTPUT_TOKENS,
                    "previous_response_id": original_response_id,
                    "instructions": follow_up_instructions,
                    "input": [{
                        "type": "function_call_output",
                        "call_id": original_call_id,
                        "output": tool_result
                    }]
                }

                if follow_up_tools:
                    continuation_args["tools"] = follow_up_tools

                final_openai_started = time.perf_counter()
                with client.responses.stream(**continuation_args) as stream:
                    for event in stream:
                        if (
                            event.type.startswith("response.web_search_call.")
                            and not web_search_status_sent
                        ):
                            print("Web search used", flush=True)
                            web_search_status_sent = True
                        if event.type == "response.output_text.delta":
                            if not first_delta_sent:
                                log_timing("FIRST DELTA", request_started)
                                first_delta_sent = True
                            yield f"data: {json.dumps({'delta': event.delta})}\n\n"

                    final = stream.get_final_response()
                log_token_usage("Final", final)
                log_timing("Final OpenAI completion", final_openai_started)

                print("Tool lifecycle completed", flush=True)

                citations = get_web_citations(final)

                if citations:
                    yield f"data: {json.dumps({'citations': citations})}\n\n"

                log_timing("TOTAL REQUEST", request_started)
                yield (
                    f"data: {json.dumps({'done': True, 'response_id': final.id, 'recommendations_relevant': has_match_results})}\n\n"
                )
            except Exception as error:
                print(f"/chat_stream failed: {error}", flush=True)
                log_timing("TOTAL REQUEST BEFORE ERROR", request_started)
                yield f"data: {json.dumps({'error': 'Sorry, something went wrong. Please try again.', 'done': True})}\n\n"

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no"
            }
        )

    except Exception as e:

        log_timing("TOTAL REQUEST BEFORE ERROR", request_started)
        return jsonify({"error": str(e)}), 500


def run_rentee_turn(message, folio_id, previous_response_id=None, message_id=None,
                    bubble_env="live"):
    """Run the existing customer turn lifecycle and collect its clean final text."""
    payload = {
        "message": message, "folio_id": folio_id, "bubble_env": bubble_env,
        "message_id": message_id,
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    # Keep one implementation of prompts, tools, retries, and event filtering: the
    # same streaming endpoint is consumed internally and collapsed for WhatsApp.
    with app.test_client() as test_client:
        response = test_client.post("/chat_stream", json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Rentee turn returned HTTP {response.status_code}")
        text_parts = []
        response_id = None
        recommendations_relevant = False
        for raw_line in response.get_data(as_text=True).splitlines():
            if not raw_line.startswith("data: "):
                continue
            event = json.loads(raw_line[6:])
            if event.get("delta"):
                text_parts.append(str(event["delta"]))
            if event.get("error"):
                raise RuntimeError("Rentee turn failed")
            if event.get("response_id"):
                response_id = event["response_id"]
            if event.get("recommendations_relevant") is True:
                recommendations_relevant = True
    answer = "".join(text_parts).strip()
    if not answer:
        raise RuntimeError("Rentee turn returned no customer-facing text")
    return answer, response_id, recommendations_relevant


def _process_whatsapp_message(message):
    message_id = str(message["id"])
    try:
        send_whatsapp_typing_indicator(message_id)
    except Exception as error:
        print(
            "[WHATSAPP] Typing indicator error ignored "
            f"message_id={message_id} error={type(error).__name__}",
            flush=True,
        )
    phone = normalize_phone(message["from"])
    text = str((message.get("text") or {}).get("body") or "").strip()
    phone_lock = _whatsapp_phone_locks.setdefault(phone, threading.Lock())
    reply_sent = False
    try:
        with phone_lock:
            base_url = get_bubble_base_url("live")
            internal_user = find_internal_user(
                phone, base_url, _bubble_records, bubble, normalize_phone
            )
            safe_phone = f"...{phone[-4:]}" if phone else "unknown"
            if extract_handoff_code(text):
                handoff_result = handle_external_handoff_message(
                    phone, text, base_url, _bubble_records, bubble,
                    _bubble_patch, normalize_phone,
                    sender_user_id=(
                        internal_user.get("_id") if internal_user else None
                    ),
                    find_or_create_lead=find_or_create_whatsapp_lead,
                    whatsapp_profile_name=message.get("customer_name"),
                )
                send_whatsapp_text(phone, handoff_result.response_text)
                followup_text = getattr(handoff_result, "followup_text", None)
                if isinstance(followup_text, str) and followup_text.strip():
                    enquiry_id = getattr(handoff_result, "enquiry_id", None)
                    try:
                        send_whatsapp_text(phone, followup_text)
                        print(
                            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                            "tenant_profile_request_sent",
                            flush=True,
                        )
                    except Exception:
                        print(
                            f"[ENQUIRY WORKFLOW] enquiry_id={enquiry_id} "
                            "tenant_profile_request_failed",
                            flush=True,
                        )
                print(
                    f"[ENQUIRY WORKFLOW] handoff message handled phone={safe_phone}",
                    flush=True,
                )
                reply_sent = True
                return
            if internal_user:
                print(
                    "[ENQUIRY WORKFLOW] internal User matched "
                    f"phone={safe_phone} user_id={internal_user.get('_id')}",
                    flush=True,
                )
                workflow_result = handle_internal_user_message(
                    internal_user, text, base_url, _bubble_patch,
                    bubble_create=_bubble_create,
                    bubble_records=_bubble_records,
                    relationship_names=get_relationship_names,
                    bubble_get=bubble,
                    normalize_phone=normalize_phone,
                    rentee_whatsapp_number=os.getenv("RENTEE_WHATSAPP_NUMBER"),
                )
                if workflow_result.handled:
                    send_whatsapp_text(phone, workflow_result.response_text)
                    workflow_result.complete()
                    print(
                        f"[ENQUIRY WORKFLOW] user_id={internal_user.get('_id')} handled",
                        flush=True,
                    )
                    reply_sent = True
                    return
            else:
                print(
                    f"[ENQUIRY WORKFLOW] no internal User match phone={safe_phone}",
                    flush=True,
                )
            linked_lead = capture_linked_tenant_profile(phone, text, "live")
            if linked_lead:
                lead, lead_created = linked_lead, False
            else:
                lead, lead_created = find_or_create_whatsapp_lead(
                    phone, message.get("customer_name"), "live"
                )
            lead_id = lead["_id"]
            folio_id, folio_created = find_or_create_lead_folio(lead_id, "live")
            previous_message = find_latest_ai_message(lead_id, "live")
            previous_message_id = previous_message.get("_id") if previous_message else None
            previous_response_id = (
                str(previous_message.get("response_ID") or "").strip()
                if previous_message else None
            )
            current_message_id = create_whatsapp_ai_message(lead_id, "live")
            print(
                "[WHATSAPP CONVERSATION] "
                f"phone={safe_phone} lead_id={lead_id} lead_created={lead_created} "
                f"folio_id={folio_id} folio_created={folio_created} "
                f"previous_message_id={previous_message_id} "
                f"previous_response_id={previous_response_id}",
                flush=True,
            )
            print(
                f"[WHATSAPP CONVERSATION] current_message_id={current_message_id}",
                flush=True,
            )
            answer, response_id, recommendations_relevant = run_rentee_turn(
                text, folio_id, previous_response_id=previous_response_id,
                message_id=current_message_id, bubble_env="live",
            )
            recommendation_listings = []
            if recommendations_relevant:
                recommendation_result = json.loads(
                    get_current_recommendations(folio_id, "live", include_media=True)
                )
                recommendation_listings = (
                    recommendation_result.get("current_recommendations") or []
                )
                recommendation_summary = build_whatsapp_recommendation_summary(
                    folio_id, "live", listings=recommendation_listings
                )
                if recommendation_summary:
                    answer = recommendation_summary
            save_whatsapp_ai_message(
                current_message_id, answer, response_id, "live"
            )
            if recommendation_listings:
                send_whatsapp_recommendation_batch(
                    phone, folio_id, recommendation_listings
                )
            else:
                send_whatsapp_text(phone, answer)
            reply_sent = True
            print(
                "[WHATSAPP CONVERSATION] "
                f"current_message_id={current_message_id} "
                f"saved_response_id={response_id}",
                flush=True,
            )
    except Exception as error:
        print(f"WhatsApp message {message_id} failed: {error}", flush=True)
        if not reply_sent:
            try:
                send_whatsapp_text(
                    phone, "Sorry, I had trouble checking that just now. Please try again."
                )
            except Exception as send_error:
                print(f"WhatsApp fallback send failed: {send_error}", flush=True)
    finally:
        with _whatsapp_processing_lock:
            _whatsapp_processing_ids.discard(message_id)
            if message_id not in _whatsapp_processed_ids:
                if len(_whatsapp_processed_order) == _whatsapp_processed_order.maxlen:
                    _whatsapp_processed_ids.discard(_whatsapp_processed_order[0])
                _whatsapp_processed_order.append(message_id)
                _whatsapp_processed_ids.add(message_id)


def _whatsapp_text_messages(payload):
    messages = []
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        return messages
    for entry in payload.get("entry", []) or []:
        for change in entry.get("changes", []) or []:
            value = change.get("value") or {}
            contacts = value.get("contacts") or []
            names = {
                str(contact.get("wa_id")): (contact.get("profile") or {}).get("name")
                for contact in contacts if contact.get("wa_id")
            }
            for item in value.get("messages", []) or []:
                if item.get("type") != "text" or not item.get("id") or not item.get("from"):
                    continue
                body = (item.get("text") or {}).get("body")
                if not isinstance(body, str) or not body.strip():
                    continue
                clean = dict(item)
                clean["customer_name"] = names.get(str(item.get("from")))
                messages.append(clean)
    return messages


@app.route("/whatsapp/webhook", methods=["GET"])
def verify_whatsapp_webhook():
    valid = (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == os.environ.get("WHATSAPP_VERIFY_TOKEN")
        and bool(os.environ.get("WHATSAPP_VERIFY_TOKEN"))
    )
    if not valid:
        return "Forbidden", 403
    return request.args.get("hub.challenge", ""), 200


@app.route("/whatsapp/webhook", methods=["POST"])
def receive_whatsapp_webhook():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid webhook payload"}), 400
    for message in _whatsapp_text_messages(payload):
        message_id = str(message["id"])
        with _whatsapp_processing_lock:
            if (
                message_id in _whatsapp_processing_ids
                or message_id in _whatsapp_processed_ids
            ):
                continue
            _whatsapp_processing_ids.add(message_id)
        worker = threading.Thread(
            target=_process_whatsapp_message, args=(message,), daemon=True,
            name=f"whatsapp-{message_id[-12:]}",
        )
        worker.start()
    return "EVENT_RECEIVED", 200


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
