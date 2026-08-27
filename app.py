from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from openai import OpenAI
import csv
import io
import os
import requests
import json
import threading
import time
from pathlib import Path

from search_flow import (
    area_recommendation_needed,
    apply_search_update,
    dump_search_state,
    listing_search_scope,
    load_search_state,
    next_search_question,
    search_brief_complete,
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
MATCH_LISTING_LIMIT = 600
CONDO_SHEET_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1wnXHS6cHoUmAVXFpkzZ9PhBKmEgG6g-n8n0jcyodYig/export?format=csv&gid=0"
)
CONDO_CACHE_TTL_SECONDS = 300
CONDO_SHEET_TIMEOUT_SECONDS = 15

_condo_cache = None
_condo_cache_checked_at = 0.0
_condo_cache_lock = threading.Lock()

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
        SKILLS_DIRECTORY / "property_search" / "SKILL.md",
        SKILLS_DIRECTORY / "condo_advice" / "SKILL.md",
    )
    return "\n\n".join(path.read_text(encoding="utf-8").strip() for path in skill_paths)


def rentee_instructions():
    return f"{CORE_PROMPT}\n\n# Skills\n\n{load_ai_skills()}"


class CondoDataError(RuntimeError):
    pass


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
        "area_status": {"type": "string", "enum": ["unchanged", "known", "unknown"]},
        "areas": {"type": "array", "items": {"type": "string"}},
        "regular_destinations": {"type": "array", "items": {"type": "string"}},
        "property_types": {"type": "array", "items": {"type": "string"}},
        "bedroom_requirement": {"type": "string"},
        "budget_requirement": {"type": "string"},
        "other_requirements": {"type": "array", "items": {"type": "string"}},
        "other_requirements_answered": {"type": "boolean"},
        "priorities": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "priorities_answered": {"type": "boolean"},
        "selected_condos": {"type": "array", "items": {"type": "string"}},
        "use_full_shortlist": {"type": "boolean"},
        "search_listings": {"type": "boolean"},
    }
    args = {
        "model": "gpt-5-mini",
        "input": user_message,
        "instructions": rentee_instructions(),
        "tool_choice": "auto",
        "tools": [
            {
                "type": "function", "name": "advance_property_search",
                "description": (
                    "Save requirements and continue a personalised home search. Useful for "
                    "new searches, refinements, area or condo recommendations, and listing "
                    "requests grounded in the saved condo shortlist. Send every requirement "
                    "stated in the current message; unchanged fields may be empty. Returns the "
                    "next useful customer-facing result or a listing-search scope."
                ),
                "parameters": {
                    "type": "object", "properties": search_properties,
                    "required": list(search_properties), "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "match_lead", "strict": True,
                "description": (
                    "Find and rank current Rentee listings for the saved customer profile. "
                    "Use for a direct request for currently available homes when no guided "
                    "shortlist needs to constrain the search. Returns grounded recommendations."
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
    if tool_call.name == "match_lead" and arguments:
        raise ValueError("match_lead does not accept arguments.")
    return arguments


def bubble(url, **kwargs):

    r = requests.get(url, timeout=30, **kwargs)

    r.raise_for_status()

    return r.json()["response"]


def get_all_listings(base_url):

    load_started = time.perf_counter()
    listings = []
    cursor = 0
    seen_cursors = set()

    while cursor not in seen_cursors:
        seen_cursors.add(cursor)
        page_started = time.perf_counter()
        page = bubble(f"{base_url}/obj/listing", params={"cursor": cursor})
        results = page.get("results", [])
        log_timing(
            f"Listing page {len(seen_cursors)}",
            page_started,
            f" ({len(results)} listings)"
        )
        listings.extend(results)
        remaining = page.get("remaining", 0) or 0
        print(
            f"Loaded {len(results)} listings; {remaining} remaining",
            flush=True
        )

        if not results or not remaining:
            break

        # Bubble's cursor is the current offset, so advance by this page size.
        cursor += len(results)

    log_timing("Load all listings", load_started, f" ({len(listings)} listings)")
    return listings


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

    yield "Searching available properties..."
    listings_started = time.perf_counter()
    listings = get_all_listings(base_url)[:MATCH_LISTING_LIMIT]
    if condo_scope:
        condo_cache = {}
        listings = [
            listing for listing in listings
            if _listing_is_in_condo_scope(
                listing, condo_scope, base_url, condo_cache
            )
        ]
        print(
            "Constrained listing search to recommended condos: "
            + ", ".join(condo_scope),
            flush=True,
        )
    log_timing("match_lead - load listings", listings_started)

    print(
        f"Scoring {len(listings)} listings (test limit: {MATCH_LISTING_LIMIT})",
        flush=True
    )

    prompt_started = time.perf_counter()
    prompt = f"""

You are helping a property seeker find their ideal home.

Review the home seeker's requirements and all available properties.

Select only properties you genuinely believe could be a good fit.

Rank the strongest matches from best to worst.

=========================

HOME SEEKER REQUIREMENTS

=========================

{lead["AIsearchtext"]}

=========================

AVAILABLE PROPERTIES

=========================

"""

    for listing in listings:

        prompt += f"""

INTERNAL LISTING ID: {listing.get("_id")}

Bedrooms: {listing.get("beds")}

Bathrooms: {listing.get("baths")}

Rent: {listing.get("priceRent")}

Sale: {listing.get("priceSale")}

{listing.get("AIsearchtext","")}

----------------------------------------

"""

    prompt += """

For each recommended property:

- Give the property or building name where available.
- Explain briefly why it suits the user's requirements.
- Mention any important compromise or consideration.
- Keep the explanation focused on what matters to the user.

Do not recommend properties simply to fill a list. If only a few properties
are genuinely suitable, recommend only those properties.

Write directly to the property seeker using 'you' and 'your'. Be helpful,
confident, and conversational, like a highly knowledgeable personal property
concierge.

Do not mention Lead IDs, Folio IDs, Listing IDs, internal database information,
the matching process, internal scoring, or estate-agent workflows.

Do not invent facts. Only use information in the home seeker requirements and
supplied property information.

Return valid JSON with exactly these fields:
- recommendations: an array in ranking order. Each item must contain the
  INTERNAL LISTING ID from the supplied properties as listing_id and a
  personalised reco_summary. Include only properties you genuinely recommend;
  never invent an ID or add properties to fill a list.
- customer_response: concise, natural, customer-facing recommendation prose.
  Never mention internal IDs, Folio IDs, Lead IDs, database fields, or the
  matching process.

For every recommended listing, reco_summary must be a short, personalised
one- or two-sentence explanation of why this listing suits this home seeker.
Focus on the one to three strongest relevant requirements and actual listing
attributes, and mention a material trade-off when applicable. Use natural,
consumer-friendly language. Do not mention IDs, scores, matching logic, or
AIsearchtext; do not use generic real-estate marketing language; and do not
invent facts or claim a requirement exists unless it appears in the supplied
home seeker requirements. reco_summary is recommendation reasoning, not a
rewritten listing description.

The recommendations array is the source of truth. customer_response must
describe only the listings represented there, in the same order. Never invent
a property, unit, building name, or property detail. If a name or detail is not
in the supplied property information, do not mention it.

"""
    log_timing("match_lead - build matching input", prompt_started)

    yield "Ranking the best matches..."
    matching_started = time.perf_counter()
    response = client.responses.create(

        model="gpt-5-mini",

        input=prompt,
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
                            "items": {
                                "type": "object",
                                "properties": {
                                    "listing_id": {"type": "string"},
                                    "reco_summary": {"type": "string"}
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
        listing["_id"]
        for listing in listings
        if listing.get("_id")
    }
    validated_recommendations = []
    seen_recommended_listing_ids = set()

    for recommendation in result["recommendations"]:
        listing_id = recommendation["listing_id"]
        if listing_id not in available_listing_ids:
            print("Ignoring invalid recommended listing ID", flush=True)
        elif listing_id not in seen_recommended_listing_ids:
            validated_recommendations.append(recommendation)
            seen_recommended_listing_ids.add(listing_id)

    new_recommendations = [
        recommendation
        for recommendation in validated_recommendations
        if recommendation["listing_id"] not in existing_listing_ids
    ]
    log_timing("match_lead - parse/validate", parse_started)

    yield "Updating your shortlist..."

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
            return result["customer_response"]
        log_timing("Clear previous newlyAdded flags", clear_started)

        new_folio_item_ids = create_folio_items(
            new_recommendations, 
            base_url,
            message_id
        )

        if new_folio_item_ids is not None:
            final_folio_item_ids = existing_folio_item_ids + new_folio_item_ids
            try:
                update_folio_items(folio_id, final_folio_item_ids, base_url)
            except Exception as error:
                print(f"Failed to update Folio Items: {error}", flush=True)
        log_timing("match_lead - update FolioItems", folio_items_update_started)

    log_timing("match_lead TOTAL", match_started)
    return result["customer_response"]


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


def update_lead_ai_searchtext(lead_id, updated_text, ai_search_summary, base_url):

    print(f"Updating Lead preferences for lead {lead_id}", flush=True)
    update_started = time.perf_counter()
    response = requests.patch(
        f"{base_url}/obj/lead/{lead_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "AIsearchtext": updated_text,
            "AIsearchsummary": ai_search_summary
        },
        timeout=30
    )
    response.raise_for_status()
    log_timing("Update Lead preferences", update_started)
    print("Lead preferences updated successfully", flush=True)


def save_search_state(lead_id, search_state, base_url):
    save_started = time.perf_counter()
    payload = {
        "searchBriefJSON": dump_search_state(search_state),
        "AIsearchtext": search_state_to_requirements_text(search_state),
        "AIsearchsummary": search_state_to_summary(search_state),
    }
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


def extract_search_update_from_profile(profile_text):
    response = client.responses.create(
        model="gpt-5-mini",
        input=(
            "Extract only explicitly recorded current search requirements from this "
            "existing renter profile. Empty values mean unknown. Preserve bedroom and "
            "budget nuance. Mark other requirements/priorities answered only when the "
            "profile explicitly records them or explicitly says none.\n\n"
            + str(profile_text or "")
        ),
        text={"format": {
            "type": "json_schema",
            "name": "existing_search_brief",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "area_status": {
                        "type": "string", "enum": ["unchanged", "known", "unknown"]
                    },
                    "areas": {"type": "array", "items": {"type": "string"}},
                    "regular_destinations": {"type": "array", "items": {"type": "string"}},
                    "property_types": {"type": "array", "items": {"type": "string"}},
                    "bedroom_requirement": {"type": "string"},
                    "budget_requirement": {"type": "string"},
                    "other_requirements": {"type": "array", "items": {"type": "string"}},
                    "other_requirements_answered": {"type": "boolean"},
                    "priorities": {"type": "array", "items": {"type": "string"}},
                    "priorities_answered": {"type": "boolean"},
                },
                "required": [
                    "area_status", "areas", "regular_destinations", "property_types",
                    "bedroom_requirement", "budget_requirement", "other_requirements",
                    "other_requirements_answered", "priorities", "priorities_answered",
                ],
                "additionalProperties": False,
            },
        }},
    )
    return json.loads(response.output_text)


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
    lines.extend(["", "Which of those areas sounds closest to what you want?"])
    return "\n".join(lines)


def advance_property_search(folio_id, bubble_env, update):
    """Advance the durable brief and return the next deterministic action."""
    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    lead_id = folio["lead"]
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    stored_state = lead.get("searchBriefJSON")
    state = load_search_state(stored_state)
    if not stored_state and str(lead.get("AIsearchtext") or "").strip():
        state = apply_search_update(
            state, extract_search_update_from_profile(lead["AIsearchtext"])
        )
    state = apply_search_update(state, update)

    if area_recommendation_needed(state):
        if not state["area_recommendations"]:
            recommendations = recommend_areas_for_search(state)
            state = set_area_recommendations(state, recommendations)
        else:
            state["stage"] = "AWAITING_AREA_SELECTION"
        save_search_state(lead_id, state, base_url)
        return {
            "action": "recommend_areas",
            "text": area_recommendation_text(state),
            "state": state,
            "lead_id": lead_id,
            "recommendations": state["area_recommendations"],
        }

    if not search_brief_complete(state):
        save_search_state(lead_id, state, base_url)
        return {
            "action": "ask", "text": next_search_question(state),
            "state": state, "lead_id": lead_id,
        }

    if not state["recommended_condos"]:
        recommendations, response_text = recommend_condos_for_search(state)
        state = set_recommended_condos(
            state, [item["condo_name"] for item in recommendations]
        )
        save_search_state(lead_id, state, base_url)
        return {
            "action": "condo_shortlist",
            "text": response_text.rstrip() + "\n\nWhich would you like to explore?",
            "state": state,
            "lead_id": lead_id,
            "recommendations": recommendations,
        }

    scope = listing_search_scope(
        state,
        selected_condos=update.get("selected_condos"),
        use_full_shortlist=bool(update.get("use_full_shortlist")),
    )
    if update.get("search_listings") and scope:
        state["selected_condos"] = list(scope)
        state["stage"] = "SEARCH_LISTINGS"
        save_search_state(lead_id, state, base_url)
        return {
            "action": "search_listings", "scope": scope,
            "state": state, "lead_id": lead_id,
        }

    save_search_state(lead_id, state, base_url)
    return {
        "action": "ask",
        "text": "Which of these condos would you like to explore?",
        "state": state,
        "lead_id": lead_id,
    }


def search_update_preference_text(update):
    parts = []
    labels = (
        ("areas", "Areas"),
        ("regular_destinations", "Regular destinations"),
        ("property_types", "Property types"),
        ("bedroom_requirement", "Bedrooms"),
        ("budget_requirement", "Budget"),
        ("other_requirements", "Other requirements"),
        ("priorities", "Ordered priorities"),
    )
    for key, label in labels:
        value = update.get(key)
        if isinstance(value, list) and value:
            parts.append(f"{label}: {', '.join(str(item) for item in value)}")
        elif isinstance(value, str) and value.strip():
            parts.append(f"{label}: {value.strip()}")
    if update.get("other_requirements_answered") and not update.get("other_requirements"):
        parts.append("Other requirements: none")
    if update.get("priorities_answered") and not update.get("priorities"):
        parts.append("Ordered priorities: no particular priorities")
    return "; ".join(parts)


def update_preferences(folio_id, preference_update, bubble_env):

    preferences_started = time.perf_counter()
    base_url = get_bubble_base_url(bubble_env)
    print(f"Updating preferences for folio: {folio_id}", flush=True)
    folio_started = time.perf_counter()
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    log_timing("update_preferences - Folio lookup", folio_started)
    lead_id = folio["lead"]
    print(f"Resolved lead: {lead_id}", flush=True)
    lead_started = time.perf_counter()
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    log_timing("update_preferences - Lead lookup", lead_started)
    existing_ai_search_text = lead.get("AIsearchtext", "")

    update_prompt = f"""
You maintain a living home-search profile for one customer.

Return the complete updated AIsearchtext and a clean AIsearchsummary after
applying the requested update.

Rules:
- Preserve all existing relevant home-search information.
- Change or remove a preference only when the customer explicitly says to do so.
- Add relevant new information, creating an appropriate structured category when needed.
- Do not invent or infer preferences.
- Do not rewrite, summarise, clean up, reorder, or delete any `secret notes` or
  dated conversation/history content. It is immutable and must remain exactly
  as written.
- Do not summarise away, delete, or rewrite unrelated preferences.

AIsearchsummary rules:
- Generate it from the FINAL updated AIsearchtext, not only this latest request.
- It is a concise, customer-facing, easy-to-scan summary of current home-search
  preferences only.
- Include relevant current preferences where available, such as transaction type,
  budget, areas, condos, bedrooms, property type, furnishing, parking, schools,
  commute, family, facilities, and move-in requirements.
- Exclude secret notes, dated conversation/history content, internal IDs, internal
  implementation notes, and preferences that have been replaced.
- Use plain structured text with only non-empty categories. Do not use a table,
  generic introductory prose, or internal terminology.

CURRENT AIsearchtext:
{existing_ai_search_text}

REQUESTED PREFERENCE UPDATE:
{preference_update}
"""

    extraction_started = time.perf_counter()
    response = client.responses.create(
        model="gpt-5-mini",
        input=update_prompt,
        instructions=(
            "Return JSON matching the supplied schema. The confirmation must be a "
            "short, natural sentence addressed directly to the customer and must not "
            "mention internal IDs, fields, APIs, or tools. ai_search_summary must be "
            "a clean current customer-facing search summary derived from the final "
            "updated_ai_search_text."
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "updated_home_search_profile",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "updated_ai_search_text": {"type": "string"},
                        "ai_search_summary": {"type": "string"},
                        "confirmation": {"type": "string"}
                    },
                    "required": [
                        "updated_ai_search_text",
                        "ai_search_summary",
                        "confirmation"
                    ],
                    "additionalProperties": False
                }
            }
        }
    )
    log_timing("Preference extraction OpenAI call", extraction_started)
    log_token_usage("Preference", response)
    parse_started = time.perf_counter()
    result = json.loads(response.output_text)
    log_timing("update_preferences - parse result", parse_started)
    updated_ai_search_text = result["updated_ai_search_text"]
    ai_search_summary = result["ai_search_summary"]

    if not updated_ai_search_text.strip():
        raise ValueError("The updated home-search profile was empty.")

    lead_update_started = time.perf_counter()
    update_lead_ai_searchtext(
        lead_id,
        updated_ai_search_text,
        ai_search_summary,
        base_url
    )
    log_timing("update_preferences - Bubble Lead PATCH", lead_update_started)

    log_timing("update_preferences TOTAL", preferences_started)
    return result["confirmation"]


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
                    try:
                        with client.responses.stream(**response_args) as stream:
                            for event in stream:
                                if not initial_first_event_logged:
                                    log_timing(
                                        "Initial OpenAI FIRST EVENT",
                                        initial_started
                                    )
                                    initial_first_event_logged = True

                                if (
                                    event.type.startswith("response.web_search_call.")
                                    and not web_search_status_sent
                                ):
                                    print("Web search used", flush=True)
                                    web_search_status_sent = True

                                if event.type == "response.output_text.delta":
                                    if not initial_first_delta_logged:
                                        log_timing(
                                            "Initial OpenAI FIRST DELTA",
                                            initial_started
                                        )
                                        initial_first_delta_logged = True
                                    # Initial text is not user-visible until the completed
                                    # response proves that no function call follows it.
                                    buffered_text_deltas.append(event.delta)

                            final_response = stream.get_final_response()
                            log_token_usage("Initial", final_response)
                    except Exception:
                        log_timing(f"{timing_label} failed", initial_started)
                        raise

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
                        f"data: {json.dumps({'done': True, 'response_id': response.id})}\n\n"
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
                        recommendations = execute_match_lead_silently(
                            folio_id,
                            bubble_env,
                            message_id,
                            search_result["scope"],
                        )
                        completed_state = dict(search_result["state"])
                        completed_state["stage"] = "AWAITING_INTEREST"
                        save_search_state(
                            search_result["lead_id"], completed_state,
                            get_bubble_base_url(bubble_env),
                        )
                        has_match_results = True
                        tool_result = (
                            f"{recommendations}\n\nI've put together a curated selection "
                            "based on your requirements. Please click INTERESTED on the "
                            "properties you'd like to view."
                        )
                    else:
                        tool_result = search_result["text"]
                    follow_up_instructions = (
                        "Return the supplied customer-facing search-flow response faithfully. "
                        "Ask no more than the single question it contains. Do not invent "
                        "listings, condos, requirements, or internal state."
                    )
                elif tool_call.name == "match_lead":
                    tool_result = execute_match_lead_silently(
                        folio_id, 
                        bubble_env,
                        message_id
                    )
                    has_match_results = True
                    follow_up_instructions = (
                        "The tool output already contains the final customer-facing answer. "
                        "Return it faithfully. Do not add, remove, reinterpret, embellish, "
                        "or invent property information."
                    )
                elif tool_call.name == "update_preferences":
                    preference_confirmation = update_preferences(
                        folio_id,
                        tool_args["preference_update"],
                        bubble_env
                    )
                    has_match_results = False
                    tool_result = preference_confirmation
                    follow_up_instructions = (
                        "Return the completed preference-update confirmation naturally. "
                        "Do not mention properties or internal errors. A personalised search "
                        "journey must be advanced through advance_property_search instead."
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
                elif tool_call.name == "update_preferences":
                    print(
                        "Submitting function_call_output for original "
                        f"update_preferences call {original_call_id}",
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
                else:
                    print(
                        "Submitting function_call_output for original "
                        f"get_property_details call {original_call_id}",
                        flush=True
                    )
                continuation_args = {
                    "model": "gpt-5-mini",
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
                    f"data: {json.dumps({'done': True, 'response_id': final.id})}\n\n"
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


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
