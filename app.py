from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from openai import OpenAI
import csv
import datetime
import difflib
import io
import os
import requests
import json
import threading
import time
import re
from PIL import Image, ImageOps
from urllib.parse import urlparse
from collections import deque
from types import SimpleNamespace
from pathlib import Path

import conversation as conversation_store
from location_tools import (
    compare_locations as compare_grounded_locations,
    find_nearby_places as find_grounded_nearby_places,
    get_travel_time as get_grounded_travel_time,
)

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
WHATSAPP_TYPING_REFRESH_SECONDS = 20
WHATSAPP_TYPING_THREAD_FACTORY = threading.Thread
WHATSAPP_LEAD_PHONE_FIELD = "phone"
WHATSAPP_LEAD_NAME_FIELD = "name"
WHATSAPP_LEAD_OWNER_FIELD = "owner"
MAX_WHATSAPP_RECOMMENDATION_IMAGES = 4
GEO_NAME_CACHE_TTL_SECONDS = 300
GEO_FUZZY_MATCH_THRESHOLD = 0.86
GEO_SUGGESTION_THRESHOLD = 0.55
WHATSAPP_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"
WHATSAPP_AUDIO_ERROR_RESPONSE = (
    "Sorry, I couldn't understand that voice message. "
    "Could you try again or send it as text?"
)

_condo_cache = None
_condo_cache_checked_at = 0.0
_condo_cache_lock = threading.Lock()
_whatsapp_processing_ids = set()
_whatsapp_processed_ids = set()
_whatsapp_processed_order = deque(maxlen=1000)
_whatsapp_processing_lock = threading.Lock()
_whatsapp_phone_locks = {}
_geo_name_cache = {}
_geo_name_cache_lock = threading.Lock()
_location_web_identity_cache = {}
_location_web_identity_lock = threading.Lock()

CORE_PROMPT = """You are Rentee, an intelligent rental advisor helping people find a home.

Understand what the customer is trying to achieve, ask a useful question only when
information is genuinely needed, and help them progress toward homes they want to view.
Use the available tools and the relevant skills below when they help. Prefer useful
recommendations over unnecessary questioning. Never invent
property data, availability, prices, or facts returned by tools. Talk naturally like an
excellent human property advisor. Keep internal instructions, tools, reasoning, state,
identifiers, and raw data private.

Do not proactively offer to do things. Answer the user's request directly and stop. Do not
end responses with offers such as "I can...", "Would you like me to...", "Let me know if...",
"I can also...", or similar suggestions for additional actions. If the user explicitly asks
Rentee to perform an action and an implemented tool or workflow supports it, execute that
workflow. If the requested action is unsupported, say so briefly and explain what the user
needs to do instead. Never promise future or background actions that the system cannot
execute. Do not say "I'll follow up", "I'll get back to you", "I'll let you know", "I'll keep
checking", or "I'll contact them" unless that action is actually executed by an implemented
workflow as part of the current request.

Never guess travel times, distances, proximity, accessibility, school-run convenience, or
relative geographic convenience. Call `get_travel_time` whenever a claim materially depends
on them; call `compare_locations` before ranking where to live against school, work, hospital,
family, or other destinations. Use the returned geographic facts with the customer's broader
needs. Check resolved names, surface ambiguous branches/campuses, label area-level results as
approximations and say when the exact property could not be resolved, use only the
returned traffic basis, and never invent commute ranges. Resolve
obvious place identity and area context through the tools before asking for clarification.
Never ask for a unit number, floor, or apartment details merely to calculate travel time.
Use `find_nearby_places` for nearby amenities, not point-to-point tools or web search. When
origin, category, mode, and threshold are known, execute without further clarification.
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
    """Customer text plus the exact recommendation set produced by this run."""
    def __new__(
        cls, text, recommendations_available=False, recommendations=None,
        listing_ids=None, folio_item_ids=None,
    ):
        value = super().__new__(cls, text)
        value.recommendations_available = bool(recommendations_available)
        value.recommendations = list(recommendations or [])
        value.listing_ids = list(listing_ids or [])
        value.folio_item_ids = list(folio_item_ids or [])
        return value


def normalize_condo_name(value):
    return " ".join(str(value or "").split()).casefold()


def resolve_condo_mentions(user_message):
    """Return canonical database names for exact case/whitespace-insensitive mentions."""
    normalized_message = re.sub(
        r"[^\w]+", " ", normalize_condo_name(user_message), flags=re.UNICODE
    ).strip()
    if not normalized_message:
        return []
    padded_message = f" {normalized_message} "
    try:
        rows = _get_condo_lookup().values()
    except CondoDataError:
        return []
    matches = []
    for row in rows:
        canonical = " ".join(str(row.get("Condo name") or "").split())
        normalized_name = re.sub(
            r"[^\w]+", " ", normalize_condo_name(canonical), flags=re.UNICODE
        ).strip()
        if normalized_name and f" {normalized_name} " in padded_message:
            matches.append(canonical)
    return list(dict.fromkeys(matches))


def resolve_condo_names(candidates):
    """Resolve exact condo candidates to canonical database names."""
    try:
        rows = _get_condo_lookup().values()
    except CondoDataError:
        return {"resolved": [], "unresolved": _unique_search_values(candidates)}
    canonical_by_key = {}
    for row in rows:
        canonical = " ".join(str(row.get("Condo name") or "").split())
        if canonical:
            canonical_by_key[normalize_condo_name(canonical)] = canonical
    resolved, unresolved = [], []
    for candidate in _unique_search_values(candidates):
        canonical = canonical_by_key.get(normalize_condo_name(candidate))
        (resolved if canonical else unresolved).append(canonical or candidate)
    return {"resolved": _unique_search_values(resolved), "unresolved": unresolved}


def _is_clear_condo_information_question(user_message, canonical_names):
    """Conservatively distinguish development facts from listing retrieval."""
    if not canonical_names:
        return False
    text = normalize_condo_name(user_message)
    listing_intent = re.search(
        r"\b(find|show|search|listing|listings|unit|units|available|availability|"
        r"rent|rental|buy|sale|option|options)\b",
        text,
    )
    return not listing_intent


def _is_explicit_inventory_search(user_message):
    """Recognize unambiguous requests for current property inventory."""
    text = normalize_condo_name(user_message)
    patterns = (
        r"\bwhat (?:have|do) you got\b",
        r"\banything available\b",
        r"\bfind me\b",
        r"\bshow me\b.*\b(home|homes|house|houses|propert(?:y|ies)|listing|listings|unit|units|matches)\b",
        r"\bany\b.*\b(home|homes|house|houses|landed|propert(?:y|ies)|listing|listings|unit|units|option|options)\b.*\bavailable\b",
        r"\b(home|homes|house|houses|landed|propert(?:y|ies)|listing|listings|unit|units|option|options)\b.*\b(in|around|under)\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


INTERNAL_ORCHESTRATION_FIELDS = frozenset({
    "search_listings", "recommend_areas", "recommend_condos", "geo_names",
    "preferred_condo_names", "area_update_mode", "condo_update_mode",
    "transaction_type", "budget_rent", "budget_buy", "use_full_shortlist",
    "new_search",
})
MAX_TOOL_ROUNDS = 4


def resembles_internal_orchestration_payload(text):
    """Identify leaked Rentee search arguments without blocking ordinary customer JSON."""
    candidate = str(text or "").strip()
    if "{" not in candidate or "}" not in candidate:
        return False
    mentioned = {
        field for field in INTERNAL_ORCHESTRATION_FIELDS
        if re.search(rf'["\']{re.escape(field)}["\']\s*:', candidate)
    }
    return len(mentioned) >= 2


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
        _condo_cache is None
        and _condo_cache_checked_at
        and now - _condo_cache_checked_at < CONDO_CACHE_TTL_SECONDS
    ):
        raise CondoDataError("Condo information is temporarily unavailable.")
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


def _normalized_location_entity_name(value):
    value = str(value or "").casefold().replace("’", "'")
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


def _known_location_coordinates(location_name):
    """Resolve conservative Rentee entity matches, stored addresses and coordinates."""
    normalized = _normalized_location_entity_name(location_name)
    if not normalized:
        return None
    try:
        rows = list(_get_condo_lookup().values())
    except CondoDataError:
        return None
    exact_matches = []
    component_matches = []
    padded_input = f" {normalized} "
    for row in rows:
        canonical = " ".join(str(row.get("Condo name") or "").split())
        normalized_canonical = _normalized_location_entity_name(canonical)
        if normalized_canonical and (
            normalized == normalized_canonical
            or f" {normalized_canonical} " in padded_input
        ):
            match = (row, canonical)
            if normalized == normalized_canonical:
                exact_matches.append(match)
            else:
                component_matches.append(match)
    matches = exact_matches or component_matches
    if len(matches) > 1:
        return {"status": "ambiguous", "candidates": [
            {"name": canonical,
             "formatted_address": next((row.get(field) for field in
                 ("Address", "address", "Full Address", "formattedAddress")
                 if row.get(field)), None)}
            for row, canonical in matches[:5]
        ]}
    if not matches:
        return None
    row, canonical = matches[0]
    selected_normalized_canonical = _normalized_location_entity_name(canonical)
    latitude = next((row.get(field) for field in
                     ("latitude", "Latitude", "lat", "Lat")
                     if row.get(field) not in (None, "")), None)
    longitude = next((row.get(field) for field in
                      ("longitude", "Longitude", "lng", "Lng", "lon", "Lon")
                      if row.get(field) not in (None, "")), None)
    address = next((str(row.get(field)).strip() for field in
                    ("Address", "address", "Full Address", "formattedAddress", "Location")
                    if str(row.get(field) or "").strip()), None)
    area = next((str(row.get(field)).strip() for field in
                 ("Area", "area", "Neighbourhood", "Neighborhood", "Geo")
                 if str(row.get(field) or "").strip()), None)
    first_component = _normalized_location_entity_name(
        str(location_name or "").split(",", 1)[0]
    )
    match_type = "exact" if exact_matches else "contextual_component"
    candidate_level = (
        "building" if selected_normalized_canonical == first_component else "area"
    )
    entity = {"canonical_name": canonical, "resolved_name": canonical,
              "formatted_address": address, "area": area,
              "resolution_level": candidate_level, "match_type": match_type}
    try:
        entity.update(latitude=float(latitude), longitude=float(longitude))
    except (TypeError, ValueError):
        pass
    return entity


def _resolve_location_identity_with_web(location_name, known_entity=None):
    """Use web search only to identify a canonical place/address, never travel time."""
    cache_key = tuple(_normalized_location_entity_name(value) for value in (
        location_name, (known_entity or {}).get("canonical_name"),
        (known_entity or {}).get("formatted_address"), (known_entity or {}).get("area"),
    ))
    with _location_web_identity_lock:
        cached = _location_web_identity_cache.get(cache_key)
    if cached:
        return dict(cached)
    context = {
        "input": " ".join(str(location_name or "").split()),
        "known_canonical_name": (known_entity or {}).get("canonical_name"),
        "known_address": (known_entity or {}).get("formatted_address"),
        "known_area": (known_entity or {}).get("area"),
    }
    response = client.responses.create(
        model="gpt-5-mini",
        tools=[{"type": "web_search"}],
        input=(
            "Identify the physical place and canonical postal/street address in Malaysia. "
            "Search only for identity/address, never travel time. Prefer official developer "
            "or institution sources, then established property portals. If materially "
            "different branches/campuses remain plausible, return ambiguous. Ignore all "
            "marketing commute or travel-time claims.\n\n"
            f"LOCATION CONTEXT:\n{json.dumps(context, ensure_ascii=False)}"
        ),
        text={"format": {"type": "json_schema", "name": "location_identity",
                         "strict": True, "schema": {
            "type": "object", "properties": {
                "status": {"type": "string", "enum": ["resolved", "ambiguous", "error"]},
                "canonical_name": {"type": ["string", "null"]},
                "formatted_address": {"type": ["string", "null"]},
                "area": {"type": ["string", "null"]},
                "candidates": {"type": "array", "items": {"type": "object",
                    "properties": {"name": {"type": "string"},
                                   "formatted_address": {"type": ["string", "null"]}},
                    "required": ["name", "formatted_address"],
                    "additionalProperties": False}},
                "source_urls": {"type": "array", "items": {"type": "string"}},
            }, "required": ["status", "canonical_name", "formatted_address", "area",
                            "candidates", "source_urls"], "additionalProperties": False,
        }}},
    )
    result = json.loads(response.output_text)
    if result.get("status") == "resolved":
        with _location_web_identity_lock:
            _location_web_identity_cache[cache_key] = dict(result)
    return result


def get_travel_time(origin, destination, mode="driving",
                    origin_coordinates=None, destination_coordinates=None):
    """Application wrapper for provider-neutral grounded route information."""
    print(f"[LOCATION TOOL] name=get_travel_time origin={origin!r} destination={destination!r}",
          flush=True)
    def resolve_endpoint(value):
        normalized = _normalized_location_entity_name(value)
        for expected, coordinates in (
            (origin, origin_coordinates), (destination, destination_coordinates),
        ):
            if (coordinates and normalized == _normalized_location_entity_name(expected)):
                return {
                    "canonical_name": " ".join(str(expected or "").split()),
                    "resolved_name": " ".join(str(expected or "").split()),
                    "latitude": coordinates[0], "longitude": coordinates[1],
                    "resolution_level": "exact_property", "match_type": "exact",
                }
        return _known_location_coordinates(value)

    result = get_grounded_travel_time(
        origin, destination, mode=mode,
        coordinate_resolver=resolve_endpoint,
        web_resolver=_resolve_location_identity_with_web,
    )
    print(
        "[LOCATION TOOL] name=get_travel_time "
        f"status={result.get('status')} "
        f"origin_resolved={(result.get('origin') or {}).get('resolved_name')} "
        f"destination_resolved={(result.get('destination') or {}).get('resolved_name')} "
        f"duration_minutes={result.get('duration_minutes')} source={result.get('source')}",
        flush=True,
    )
    return json.dumps(result, ensure_ascii=False)


def find_nearby_places(origin, categories, travel_mode="walking",
                       max_travel_minutes=None, max_results=None):
    """Application wrapper for grounded amenity discovery and route filtering."""
    print(
        "[NEARBY TOOL] "
        f"origin={origin!r} categories={categories!r} mode={travel_mode} "
        f"max_minutes={max_travel_minutes}", flush=True,
    )
    result = find_grounded_nearby_places(
        origin, categories, travel_mode=travel_mode,
        max_travel_minutes=max_travel_minutes, max_results=max_results,
    )
    print(
        "[NEARBY RESOLVE] "
        f"origin_resolved={(result.get('origin') or {}).get('resolved_name')!r} "
        f"resolution_method={(result.get('origin') or {}).get('resolution_method')}",
        flush=True,
    )
    print(
        "[NEARBY SEARCH] "
        f"candidate_count={result.get('candidate_count', 0)} "
        f"radius_m={result.get('radius_m')}", flush=True,
    )
    print(
        "[NEARBY ROUTE] "
        f"routed_count={result.get('routed_count', 0)} "
        f"within_threshold={result.get('within_threshold', 0)}", flush=True,
    )
    print(
        "[NEARBY TOOL] "
        f"status={result.get('status')} result_count={len(result.get('places') or [])} "
        f"duration_ms={result.get('provider_duration_ms')}", flush=True,
    )
    return json.dumps(result, ensure_ascii=False)


def compare_locations(candidate_locations, destinations, trusted_coordinates=None):
    """Application wrapper for a batched residential-location comparison."""
    print(
        "[LOCATION TOOL] name=compare_locations "
        f"candidate_count={len(candidate_locations or [])} "
        f"destination_count={len(destinations or [])}", flush=True,
    )
    trusted_coordinates = trusted_coordinates or {}

    def resolve_location(value):
        coordinates = trusted_coordinates.get(_normalized_location_entity_name(value))
        if coordinates:
            return {
                "canonical_name": " ".join(str(value or "").split()),
                "resolved_name": " ".join(str(value or "").split()),
                "latitude": coordinates[0], "longitude": coordinates[1],
                "resolution_level": "exact_property", "match_type": "exact",
            }
        return _known_location_coordinates(value)

    result = compare_grounded_locations(
        candidate_locations, destinations,
        coordinate_resolver=resolve_location,
        web_resolver=_resolve_location_identity_with_web,
    )
    print(
        "[LOCATION TOOL] name=compare_locations "
        f"status={result.get('status')} source={result.get('source')} "
        f"duration_ms={result.get('provider_duration_ms')}", flush=True,
    )
    return json.dumps(result, ensure_ascii=False)


def _requires_travel_time_tool(message):
    text = normalize_condo_name(message)
    return bool(
        re.search(r"\b(how far|how long|travel time|drive time|commute time)\b", text)
        or (" from " in f" {text} " and re.search(r"\b(minutes?|hours?|distance|far)\b", text))
    )


def _requires_location_comparison_tool(message):
    text = normalize_condo_name(message)
    recommendation_intent = bool(re.search(
        r"\b(where should (?:i|we) live|which areas?|which locations?|"
        r"suggest.*(?:live|area)|best area|easiest commute)\b", text
    ))
    destination_context = bool(re.search(
        r"\b(school|work|office|hospital|commute|kids? go|family lives?|"
        r"frequent destination)\b", text
    ))
    return recommendation_intent and destination_context


def _requires_nearby_places_tool(message):
    text = normalize_condo_name(message)
    category = re.search(
        r"\b(supermarkets?|grocer(?:y|ies)|grocery stores?|convenience stores?|"
        r"pharmacies|pharmacy|cafes?|restaurants?|schools?|nurser(?:y|ies)|"
        r"kindergartens?|gyms?|parks?|malls?|clinics?|hospitals?|"
        r"public transport|petrol stations?|gas stations?|laundromats?|shops?)\b",
        text,
    )
    proximity = re.search(
        r"\b(nearby|nearest|closest|near|close to|close by|around|walking distance|"
        r"within .*?(?:walk|drive|minutes?))\b",
        text,
    )
    broad_around_request = re.search(
        r"\bwhat(?:'s| is) (?:there )?(?:nearby|closest|nearest|"
        r"near|around|within (?:walking|driving) distance)\b",
        text,
    )
    return bool((category and proximity) or broad_around_request)


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


def build_response_args(
    user_message, previous_response_id=None, conversation_context=None,
):
    """Build the deliberately small customer-turn context and stable tool contracts."""
    nearby_required = _requires_nearby_places_tool(user_message)
    recognized_condos = [] if nearby_required else resolve_condo_mentions(user_message)
    search_properties = {
        "regular_destinations": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "Places the customer regularly needs to reach, such as a workplace, school, "
                "hospital, or family location. Never put a requested home-search area here "
                "unless it was independently stated as a recurring destination."
            ),
        },
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
        "geo_names": {
            "type": "array", "items": {"type": "string"},
            "description": (
                "Candidate neighbourhood/area locations stated by the customer. Never "
                "put a known condo or building name here; use preferred_condo_names. "
                "The application canonicalizes areas and may ask for clarification."
            ),
        },
        "preferred_condo_names": {
            "type": "array", "items": {"type": "string"},
            "description": "Exact condo/building names constraining listing retrieval.",
        },
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
        "budget_rent": {
            "type": "number",
            "description": (
                "Customer's rental budget in RM; use only for rent searches. "
                "For example, '15k rent' means 15000 in budget_rent. Never put a "
                "purchase or sale budget here."
            ),
        },
        "budget_buy": {
            "type": "number",
            "description": (
                "Customer's purchase budget in RM; use only for buy, purchase, or "
                "sale searches. For example, '5 million to buy' or 'RM5m purchase "
                "budget' means 5000000 in budget_buy. Never put a rental budget here."
            ),
        },
    }
    condo_recognition_context = ""
    if recognized_condos:
        condo_recognition_context = (
            "\n\n# Deterministic condo recognition\n"
            "The application resolved exact condo-name mentions in the customer message "
            "using case-insensitive database matching. Canonical names: "
            + json.dumps(recognized_condos, ensure_ascii=False)
            + ". Preserve these canonical spellings. For a condo-information question, "
            "call `get_condo_info` with these names. For an explicit listing/search "
            "request, call `advance_property_search`, put them in "
            "`preferred_condo_names`, set `condo_update_mode` to `replace`, and reset "
            "the prior area constraint."
        )
    args = {
        "model": "gpt-5-mini",
        "input": user_message,
        "instructions": rentee_instructions() + condo_recognition_context + (
            f"\n\n{conversation_context}" if conversation_context else ""
        ),
        "reasoning": {"effort": "low"},
        "max_output_tokens": INITIAL_MAX_OUTPUT_TOKENS,
        "tool_choice": "auto",
        "tools": [
            {
                "type": "function", "name": "advance_property_search",
                "description": (
                    "Save new or refined search requirements, recommend areas or condos, or "
                    "search current listings within named or previously recommended condos. "
                    "Use it for what-have-you-got, properties, homes, houses, landed homes, "
                    "listings, units, options, rentals, sales, or availability, and whenever "
                    "the current message changes the search or constrains listings. Put search "
                    "areas only in geo_names, not regular_destinations. "
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
                    "only for condo facts or qualitative questions about a development: facilities, "
                    "age, character, suitability, pros and cons, or comparisons. Never use this "
                    "for current properties, inventory, listings, units, options, houses, landed "
                    "homes, rentals, sales, or availability; use advance_property_search for "
                    "those requests. Provide all names in one call."
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
            {
                "type": "function", "name": "get_travel_time",
                "description": (
                    "Get grounded driving or walking distance and estimated time between two specific "
                    "places. Required before claims about commute time, school-run time, "
                    "distance, proximity, accessibility, or relative convenience. If the "
                    "customer says this property/home, use the trusted current listing "
                    "context as the origin; a unit number or floor is never required."
                ),
                "parameters": {
                    "type": "object", "properties": {
                        "origin": {"type": "string", "description":
                                   "Property/building, area, or trusted current listing place."},
                        "destination": {"type": "string", "description": "Specific destination place."},
                        "mode": {"type": "string", "enum": ["driving", "walking"]},
                    }, "required": ["origin", "destination"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "find_nearby_places",
                "description": (
                    "Find real nearby amenities around a resolved origin and route-filter them "
                    "by walking or driving time. Use for nearby supermarkets, groceries, cafes, "
                    "pharmacies, parks, schools, restaurants, shops, clinics, transport, and "
                    "similar categories. Do not use get_travel_time unless both endpoints are "
                    "already named. Preserve the full authoritative property name and address "
                    "in origin; Google resolves it to coordinates. If walking distance "
                    "has no stated threshold, use 10 minutes. For broad shops, refresh without "
                    "clarification using shopping_mall, supermarket, grocery_store, and "
                    "convenience_store."
                ),
                "parameters": {
                    "type": "object", "properties": {
                        "origin": {"type": "string", "description":
                                   "Starting property, building, landmark, or area."},
                        "categories": {"type": "array", "items": {"type": "string"},
                                       "minItems": 1},
                        "travel_mode": {"type": "string", "enum": ["walking", "driving"]},
                        "max_travel_minutes": {"type": ["integer", "null"],
                                               "minimum": 1, "maximum": 60},
                        "max_results": {"type": ["integer", "null"],
                                        "minimum": 1, "maximum": 20},
                    }, "required": ["origin", "categories", "travel_mode"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function", "name": "compare_locations",
                "description": (
                    "Compare candidate residential areas or properties against important "
                    "destinations. Required before recommending where to live based on "
                    "geography, commute, school run, distance, or proximity."
                ),
                "parameters": {
                    "type": "object", "properties": {
                        "candidate_locations": {"type": "array", "items": {"type": "string"},
                                                "minItems": 1},
                        "destinations": {"type": "array", "minItems": 1, "items": {
                            "type": "object", "properties": {
                                "name": {"type": "string"}, "label": {"type": "string"},
                            }, "required": ["name"], "additionalProperties": False,
                        }},
                    }, "required": ["candidate_locations", "destinations"],
                    "additionalProperties": False,
                },
            },
            {"type": "web_search"},
        ],
    }
    if previous_response_id:
        args["previous_response_id"] = previous_response_id
    if _is_explicit_inventory_search(user_message):
        args["tool_choice"] = {"type": "function", "name": "advance_property_search"}
    elif _is_clear_condo_information_question(user_message, recognized_condos):
        args["tool_choice"] = {"type": "function", "name": "get_condo_info"}
    if _requires_location_comparison_tool(user_message):
        args["tool_choice"] = {"type": "function", "name": "compare_locations"}
    elif nearby_required:
        args["tool_choice"] = {"type": "function", "name": "find_nearby_places"}
    elif _requires_travel_time_tool(user_message):
        args["tool_choice"] = {"type": "function", "name": "get_travel_time"}
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


class WhatsAppIdentityCollision(RuntimeError):
    """Raised when one canonical WhatsApp number belongs to multiple Users."""


def find_bubble_users_by_phone(phone, bubble_env="live"):
    """Return every Bubble User with this exact canonical WhatsApp identity."""
    canonical_phone = normalize_phone(phone)
    if not canonical_phone:
        raise ValueError("WhatsApp sender phone is missing.")
    constraints = [{
        "key": "phone", "constraint_type": "equals",
        "value": canonical_phone,
    }]
    return [
        user for user in _bubble_records(
            get_bubble_base_url(bubble_env), "user", constraints
        )
        if user.get("_id")
    ]


def create_whatsapp_bubble_user(phone, profile_name=None, bubble_env="live"):
    """Create the deterministic Bubble identity for a WhatsApp-only user."""
    canonical_phone = normalize_phone(phone)
    if not canonical_phone:
        raise ValueError("WhatsApp sender phone is missing.")
    payload = {
        "phone": canonical_phone,
        "email": f"whatsapp-{canonical_phone}@users.rentee.internal",
    }
    name = str(profile_name or "").strip()
    if name:
        payload["name"] = name
    user_id = _bubble_create(get_bubble_base_url(bubble_env), "user", payload)
    return {"_id": user_id, **payload}


def resolve_whatsapp_user(phone, profile_name=None, bubble_env="live"):
    """Resolve/create one Bubble User using canonical WhatsApp phone identity."""
    canonical_phone = normalize_phone(phone)
    if not canonical_phone:
        raise ValueError("WhatsApp sender phone is missing.")

    def resolve_matches(matches):
        if len(matches) == 1:
            user = matches[0]
            print(
                f"[WHATSAPP USER] phone={canonical_phone} action=existing "
                f"user_id={user['_id']}", flush=True,
            )
            return user
        if len(matches) > 1:
            user_ids = sorted(str(user["_id"]) for user in matches)
            print(
                f"[WHATSAPP IDENTITY COLLISION] phone={canonical_phone} "
                f"matches={len(matches)} user_ids={user_ids}", flush=True,
            )
            raise WhatsAppIdentityCollision(
                f"Multiple Bubble Users match WhatsApp phone {canonical_phone}."
            )
        return None

    existing = resolve_matches(
        find_bubble_users_by_phone(canonical_phone, bubble_env)
    )
    if existing:
        return existing
    try:
        created = create_whatsapp_bubble_user(
            canonical_phone, profile_name, bubble_env
        )
    except Exception as create_error:
        # A concurrent webhook may have created the same deterministic identity.
        raced = resolve_matches(
            find_bubble_users_by_phone(canonical_phone, bubble_env)
        )
        if raced:
            return raced
        raise create_error
    print(
        f"[WHATSAPP USER] phone={canonical_phone} action=created "
        f"user_id={created['_id']}", flush=True,
    )
    return created


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
OWNER_CHECK_RESPONSE = "Thanks — I've got the profile. Let me check this with the owner."
OWNER_CHECK_STATUSES = {"Pending", "Sent", "Replied"}


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


def prepare_owner_check(lead, bubble_env="live"):
    """Best-effort durable pending state; this function sends no messages."""
    lead_id = str((lead or {}).get("_id") or "").strip()
    enquiry_id = str(
        (lead or {}).get("ActiveForwardedEnquiry") or ""
    ).strip()
    if not lead_id:
        return None
    if not enquiry_id:
        print(
            f"[OWNER CHECK] lead_id={lead_id} unresolved "
            "reason=no_active_forwarded_enquiry", flush=True,
        )
        return None
    base_url = get_bubble_base_url(bubble_env)
    try:
        enquiry = bubble(f"{base_url}/obj/enquiry/{enquiry_id}")
        if str(enquiry.get("Lead") or "").strip() != lead_id:
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} lead_id={lead_id} "
                "unresolved reason=active_enquiry_lead_mismatch", flush=True,
            )
            return None
        listing_id = str(enquiry.get("Listing") or "").strip()
        if not listing_id:
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} "
                "unresolved reason=missing_listing", flush=True,
            )
            return enquiry
        print(
            f"[OWNER CHECK] enquiry_id={enquiry_id} listing_id={listing_id}",
            flush=True,
        )
        status = str(enquiry.get("OwnerCheckStatus") or "").strip()
        if status in {"Sent", "Replied"}:
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} "
                f"action=skipped status={status}", flush=True,
            )
            return enquiry
        if status and status not in OWNER_CHECK_STATUSES:
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} "
                "unresolved reason=invalid_existing_status", flush=True,
            )
            return enquiry
        auth_state = "admin_token_present" if BUBBLE_API_TOKEN else "admin_token_missing"
        print(
            f"[OWNER CHECK] listing_id={listing_id} bubble_auth={auth_state}",
            flush=True,
        )
        listing = bubble(f"{base_url}/obj/listing/{listing_id}")
        owner_contact_present = "ownerContact" in listing
        print(
            f"[OWNER CHECK] listing_id={listing_id} "
            f"raw_owner_contact_present={str(owner_contact_present).lower()}",
            flush=True,
        )
        owner_contact = str(listing.get("ownerContact") or "").strip()
        if not owner_contact:
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} "
                "unresolved reason=missing_owner_contact", flush=True,
            )
            return enquiry
        owner_phone = normalize_phone(owner_contact)
        if not 8 <= len(owner_phone) <= 15:
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} "
                "unresolved reason=invalid_owner_contact", flush=True,
            )
            return enquiry
        if (
            status == "Pending"
            and normalize_phone(enquiry.get("OwnerCheckPhone")) == owner_phone
        ):
            print(
                f"[OWNER CHECK] enquiry_id={enquiry_id} action=pending_reused",
                flush=True,
            )
            return enquiry
        payload = {
            "OwnerCheckStatus": "Pending",
            "OwnerCheckPhone": owner_phone,
        }
        _bubble_patch(f"{base_url}/obj/enquiry/{enquiry_id}", payload)
        enquiry.update(payload)
        print(
            f"[OWNER CHECK] enquiry_id={enquiry_id} "
            "resolved_via=lead_active_forwarded_enquiry action=pending_created",
            flush=True,
        )
        return enquiry
    except Exception as error:
        print(
            f"[OWNER CHECK] enquiry_id={enquiry_id} unresolved "
            f"reason=persistence_failed error={type(error).__name__}", flush=True,
        )
        return None


def _owner_check_amount(value):
    if isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None
    return f"RM{amount:,.0f}"


def build_owner_check_message(enquiry, lead, listing):
    """Build grounded owner/co-broke copy without customer contact details."""
    transaction_types = (enquiry or {}).get("TransactionType") or []
    if isinstance(transaction_types, str):
        transaction_types = [transaction_types]
    transaction = next(
        (value for value in transaction_types if value in {RENT_TRANSACTION, BUY_TRANSACTION}),
        None,
    )
    property_parts = []
    property_name = next((
        str(listing.get(field)).strip()
        for field in ("name", "title", "listingName", "condoName")
        if str(listing.get(field) or "").strip()
    ), None)
    if property_name:
        property_parts.append(property_name)
    beds = listing.get("beds")
    if isinstance(beds, (int, float)) and not isinstance(beds, bool) and beds > 0:
        property_parts.append(f"{beds:g} bed")
    price_field = "priceSale" if transaction == BUY_TRANSACTION else "priceRent"
    asking = _owner_check_amount(listing.get(price_field))
    if asking:
        asking += "/month" if transaction != BUY_TRANSACTION else ""
        property_parts.append(f"at {asking}")

    profile_parts = []
    nationality = str((lead or {}).get("nationality") or "").strip()
    if nationality:
        profile_parts.append(nationality)
    household = []
    for field, label in (
        ("adults", "adults"), ("children", "children"), ("helpers", "helpers"),
    ):
        value = (lead or {}).get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            household.append(f"{value} {label}")
    if household:
        profile_parts.append(" + ".join(household))
    for field, prefix in (
        ("occupation", "occupation"),
        ("pets", "pets"),
        ("startDate", "moving" if transaction != BUY_TRANSACTION else "target timing"),
        ("furnishingPreference", "furnishing"),
    ):
        value = str((lead or {}).get(field) or "").strip()
        if value:
            profile_parts.append(f"{prefix}: {value}")
    budget_field = "budgetBuy" if transaction == BUY_TRANSACTION else "budgetRent"
    budget = _owner_check_amount((lead or {}).get(budget_field))
    if budget:
        profile_parts.append(f"budget {budget}")

    message = "Hi, I have an enquiry"
    if property_parts:
        message += " for " + ", ".join(property_parts)
    message += "."
    if profile_parts:
        message += " " + ", ".join(profile_parts) + "."
    profile_label = "buyer" if transaction == BUY_TRANSACTION else "tenant"
    return (
        f"{message} Is the unit still available and would the owner consider "
        f"this {profile_label} profile?"
    )


def _masked_whatsapp_phone(value):
    phone = normalize_phone(value)
    return f"...{phone[-6:]}" if phone else "unknown"


def _bubble_created_at(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc)


def owner_check_whatsapp_window(phone, bubble_env="live", now=None):
    """Return open/closed/unknown from the latest inbound WhatsApp Message."""
    canonical = normalize_phone(phone)
    constraints = [
        {"key": "phone", "constraint_type": "equals", "value": canonical},
        {"key": "direction", "constraint_type": "equals", "value": "Inbound"},
    ]
    try:
        messages = list(_bubble_records(
            get_bubble_base_url(bubble_env), "message", constraints
        ))
    except Exception:
        return "unknown"
    if not messages:
        return "closed"
    messages.sort(
        key=lambda item: str(
            item.get("Created Date") or item.get("created_date") or ""
        ),
        reverse=True,
    )
    created_at = _bubble_created_at(
        messages[0].get("Created Date") or messages[0].get("created_date")
    )
    if created_at is None:
        return "unknown"
    current = now or datetime.datetime.now(datetime.timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=datetime.timezone.utc)
    current = current.astimezone(datetime.timezone.utc)
    age = current - created_at
    return "open" if datetime.timedelta(0) <= age <= datetime.timedelta(hours=24) else "closed"


def send_whatsapp_template(to_phone, template_name, language_code="en_US"):
    """Send one configured Meta WhatsApp template without changing its content."""
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    response = requests.post(
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/"
        f"{phone_number_id}/messages",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": normalize_phone(to_phone),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
            },
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    message_id = ((payload.get("messages") or [{}])[0]).get("id")
    safe_phone = normalize_phone(to_phone)
    print(
        f"WhatsApp sent to ...{safe_phone[-4:]} status={response.status_code} "
        f"message_id={message_id}", flush=True,
    )
    return [message_id]


def download_whatsapp_audio(media_id):
    """Download one Meta-hosted WhatsApp audio object into memory."""
    media_id = str(media_id or "").strip()
    if not media_id:
        raise ValueError("WhatsApp audio media ID is missing.")
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    headers = {"Authorization": f"Bearer {access_token}"}
    metadata_response = requests.get(
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{media_id}",
        headers=headers, timeout=30,
    )
    metadata_response.raise_for_status()
    metadata = metadata_response.json()
    media_url = str(metadata.get("url") or "").strip()
    if not media_url:
        raise ValueError("Meta media metadata did not contain a download URL.")
    audio_response = requests.get(media_url, headers=headers, timeout=60)
    audio_response.raise_for_status()
    audio_bytes = bytes(audio_response.content or b"")
    if not audio_bytes:
        raise ValueError("Downloaded WhatsApp audio was empty.")
    mime_type = str(
        metadata.get("mime_type")
        or audio_response.headers.get("Content-Type")
        or "audio/ogg"
    ).split(";", 1)[0].strip()
    return audio_bytes, mime_type


def transcribe_whatsapp_audio(media_id, message_id=None):
    """Download and transcribe WhatsApp audio without invoking Rentee's chat model."""
    audio_bytes, mime_type = download_whatsapp_audio(media_id)
    print(
        f"[WHATSAPP AUDIO] message_id={message_id or 'unknown'} "
        f"media_downloaded bytes={len(audio_bytes)}", flush=True,
    )
    extension = next((
        suffix for marker, suffix in (
            ("ogg", "ogg"), ("mpeg", "mp3"), ("mp4", "m4a"),
            ("wav", "wav"), ("webm", "webm"),
        ) if marker in mime_type.casefold()
    ), "ogg")
    transcription = client.audio.transcriptions.create(
        model=WHATSAPP_TRANSCRIPTION_MODEL,
        file=(f"whatsapp-voice.{extension}", audio_bytes, mime_type),
    )
    transcript = str(getattr(transcription, "text", "") or "").strip()
    if not transcript:
        raise ValueError("OpenAI returned an empty WhatsApp transcription.")
    print(
        f"[WHATSAPP TRANSCRIPTION] message_id={message_id or 'unknown'} "
        f"chars={len(transcript)}", flush=True,
    )
    return transcript


def persist_owner_check_whatsapp_message(
    phone, message_content, whatsapp_message_id, lead_id=None,
    conversation_id=None, bubble_env="live",
):
    conversation_id = conversation_store.relationship_id(conversation_id)
    if not conversation_id:
        raise ValueError("New outbound WhatsApp Message requires Conversation.")
    payload = {
        "phone": normalize_phone(phone),
        "direction": "Outbound",
        "own_Sent?": "No",
        "messageContent": str(message_content or ""),
        "whatsappMessageId": str(whatsapp_message_id or "").strip(),
    }
    if lead_id:
        payload["lead"] = lead_id
    payload["Conversation"] = conversation_id
    message_id = _bubble_create(get_bubble_base_url(bubble_env), "message", payload)
    conversation_store.update_conversation_last_outbound_at(
        conversation_id, bubble_env
    )
    print(
        f"[MESSAGE] direction=outbound conversation_id={conversation_id} "
        "action=persisted", flush=True,
    )
    return message_id


def _first_whatsapp_message_id(sent_ids):
    if not isinstance(sent_ids, list):
        return None
    return next((str(value).strip() for value in sent_ids if str(value or "").strip()), None)


def send_pending_owner_check(lead, enquiry, bubble_env="live", now=None):
    """Send one durable Pending owner check and mark Sent only after success."""
    enquiry_id = str((enquiry or {}).get("_id") or "").strip()
    if str((enquiry or {}).get("OwnerCheckStatus") or "").strip() != "Pending":
        return False
    phone = normalize_phone((enquiry or {}).get("OwnerCheckPhone"))
    if not 8 <= len(phone) <= 15:
        return False
    listing_id = str((enquiry or {}).get("Listing") or "").strip()
    if not enquiry_id or not listing_id:
        return False
    base_url = get_bubble_base_url(bubble_env)
    try:
        listing = bubble(f"{base_url}/obj/listing/{listing_id}")
        message = build_owner_check_message(enquiry, lead, listing)
        principal_id = conversation_store.relationship_id(
            (enquiry or {}).get("Principal")
        )
        conversation = None
        if principal_id:
            conversation, _conversation_created = (
                conversation_store.find_or_create_conversation(
                    principal_id,
                    phone,
                    enquiry_id=enquiry_id,
                    counterparty_role="Owner Representative",
                    rentee_role="Tenant Introducing Agent",
                    subject=str((listing or {}).get("name") or "").strip() or None,
                    bubble_env=bubble_env,
                    side="owner",
                )
            )
        conversation_id = conversation_store.relationship_id(
            (conversation or {}).get("_id")
        )
        if not conversation_id:
            print(
                f"[CONVERSATION ROUTING] direction=outbound action=unresolved "
                f"reason=missing_principal enquiry_id={enquiry_id}", flush=True,
            )
            return False
        current = now or datetime.datetime.now(datetime.timezone.utc)
        window = owner_check_whatsapp_window(phone, bubble_env, current)
        method = "freeform" if window == "open" else "template"
        print(
            f"[OWNER CHECK SEND] enquiry_id={enquiry_id} "
            "status=pending action=send_attempt", flush=True,
        )
        print(
            f"[OWNER CHECK SEND] enquiry_id={enquiry_id} "
            f"destination=ownerContact phone={_masked_whatsapp_phone(phone)} "
            f"window={window} method={method}", flush=True,
        )
        if method == "freeform":
            sent_ids = send_whatsapp_text(phone, message)
        else:
            template_name = str(
                os.getenv("WHATSAPP_OWNER_CHECK_TEMPLATE_NAME")
                or os.getenv("OWNER_CHECK_TEMPLATE_NAME")
                or ""
            ).strip()
            if not template_name:
                print(
                    f"[OWNER CHECK SEND] enquiry_id={enquiry_id} "
                    "action=send_failed error=missing_template_configuration",
                    flush=True,
                )
                return False
            language = str(
                os.getenv("WHATSAPP_OWNER_CHECK_TEMPLATE_LANGUAGE")
                or os.getenv("OWNER_CHECK_TEMPLATE_LANGUAGE")
                or "en_US"
            ).strip()
            sent_ids = send_whatsapp_template(phone, template_name, language)
        meta_message_id = _first_whatsapp_message_id(sent_ids)
        if meta_message_id:
            try:
                persist_owner_check_whatsapp_message(
                    phone, message, meta_message_id,
                    lead_id=str((lead or {}).get("_id") or "").strip() or None,
                    conversation_id=conversation_id,
                    bubble_env=bubble_env,
                )
            except Exception as error:
                print(
                    f"[OWNER CHECK SEND] enquiry_id={enquiry_id} "
                    "action=message_persistence_failed "
                    f"error={type(error).__name__} preserving_sent_state=true",
                    flush=True,
                )
        else:
            print(
                f"[OWNER CHECK SEND] enquiry_id={enquiry_id} "
                "action=message_persistence_failed "
                "error=missing_meta_message_id preserving_sent_state=true",
                flush=True,
            )
        sent_at = current.isoformat()
        sent_at = sent_at.replace("+00:00", "Z")
        payload = {"OwnerCheckStatus": "Sent", "OwnerCheckSentAt": sent_at}
        _bubble_patch(f"{base_url}/obj/enquiry/{enquiry_id}", payload)
        enquiry.update(payload)
        print(
            f"[OWNER CHECK SEND] enquiry_id={enquiry_id} action=sent", flush=True,
        )
        return True
    except Exception as error:
        print(
            f"[OWNER CHECK SEND] enquiry_id={enquiry_id} "
            f"action=send_failed error={type(error).__name__}", flush=True,
        )
        return False


def _requests_owner_check(answer):
    return " ".join(str(answer or "").split()).endswith(OWNER_CHECK_RESPONSE)


def handle_owner_check_reply(conversation, message_text, bubble_env="live"):
    """Record one reply for an owner-check Conversation awaiting a response."""
    conversation = conversation or {}
    if str(conversation.get("CounterParty Role") or "").strip() != (
        "Owner Representative"
    ):
        return None
    enquiry_id = conversation_store.relationship_id(conversation.get("Enquiry"))
    if not enquiry_id:
        return None
    base_url = get_bubble_base_url(bubble_env)
    enquiry = bubble(f"{base_url}/obj/enquiry/{enquiry_id}")
    if str(enquiry.get("OwnerCheckStatus") or "").strip() != "Sent":
        return None
    _bubble_patch(f"{base_url}/obj/enquiry/{enquiry_id}", {
        "OwnerCheckStatus": "Replied",
        "OwnerCheckResponse": str(message_text),
    })
    print(
        f"[OWNER CHECK REPLY] enquiry_id={enquiry_id} action=recorded",
        flush=True,
    )
    return "Thanks — noted."


def find_forwarded_lead_conversation(lead, phone, bubble_env="live"):
    """Resolve the active enquiry-specific Conversation for a forwarded Lead."""
    lead_id = conversation_store.relationship_id((lead or {}).get("_id"))
    enquiry_id = conversation_store.relationship_id(
        (lead or {}).get("ActiveForwardedEnquiry")
    )
    if not lead_id or not enquiry_id:
        return None
    base_url = get_bubble_base_url(bubble_env)
    enquiry = bubble(f"{base_url}/obj/enquiry/{enquiry_id}")
    if conversation_store.relationship_id(enquiry.get("Lead")) != lead_id:
        return None
    principal_id = conversation_store.relationship_id(enquiry.get("Principal"))
    if not principal_id:
        return None
    representative = str(enquiry.get("Agent?") or "").strip() == "Yes"
    conversation, _created = conversation_store.find_or_create_conversation(
        principal_id,
        phone,
        enquiry_id=enquiry_id,
        counterparty_role=("Lead Representative" if representative else "Lead"),
        rentee_role=(
            "Lead Representative Coordinator" if representative else "Lead Advisor"
        ),
        bubble_env=bubble_env,
        side="lead",
    )
    return conversation


def find_reply_context(message, bubble_env="live"):
    """Resolve Meta reply context to its Conversation and optional exact Listing."""
    replied_to_id = str(((message or {}).get("context") or {}).get("id") or "").strip()
    if not replied_to_id:
        return None
    constraints = [{
        "key": "whatsappMessageId", "constraint_type": "equals",
        "value": replied_to_id,
    }]
    previous = next(iter(_bubble_records(
        get_bubble_base_url(bubble_env), "message", constraints
    )), None)
    conversation_id = conversation_store.relationship_id(
        (previous or {}).get("Conversation")
    )
    if not conversation_id:
        return None
    listing_id = conversation_store.relationship_id(
        (previous or {}).get("listing")
    )
    conversation = bubble(
        f"{get_bubble_base_url(bubble_env)}/obj/conversation/{conversation_id}"
    )
    conversation.setdefault("_id", conversation_id)
    return {"conversation": conversation, "listing_id": listing_id}


def find_reply_to_conversation(message, bubble_env="live"):
    """Backwards-compatible Conversation-only Meta reply resolver."""
    context = find_reply_context(message, bubble_env)
    return context["conversation"] if context else None


def active_conversations_by_phone(phone, bubble_env="live"):
    """Return all exact active phone Conversations without a Principal constraint."""
    canonical_phone = normalize_phone(phone)
    constraints = [
        {"key": "CounterParty Phone", "constraint_type": "equals",
         "value": canonical_phone},
        {"key": "Status", "constraint_type": "equals", "value": "Active"},
    ]
    return [
        item for item in _bubble_records(
            get_bubble_base_url(bubble_env), "conversation", constraints
        )
        if item.get("_id")
        and normalize_phone(item.get("CounterParty Phone")) == canonical_phone
        and str(item.get("Status") or "").strip() == "Active"
    ]


def find_active_conversation_by_phone(
    phone, message_text="", bubble_env="live", include_candidates=False,
):
    """Conservatively resolve a phone-only enquiry Conversation.

    Phone is an identity hint, not a conversation identity.  Keep the single
    enquiry fallback for legacy inbound traffic only when it cannot conflict
    with a general Conversation; never guess between multiple enquiries.
    """
    all_candidates = active_conversations_by_phone(phone, bubble_env)
    result = None
    resolution = "none"
    if len(all_candidates) == 1:
        result, resolution = all_candidates[0], "single_active"
    elif len(all_candidates) > 1:
        resolution = "ambiguous"
    response = (result, resolution, len(all_candidates))
    return response + (all_candidates,) if include_candidates else response


def _has_strong_property_search_clue(message_text, bubble_env="live"):
    """Recognize only explicit property-search criteria, without an LLM guess."""
    text = " ".join(str(message_text or "").casefold().split())
    if not text:
        return False
    explicit_patterns = (
        r"\b\d+(?:\.\d+)?\s*(?:bed|beds|bedroom|bedrooms)\b",
        r"\b(?:rm\s*)?\d+(?:[,.]\d+)*(?:\s*k)?\b.*\b(?:budget|rent|month)\b",
        r"\b\d+(?:\.\d+)?\s*k\b",
        r"\b(?:fully|partially|semi|un)\s*-?furnished\b|\bfurnished\s+only\b",
        r"\b(?:pet[- ]?friendly|pets? allowed|with (?:a )?pet)\b",
        r"\b(?:landed|condo(?:minium)?|apartment|bungalow|townhouse|terrace)\b",
        r"\b(?:balcony|cheaper|lower budget)\b",
    )
    if any(re.search(pattern, text) for pattern in explicit_patterns):
        return True
    try:
        valid_geo_names = get_valid_geo_names(bubble_env)
    except Exception:
        valid_geo_names = []
    padded_text = f" {re.sub(r'[^a-z0-9]+', ' ', text).strip()} "
    return any(
        f" {re.sub(r'[^a-z0-9]+', ' ', str(name).casefold()).strip()} "
        in padded_text
        for name in valid_geo_names
        if str(name).strip()
    )


def _property_search_conversation(conversation):
    """A general Lead/search Conversation, never an enquiry/owner workflow."""
    conversation = conversation or {}
    role = str(conversation.get("CounterParty Role") or "").strip()
    return (
        not conversation_store.relationship_id(conversation.get("Enquiry"))
        and role != "Owner Representative"
    )


def route_conversation_by_message_clue(
    message_text, candidates, bubble_env="live",
):
    """Filter ambiguous candidates only for high-confidence workflow clues."""
    candidates = [item for item in candidates or [] if item.get("_id")]
    if not _has_strong_property_search_clue(message_text, bubble_env):
        return None, None, candidates
    compatible = [item for item in candidates if _property_search_conversation(item)]
    selected = compatible[0] if len(compatible) == 1 else None
    return selected, "property_search", compatible or candidates


def _conversation_property_name(conversation, bubble_env="live"):
    subject = " ".join(str((conversation or {}).get("Subject") or "").split())
    if subject:
        return subject
    listing_id = conversation_store.relationship_id(
        (conversation or {}).get("Listing")
    )
    if not listing_id:
        return None
    try:
        listing = bubble(
            f"{get_bubble_base_url(bubble_env)}/obj/listing/{listing_id}"
        )
    except Exception:
        return None
    for field in ("name", "title", "listingName", "condoName"):
        value = " ".join(str(listing.get(field) or "").split())
        if value:
            return value
    condo_id = conversation_store.relationship_id(listing.get("condo"))
    if condo_id:
        try:
            condo = bubble(
                f"{get_bubble_base_url(bubble_env)}/obj/condo/{condo_id}"
            )
            for field in ("name", "Name", "Condo name"):
                value = " ".join(str(condo.get(field) or "").split())
                if value:
                    return value
        except Exception:
            pass
    return None


def _conversation_search_details(conversation, bubble_env="live"):
    subject = " ".join(str((conversation or {}).get("Subject") or "").split())
    if subject:
        return None, subject
    lead_id = conversation_store.relationship_id((conversation or {}).get("Lead"))
    if not lead_id:
        return None, None
    try:
        lead = bubble(f"{get_bubble_base_url(bubble_env)}/obj/lead/{lead_id}")
    except Exception:
        return None, None
    bedrooms = lead.get("bedroomsMin")
    areas = []
    raw_brief = lead.get("searchBriefJSON")
    if isinstance(raw_brief, str) and raw_brief.strip():
        try:
            brief = json.loads(raw_brief)
        except (TypeError, ValueError):
            brief = {}
        bedrooms = bedrooms or brief.get("bedrooms_min") or brief.get("bedroomsMin")
        areas = (
            brief.get("geo_names") or brief.get("areas")
            or brief.get("active_areas") or []
        )
    if isinstance(areas, str):
        areas = [areas]
    area = next((" ".join(str(item).split()) for item in areas if str(item).strip()), None)
    return bedrooms, area


def conversation_display_label(conversation, bubble_env="live"):
    """Return a deterministic, customer-facing label for one Conversation."""
    conversation = conversation or {}
    property_name = _conversation_property_name(conversation, bubble_env)
    role = str(conversation.get("CounterParty Role") or "").strip()
    enquiry_id = conversation_store.relationship_id(conversation.get("Enquiry"))
    if role == "Owner Representative" and enquiry_id:
        return (
            f"the tenant profile for {property_name}" if property_name
            else "a tenant profile you were reviewing"
        )
    if enquiry_id:
        return (
            f"the enquiry about {property_name}" if property_name
            else "an earlier property enquiry"
        )
    bedrooms, area = _conversation_search_details(conversation, bubble_env)
    if area and bedrooms:
        return f"your {bedrooms}-bedroom search in {area}"
    if area:
        return f"your {area} property search"
    return "your earlier property conversation"


def build_conversation_ambiguity_message(candidates, bubble_env="live"):
    """Build a short clarification, never exposing or guessing internal IDs."""
    ranked = sorted(
        (item for item in candidates if item.get("_id")),
        key=lambda item: (
            str(item.get("CounterParty Role") or "").strip()
            == "Owner Representative",
            bool(conversation_store.relationship_id(item.get("Enquiry"))),
            bool(item.get("Subject")),
            bool(conversation_store.relationship_id(item.get("Listing"))),
            str(item.get("Last Inbound At") or item.get("Last Outbound At")
                or item.get("Created Date") or ""),
            str(item.get("_id")),
        ),
        reverse=True,
    )[:3]
    labels = [
        " ".join(str(conversation_display_label(item, bubble_env) or "").split())
        for item in ranked
    ]
    usable = [(item, label) for item, label in zip(ranked, labels) if label]
    ranked = [item for item, _label in usable]
    labels = [label for _item, label in usable]
    fallback = (
        "I have a couple of property conversations with you open. "
        "Could you reply to the message you mean, or tell me which "
        "property or search this is about?"
    )
    if len(labels) < 2:
        return fallback, []
    if len(set(labels)) != len(labels):
        enriched = []
        for item, label in zip(ranked, labels):
            raw_date = (
                item.get("Last Inbound At") or item.get("Last Outbound At")
                or item.get("Created Date")
            )
            created_at = _bubble_created_at(raw_date)
            enriched.append(
                f"{label} from {created_at.strftime('%-d %b')}"
                if created_at else label
            )
        if len(set(enriched)) == len(enriched):
            labels = enriched
        else:
            return fallback, []
    labels = list(dict.fromkeys(label.strip() for label in labels if label.strip()))
    if len(labels) < 2:
        return fallback, []
    if len(labels) == 2:
        message = f"Just checking — do you mean {labels[0]}, or {labels[1]}?"
    else:
        message = (
            "Just checking — which do you mean: "
            + ", ".join(labels[:-1]) + f", or {labels[-1]}?"
        )
    return message, labels


def conversation_has_outbound_message(conversation_id, bubble_env="live"):
    """Use existing durable Message history as the one-time onboarding marker."""
    conversation_id = conversation_store.relationship_id(conversation_id)
    if not conversation_id:
        return False
    constraints = [
        {"key": "Conversation", "constraint_type": "equals",
         "value": conversation_id},
        {"key": "direction", "constraint_type": "equals", "value": "Outbound"},
    ]
    return next(iter(_bubble_records(
        get_bubble_base_url(bubble_env), "message", constraints
    )), None) is not None


def _profile_relationship_names(lead, field, object_type, bubble_env="live"):
    ids = list((lead or {}).get(field) or [])
    if not ids:
        return []
    names = get_relationship_names(
        get_bubble_base_url(bubble_env), object_type, ids
    )
    return [names.get(str(value)) for value in ids if names.get(str(value))]


def build_prefilled_search_confirmation(lead, bubble_env="live"):
    """Summarize useful agent-entered search facts without internal field names."""
    lead = lead or {}
    transaction_values = " ".join(lead.get("TransactionType") or []).casefold()
    transaction = (
        "rental" if "rent" in transaction_values else
        "property to buy" if "sale" in transaction_values or "purchase" in transaction_values
        else "property"
    )
    bedrooms = _as_number(lead.get("bedroomsMin"))
    budget = _as_number(
        lead.get("budgetRent") if transaction == "rental" else lead.get("budgetBuy")
    )
    areas = _profile_relationship_names(lead, "Geo", "geo", bubble_env)
    condos = _profile_relationship_names(
        lead, "preferredCondos", "condo", bubble_env
    )
    details = []
    if bedrooms is not None:
        rendered_beds = int(bedrooms) if bedrooms.is_integer() else bedrooms
        details.append(f"a {rendered_beds}-bedroom {transaction}")
    elif transaction_values:
        details.append(f"a {transaction}")
    if areas:
        details.append("around " + " or ".join(areas[:3]))
    if budget is not None:
        suffix = "/month" if transaction == "rental" else ""
        details.append(f"up to RM{budget:,.0f}{suffix}")
    furnishing = " ".join(str(lead.get("furnishingPreference") or "").split())
    if furnishing:
        details.append(furnishing.casefold())
    if condos:
        details.append("with interest in " + " or ".join(condos[:2]))
    adults = _as_number(lead.get("adults"))
    children = _as_number(lead.get("children"))
    household = []
    if adults is not None:
        household.append(f"{int(adults)} adult" + ("s" if adults != 1 else ""))
    if children is not None:
        household.append(f"{int(children)} child" + ("ren" if children != 1 else ""))
    if household:
        details.append("for " + " and ".join(household))
    pets = " ".join(str(lead.get("pets") or "").split())
    if pets and pets.casefold() not in {"no", "none", "no pets"}:
        details.append("with " + pets)
    start_date = str(lead.get("startDate") or "").strip()
    if start_date:
        try:
            parsed = datetime.datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            details.append(f"moving in {parsed.strftime('%B')}")
        except ValueError:
            pass
    if not details:
        return None
    return "I understand you're looking for " + ", ".join(details) + ". Is that still right?"


def find_general_conversation(
    principal_id, phone, lead_id=None, counterparty_user_id=None,
    counterparty_role=None, rentee_role=None, bubble_env="live",
):
    """Find/create the safe non-enquiry Conversation for one counterparty."""
    principal_id = conversation_store.relationship_id(principal_id)
    if not principal_id:
        return None
    conversation, _created = conversation_store.find_or_create_conversation(
        principal_id, phone,
        counterparty_user_id=counterparty_user_id,
        counterparty_role=counterparty_role,
        rentee_role=rentee_role,
        bubble_env=bubble_env, side="general", lead_id=lead_id,
    )
    return conversation


def find_existing_general_conversation_by_phone(phone, bubble_env="live"):
    """Reuse a general Conversation only when phone lookup is unambiguous."""
    constraints = [
        {"key": "CounterParty Phone", "constraint_type": "equals",
         "value": normalize_phone(phone)},
        {"key": "Status", "constraint_type": "equals", "value": "Active"},
    ]
    candidates = [
        item for item in _bubble_records(
            get_bubble_base_url(bubble_env), "conversation", constraints
        )
        if not conversation_store.relationship_id(item.get("Enquiry"))
        and conversation_store.relationship_id(item.get("Principal"))
        and item.get("_id")
    ]
    return candidates[0] if len(candidates) == 1 else None


def persist_sent_whatsapp_text(
    phone, text, sent_ids, conversation_id, lead_id=None, bubble_env="live",
):
    """Persist one successfully sent non-streaming WhatsApp text."""
    conversation_id = conversation_store.relationship_id(conversation_id)
    if not conversation_id:
        raise ValueError("New outbound WhatsApp Message requires Conversation.")
    meta_message_id = _first_whatsapp_message_id(sent_ids)
    if not meta_message_id:
        raise ValueError("Successful WhatsApp send did not return a message ID.")
    payload = {
        "phone": normalize_phone(phone),
        "direction": "Outbound",
        "own_Sent?": "No",
        "messageContent": str(text or ""),
        "messageType": "text",
        "image_URL": "",
        "whatsappMessageId": meta_message_id,
        "Conversation": conversation_id,
    }
    if lead_id:
        payload["lead"] = lead_id
    message_id = _bubble_create(
        get_bubble_base_url(bubble_env), "message", payload
    )
    conversation_store.update_conversation_last_outbound_at(
        conversation_id, bubble_env
    )
    print(
        f"[MESSAGE] direction=outbound conversation_id={conversation_id} "
        "action=persisted", flush=True,
    )
    return message_id


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
        and str(message.get("direction") or "Outbound").strip().casefold()
        == "outbound"
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


def persist_inbound_whatsapp_message(
    phone, whatsapp_message_id, message_content, lead_id=None,
    conversation_id=None, bubble_env="live", listing_id=None,
):
    """Create one durable Bubble Message for an inbound Meta message."""
    base_url = get_bubble_base_url(bubble_env)
    meta_id = str(whatsapp_message_id or "").strip()
    constraints = [{
        "key": "whatsappMessageId", "constraint_type": "equals", "value": meta_id,
    }]
    existing = next(iter(_bubble_records(base_url, "message", constraints)), None)
    if existing and existing.get("_id"):
        message_id = existing["_id"]
        patch = {}
        if lead_id and str(existing.get("lead") or "") != str(lead_id):
            patch["lead"] = lead_id
        if conversation_id and not conversation_store.relationship_id(
            existing.get("Conversation")
        ):
            patch["Conversation"] = conversation_id
        existing_listing_id = conversation_store.relationship_id(
            existing.get("listing")
        )
        if listing_id and not existing_listing_id:
            patch["listing"] = listing_id
        elif listing_id and existing_listing_id != str(listing_id):
            print(
                "[MESSAGE] direction=inbound action=listing_conflict "
                f"existing_listing_id={existing_listing_id} "
                f"reply_listing_id={listing_id}", flush=True,
            )
        if patch:
            _bubble_patch(
                f"{base_url}/obj/message/{message_id}", patch
            )
        if conversation_id:
            conversation_store.update_conversation_last_inbound_at(
                conversation_id, bubble_env
            )
        return message_id, False
    conversation_id = conversation_store.relationship_id(conversation_id)
    if not conversation_id:
        raise ValueError("New inbound WhatsApp Message requires Conversation.")
    payload = {
        "phone": normalize_phone(phone),
        "direction": "Inbound",
        "own_Sent?": "Yes",
        "whatsappMessageId": meta_id,
        "messageContent": str(message_content or ""),
    }
    if lead_id:
        payload["lead"] = lead_id
    payload["Conversation"] = conversation_id
    if listing_id:
        payload["listing"] = listing_id
    message_id = _bubble_create(base_url, "message", payload)
    conversation_store.update_conversation_last_inbound_at(
        conversation_id, bubble_env
    )
    print(
        f"[MESSAGE] direction=inbound conversation_id={conversation_id} "
        "action=persisted", flush=True,
    )
    return message_id, True


def attach_whatsapp_message_lead(message_id, lead_id, bubble_env="live"):
    if message_id and lead_id:
        _bubble_patch(
            f"{get_bubble_base_url(bubble_env)}/obj/message/{message_id}",
            {"lead": lead_id},
        )


def create_whatsapp_ai_message(
    lead_id, phone=None, bubble_env="live", conversation_id=None,
):
    conversation_id = conversation_store.relationship_id(conversation_id)
    if not conversation_id:
        raise ValueError("New AI WhatsApp Message requires Conversation.")
    payload = {
        "lead": lead_id,
        "phone": normalize_phone(phone),
        "direction": "Outbound",
        "own_Sent?": "No",
        "messageContent": "",
        "messageType": "text",
        "image_URL": "",
    }
    payload["Conversation"] = conversation_id
    return _bubble_create(get_bubble_base_url(bubble_env), "message", payload)


def save_whatsapp_ai_message(message_id, answer, response_id, bubble_env="live"):
    response = requests.patch(
        f"{get_bubble_base_url(bubble_env)}/obj/message/{message_id}",
        headers=_bubble_headers(),
        json={"messageContent": answer, "response_ID": response_id},
        timeout=30,
    )
    response.raise_for_status()


def save_whatsapp_message_id(
    message_id, whatsapp_message_id, bubble_env="live", conversation_id=None,
):
    meta_id = str(whatsapp_message_id or "").strip()
    if not message_id or not meta_id:
        return
    _bubble_patch(
        f"{get_bubble_base_url(bubble_env)}/obj/message/{message_id}",
        {"whatsappMessageId": meta_id},
    )
    if conversation_id:
        conversation_store.update_conversation_last_outbound_at(
            conversation_id, bubble_env
        )


def persist_recommendation_whatsapp_ids(
    message_id, deliveries, phone, conversation_id, lead_id=None,
    bubble_env="live",
):
    """Persist one Bubble Message matching each actual WhatsApp delivery."""
    conversation_id = conversation_store.relationship_id(conversation_id)
    if not conversation_id:
        raise ValueError("Recommendation messages require a Conversation.")
    unique_deliveries = []
    seen_ids = set()
    for delivery in deliveries or []:
        if not isinstance(delivery, dict):
            continue
        meta_id = str(delivery.get("whatsapp_message_id") or "").strip()
        if not meta_id or meta_id in seen_ids:
            continue
        seen_ids.add(meta_id)
        unique_deliveries.append(dict(delivery, whatsapp_message_id=meta_id))
    persisted = []
    for index, delivery in enumerate(unique_deliveries, start=1):
        meta_id = delivery["whatsapp_message_id"]
        message_type = (
            "image" if delivery.get("message_type") == "image" else "text"
        )
        payload = {
            "phone": normalize_phone(phone),
            "direction": "Outbound",
            "own_Sent?": "No",
            "messageType": message_type,
            "messageContent": (
                "" if message_type == "image"
                else str(delivery.get("message_content") or "")
            ),
            "image_URL": (
                str(delivery.get("image_url") or "")
                if message_type == "image" else ""
            ),
            "whatsappMessageId": meta_id,
            "Conversation": conversation_id,
        }
        if lead_id:
            payload["lead"] = lead_id
        listing_id = conversation_store.relationship_id(
            delivery.get("listing_id")
        )
        if listing_id:
            payload["listing"] = listing_id
        try:
            if index == 1:
                _bubble_patch(
                    f"{get_bubble_base_url(bubble_env)}/obj/message/{message_id}",
                    payload,
                )
                conversation_store.update_conversation_last_outbound_at(
                    conversation_id, bubble_env
                )
                persisted_message_id = message_id
            else:
                persisted_message_id = _bubble_create(
                    get_bubble_base_url(bubble_env), "message", payload
                )
            persisted.append(persisted_message_id)
            print(
                "[WHATSAPP MESSAGE] direction=outbound "
                f"recommendation_index={index} "
                f"conversation_id={conversation_id} "
                f"listing_id={listing_id or 'none'} action=persisted",
                flush=True,
            )
        except Exception as error:
            print(
                "[WHATSAPP MESSAGE] direction=outbound "
                f"recommendation_index={index} "
                f"conversation_id={conversation_id} action=persist_failed "
                f"error={type(error).__name__}",
                flush=True,
            )
    return persisted


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


def normalize_image_for_whatsapp(image_bytes):
    """Apply EXIF orientation to pixels and return a fresh JPEG payload."""
    with Image.open(io.BytesIO(bytes(image_bytes or b""))) as source:
        orientation = source.getexif().get(274, 1)
        normalized = ImageOps.exif_transpose(source)
        if normalized.mode not in ("RGB", "L"):
            normalized = normalized.convert("RGB")
        output = io.BytesIO()
        normalized.save(output, format="JPEG", quality=88, optimize=True)
    action = "normalized" if orientation not in (None, 1) else "unchanged"
    print(
        f"[WHATSAPP IMAGE] action={action} exif_orientation={orientation or 1}",
        flush=True,
    )
    return output.getvalue()


def _send_whatsapp_image_link(to_phone, image_url):
    """Existing public-link send retained as the safe media fallback."""
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    response = requests.post(
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/messages",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp", "to": normalize_phone(to_phone),
            "type": "image", "image": {"link": image_url},
        },
        timeout=30,
    )
    response.raise_for_status()
    return response


def send_whatsapp_image(to_phone, image_url):
    """Normalize a remote image, upload its bytes, then send the Meta media ID."""
    phone_number_id = os.environ["WHATSAPP_PHONE_NUMBER_ID"]
    access_token = os.environ["WHATSAPP_ACCESS_TOKEN"]
    try:
        source_response = requests.get(image_url, timeout=30)
        source_response.raise_for_status()
        normalized_bytes = normalize_image_for_whatsapp(source_response.content)
        upload_response = requests.post(
            f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/media",
            headers={"Authorization": f"Bearer {access_token}"},
            data={"messaging_product": "whatsapp", "type": "image/jpeg"},
            files={"file": ("listing.jpg", normalized_bytes, "image/jpeg")},
            timeout=30,
        )
        upload_response.raise_for_status()
        media_id = str((upload_response.json() or {}).get("id") or "").strip()
        if not media_id:
            raise ValueError("Meta media upload did not return an ID.")
    except Exception as error:
        print(
            "[WHATSAPP IMAGE] action=normalization_failed "
            f"fallback=existing_behavior error={type(error).__name__}",
            flush=True,
        )
        return _send_whatsapp_image_link(to_phone, image_url)
    response = requests.post(
        f"https://graph.facebook.com/{WHATSAPP_GRAPH_API_VERSION}/{phone_number_id}/messages",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json={
            "messaging_product": "whatsapp",
            "to": normalize_phone(to_phone),
            "type": "image",
            "image": {"id": media_id},
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


class WhatsAppTypingKeepalive:
    """Best-effort, per-message lifecycle for Meta's expiring typing state."""

    def __init__(self, message_id, refresh_seconds=WHATSAPP_TYPING_REFRESH_SECONDS):
        self.message_id = str(message_id)
        self.refresh_seconds = refresh_seconds
        self.stop_event = threading.Event()
        self.request_lock = threading.Lock()
        self.thread = None
        self.thread_started = False
        self._stopped = False

    def _send(self, action):
        try:
            send_whatsapp_typing_indicator(self.message_id)
            print(f"WA typing {action} message_id={self.message_id}", flush=True)
        except Exception as error:
            print(
                f"WA typing {action} failed message_id={self.message_id} "
                f"error={type(error).__name__}", flush=True,
            )

    def _run(self):
        while not self.stop_event.wait(self.refresh_seconds):
            with self.request_lock:
                if self.stop_event.is_set():
                    break
                self._send("refreshed")

    def start(self):
        with self.request_lock:
            self._send("started")
        try:
            self.thread = WHATSAPP_TYPING_THREAD_FACTORY(
                target=self._run, daemon=True,
                name=f"whatsapp-typing-{self.message_id[-12:]}",
            )
            self.thread.start()
            self.thread_started = True
        except Exception as error:
            print(
                f"WA typing keepalive start failed message_id={self.message_id} "
                f"error={type(error).__name__}", flush=True,
            )
        return self

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        # Wait for any refresh already inside the request boundary. Once this lock
        # is acquired, no typing request can race a subsequent customer reply.
        with self.request_lock:
            pass
        if (self.thread_started and self.thread
                and self.thread is not threading.current_thread()):
            self.thread.join()
        print(f"WA typing stopped message_id={self.message_id}", flush=True)


def whatsapp_typing_keepalive(
    message_id, refresh_seconds=WHATSAPP_TYPING_REFRESH_SECONDS,
):
    return WhatsAppTypingKeepalive(message_id, refresh_seconds).start()


def _stop_whatsapp_typing(keepalive):
    if keepalive:
        keepalive.stop()


def build_listing_bubble_constraints(requirements, condo_ids=None):
    """Build safe Bubble query variants from the existing structured requirements."""
    base_constraints = []
    python_only_filters = []
    bedrooms_min = requirements.get("bedrooms_min")
    if bedrooms_min is not None:
        # Bubble exposes strict numeric comparison; subtracting a tiny epsilon preserves
        # the existing inclusive bedrooms_min semantic.
        base_constraints.append({
            "key": "beds", "constraint_type": "greater than",
            "value": bedrooms_min - 0.000001,
        })

    modes = _transaction_modes(requirements.get("transaction_type"))
    budget_rent = requirements.get("budget_rent")
    budget_buy = requirements.get("budget_buy")
    transaction_groups = [[]]
    if modes:
        transaction_groups = []
        if "rent" in modes:
            transaction_groups.append([
                {"key": "TransactionType", "constraint_type": "contains",
                 "value": RENT_TRANSACTION},
                {"key": "priceRent", "constraint_type": "is_not_empty"},
            ])
        if "buy" in modes:
            transaction_groups.append([
                {"key": "TransactionType", "constraint_type": "contains",
                 "value": BUY_TRANSACTION},
                {"key": "priceSale", "constraint_type": "is_not_empty"},
            ])
    if (budget_rent and "rent" in modes) or (budget_buy and "buy" in modes):
        python_only_filters.append("budget")
    if requirements.get("furnishing_preference"):
        python_only_filters.append("furnishing")

    resolved_condo_ids = _unique_search_values(
        condo_ids or requirements.get("preferred_condo_ids") or []
    )
    geo_ids = _unique_search_values(requirements.get("geo_ids") or [])
    scope_field = None
    scope_ids = []
    if resolved_condo_ids:
        scope_field, scope_ids = "condo", resolved_condo_ids
    elif geo_ids:
        scope_field, scope_ids = "Geo", geo_ids
    queries = []
    location_groups = [[]]
    if scope_ids:
        location_groups = [
            [{
                "key": scope_field,
                "constraint_type": "contains" if scope_field == "Geo" else "equals",
                "value": scope_id,
            }]
            for scope_id in scope_ids
        ]
    queries = [
        base_constraints + transaction_group + location_group
        for transaction_group in transaction_groups
        for location_group in location_groups
    ]
    return {
        "queries": queries,
        "scope_field": scope_field,
        "scope_ids": scope_ids,
        "python_only_filters": python_only_filters,
    }


def get_plausible_listings(
    base_url, lead, condo_scope=None, target=RETRIEVAL_CANDIDATE_TARGET
):
    """Query Bubble narrowly, then retain Python filtering as validation."""
    started = time.perf_counter()
    plausible = []
    filter_counts = {
        key: 0 for key in (
            "fetched", "after_transaction", "after_condo", "after_bedrooms",
            "after_budget", "after_geo", "plausible", "rejected_budget",
        )
    }
    pages = 0
    fetched = 0
    seen_listing_ids = set()
    condo_cache = {}
    requirements = structured_lead_requirements(lead)
    resolved_condo_ids = []
    condo_resolution_failed = False
    if condo_scope:
        resolved_condo_ids = get_named_object_ids(base_url, "condo", condo_scope)
        condo_resolution_failed = len(resolved_condo_ids) != len(
            _unique_search_values(condo_scope)
        )
        if condo_resolution_failed:
            print(
                "[LISTING QUERY] fallback=full_scan reason=condo_resolution_failed",
                flush=True,
            )
            resolved_condo_ids = []
            requirements = dict(requirements)
            requirements["preferred_condo_ids"] = []
    plan = build_listing_bubble_constraints(requirements, resolved_condo_ids)
    validation_lead = dict(lead)
    if resolved_condo_ids:
        validation_lead["preferredCondos"] = resolved_condo_ids
        validation_lead["Geo"] = []

    print(
        "[LISTING QUERY] transaction="
        f"{sorted(_transaction_modes(requirements['transaction_type']))}",
        flush=True,
    )
    print(
        "[LISTING QUERY] transaction_values="
        f"{bubble_transaction_values(requirements['transaction_type'])!r}",
        flush=True,
    )
    modes = _transaction_modes(requirements["transaction_type"])
    price_field = (
        "priceSale" if modes == {"buy"} else
        "priceRent" if modes == {"rent"} else None
    )
    print(f"[LISTING QUERY] price_field={price_field}", flush=True)
    print(f"[LISTING QUERY] condo_scope={list(condo_scope or [])!r}", flush=True)
    print(f"[LISTING QUERY] geo_scope={requirements['geo_ids']!r}", flush=True)
    print(f"[LISTING QUERY] resolved_condo_ids={resolved_condo_ids!r}", flush=True)
    print(
        f"[LISTING QUERY] resolved_geo_ids="
        f"{plan['scope_ids'] if plan['scope_field'] == 'Geo' else []!r}",
        flush=True,
    )
    print(f"[LISTING QUERY] bedrooms_min={requirements['bedrooms_min']!r}", flush=True)
    print(
        f"[LISTING QUERY] budget_rent={requirements['budget_rent']!r} "
        f"budget_buy={requirements['budget_buy']!r}", flush=True,
    )
    print(f"[LISTING QUERY] bubble_constraints={plan['queries']!r}", flush=True)
    print(f"[LISTING QUERY] python_only_filters={plan['python_only_filters']!r}", flush=True)
    print("[LISTING QUERY] mode=structured_bubble_query", flush=True)

    for constraints in plan["queries"]:
        cursor = 0
        seen_cursors = set()
        while cursor not in seen_cursors and (cursor == 0 or len(plausible) < target):
            seen_cursors.add(cursor)
            params = {"cursor": cursor}
            if constraints:
                params["constraints"] = json.dumps(constraints, separators=(",", ":"))
            page = bubble(f"{base_url}/obj/listing", params=params)
            raw_results = page.get("results", []) or []
            pages += 1
            fetched += len(raw_results)
            results = []
            for listing in raw_results:
                listing_id = str(listing.get("_id") or "")
                if listing_id and listing_id in seen_listing_ids:
                    continue
                if listing_id:
                    seen_listing_ids.add(listing_id)
                results.append(listing)
            if condo_scope and condo_resolution_failed:
                results = [
                    listing for listing in results
                    if _listing_is_in_condo_scope(
                        listing, condo_scope, base_url, condo_cache
                    )
                ]
            page_plausible, page_counts = shortlist_structured_listings(
                validation_lead, results, return_counts=True
            )
            plausible.extend(page_plausible)
            for key in filter_counts:
                filter_counts[key] += page_counts[key]
            if not raw_results or not page.get("remaining"):
                break
            cursor += len(raw_results)
    print(f"[LISTING QUERY] pages={pages} fetched={fetched}", flush=True)
    print(
        "[LISTING FILTER] "
        f"fetched={filter_counts['fetched']} "
        f"after_transaction={filter_counts['after_transaction']} "
        f"after_condo={filter_counts['after_condo']} "
        f"after_bedrooms={filter_counts['after_bedrooms']} "
        f"after_budget={filter_counts['after_budget']} "
        f"rejected_budget={filter_counts['rejected_budget']} "
        f"after_geo={filter_counts['after_geo']} "
        f"plausible={filter_counts['plausible']} "
        f"budget_rent={requirements['budget_rent']!r} "
        f"budget_buy={requirements['budget_buy']!r}",
        flush=True,
    )
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
    property_word = "property" if total == 1 else "properties"
    top_verb = "is" if len(shown) == 1 else "are"
    intro = (
        f"I've shortlisted {total} {property_word} for you. "
        f"My top {len(shown)} {top_verb}:"
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
    footer = f"{link_intro}\n{build_folio_url(folio_id)}"
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
    deliveries = []

    def record_text(text, sent_ids, listing_id=None):
        parts = split_whatsapp_text(text)
        for index, meta_id in enumerate(sent_ids or []):
            if not meta_id:
                continue
            deliveries.append({
                "whatsapp_message_id": meta_id,
                "message_type": "text",
                "message_content": parts[index] if index < len(parts) else str(text),
                "listing_id": listing_id or None,
            })

    intro_ids = send_whatsapp_text(to_phone, intro)
    if isinstance(intro_ids, list):
        record_text(intro, intro_ids)
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
                try:
                    image_payload = response.json()
                    image_message_id = None
                    if isinstance(image_payload, dict):
                        image_message_id = (
                            (image_payload.get("messages") or [{}])[0].get("id")
                        )
                    if image_message_id:
                        deliveries.append({
                            "whatsapp_message_id": image_message_id,
                            "message_type": "image",
                            "image_url": image_url,
                            "listing_id": listing_id or None,
                        })
                except (AttributeError, TypeError, ValueError, IndexError):
                    pass
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
        card_ids = send_whatsapp_text(to_phone, card)
        if isinstance(card_ids, list):
            record_text(card, card_ids, listing_id or None)
    footer_ids = send_whatsapp_text(to_phone, footer)
    if isinstance(footer_ids, list):
        record_text(footer, footer_ids)
    return deliveries


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
        "furnishing_preference": lead.get("furnishingPreference"),
        "pets": lead.get("pets"),
        "start_date": lead.get("startDate"),
        "adults": _as_number(lead.get("adults")),
        "children": _as_number(lead.get("children")),
        "helpers": _as_number(lead.get("helpers")),
        "viewing_preference": lead.get("viewingPreference"),
    }


def known_tenant_profile_context(lead):
    """Return Gwen-supplied free text for ranking context, never hard filters."""
    value = " ".join(str((lead or {}).get("LeadDetailsShare") or "").split())
    return value or None


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
        "propertyType", "TransactionType", "condo", "Geo", "Address", "address", "Location",
        "latitude", "Latitude", "lat", "Lat", "longitude", "Longitude",
        "lng", "Lng", "lon", "Lon",
        "Furnishing", "furnished",
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
    if "both" in rendered:
        modes.update(("rent", "buy"))
    if "rent" in rendered or "let" in rendered:
        modes.add("rent")
    if "buy" in rendered or "sale" in rendered or "purchas" in rendered:
        modes.add("buy")
    return modes


def bubble_transaction_values(value):
    """Serialize normalized transaction intent to valid Bubble option values."""
    modes = _transaction_modes(value)
    return [
        option for mode, option in (
            ("rent", RENT_TRANSACTION), ("buy", BUY_TRANSACTION)
        )
        if mode in modes
    ]


def shortlist_structured_listings(lead, listings, return_counts=False):
    """Remove obvious mismatches while leaving trade-off judgement to the model."""
    requirements = structured_lead_requirements(lead)
    modes = _transaction_modes(requirements["transaction_type"])
    bedrooms_min = requirements["bedrooms_min"]
    geo_ids = {str(item) for item in requirements["geo_ids"]}
    condo_ids = {str(item) for item in requirements["preferred_condo_ids"]}
    budget_rent = requirements["budget_rent"]
    budget_buy = requirements["budget_buy"]
    shortlisted = []
    counts = {
        "fetched": len(listings), "after_transaction": 0, "after_condo": 0,
        "after_bedrooms": 0, "after_budget": 0, "after_geo": 0,
    }

    for listing in listings:
        rent = _as_number(listing.get("priceRent"))
        sale = _as_number(listing.get("priceSale"))
        listing_modes = _transaction_modes(listing.get("TransactionType") or [])
        if modes and not modes.intersection(listing_modes):
            continue
        if modes == {"rent"} and rent is None:
            continue
        if modes == {"buy"} and sale is None:
            continue
        counts["after_transaction"] += 1
        if condo_ids and str(listing.get("condo")) not in condo_ids:
            continue
        counts["after_condo"] += 1
        beds = _as_number(listing.get("beds"))
        if bedrooms_min is not None and beds is not None and beds < bedrooms_min:
            continue
        counts["after_bedrooms"] += 1
        if budget_rent and "buy" not in modes and rent:
            if rent > budget_rent * 1.2 or rent < budget_rent * 0.45:
                continue
        if budget_buy and "rent" not in modes and sale:
            # Purchase budget is a hard maximum. Cheaper eligible homes remain in
            # the pool; closeness to the spending tier is handled by ranking.
            if sale > budget_buy:
                continue
        counts["after_budget"] += 1
        listing_geos = listing.get("Geo") or []
        if not isinstance(listing_geos, list):
            listing_geos = [listing_geos]
        if geo_ids and not condo_ids and not geo_ids.intersection(str(item) for item in listing_geos):
            continue
        counts["after_geo"] += 1
        shortlisted.append(listing)
    if return_counts:
        counts["plausible"] = len(shortlisted)
        counts["rejected_budget"] = counts["after_bedrooms"] - counts["after_budget"]
        return shortlisted, counts
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
    plausible_listing_count = len(listings)
    listings = reduce_listing_candidates(search_lead, listings)
    print(
        "Listing candidates: "
        f"fetched={fetched_listing_count} "
        f"plausible={plausible_listing_count} "
        f"sent_to_ranking={len(listings)}",
        flush=True,
    )
    log_timing("match_lead - load listings", listings_started)

    if not listings:
        log_timing("match_lead TOTAL", match_started)
        return (
            "I don't have a suitable current property match for that search at the moment."
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
        "structured_search_requirements": structured_lead_requirements(search_lead),
        "available_listings": grounded_listing_facts,
    }
    tenant_profile_context = known_tenant_profile_context(search_lead)
    if tenant_profile_context:
        matching_input["known_tenant_profile"] = tenant_profile_context
    prompt = (
        "Rank the grounded listings for this customer. Treat "
        "structured_search_requirements as the authoritative search criteria. If "
        "known_tenant_profile is present, use it only as Gwen-supplied suitability "
        "context and for grounded trade-offs; do not turn it into hard retrieval "
        "criteria or let it override the structured search requirements. Use the "
        "structured listing facts and make sensible trade-offs, including fit with the "
        "customer's budget tier. The supplied available_listings have already passed "
        "structured retrieval and are plausible candidates. Your primary job is to rank "
        "and select the strongest candidates, not to independently re-run retrieval. If "
        "one or more candidates are reasonably suitable, you MUST return recommendations. "
        "Return an empty recommendations array only when every candidate has a concrete, "
        "material mismatch that makes it unsuitable. Do not reject every candidate merely "
        "because none is perfect. Prefer the strongest 3 to 7 available options when there "
        "are plausible candidates, make sensible trade-offs, and state compromises in each "
        "reco_summary. Keep each reco_summary concise. Never invent "
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

    fallback_reason = None
    if grounded_listing_facts and not validated_recommendations:
        fallback_reason = (
            "invalid_model_listing_ids" if result["recommendations"]
            else "empty_model_recommendations"
        )
        fallback_facts = grounded_listing_facts[:3]
        validated_recommendations = [
            {
                "listing_id": facts["listing_id"],
                "reco_summary": "Matches the current structured property search filters.",
            }
            for facts in fallback_facts
        ]
        print(
            f"[RANK FALLBACK] reason={fallback_reason} "
            f"candidate_count={len(grounded_listing_facts)} "
            f"fallback_count={len(validated_recommendations)} "
            f"listing_ids={[item['listing_id'] for item in validated_recommendations]}",
            flush=True,
        )
        result["customer_response"] = (
            "I found a few current listings that match your search. "
            "Here are the strongest available options."
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
            "I don't have a suitable current property match for that search at the moment."
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
    current_run_recommendations = (
        new_recommendations if new_recommendations else validated_recommendations
    )
    new_folio_item_ids = []
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

    if not recommendations_available:
        log_timing("match_lead TOTAL", match_started)
        return customer_response

    facts_by_listing_id = {
        str(facts.get("listing_id")): facts
        for facts in grounded_listing_facts if facts.get("listing_id")
    }
    source_listing_by_id = {
        str(listing.get("_id")): listing
        for listing in listings if listing.get("_id")
    }
    current_run_listings = []
    for position, recommendation in enumerate(current_run_recommendations, start=1):
        listing_id = str(recommendation["listing_id"])
        item = dict(facts_by_listing_id.get(listing_id) or {})
        source_listing = source_listing_by_id.get(listing_id) or {}
        item.update({
            "listing_id": listing_id,
            "position": position,
            "recommendation_reason": recommendation.get("reco_summary") or "",
            "coverPhoto": source_listing.get("coverPhoto"),
            "photos": source_listing.get("photos"),
        })
        current_run_listings.append(item)
    recommendation_summary = build_whatsapp_recommendation_summary(
        folio_id, bubble_env, listings=current_run_listings
    )
    if recommendation_summary:
        customer_response = (
            result["customer_response"] + "\n\n" + recommendation_summary
            if fallback_reason else recommendation_summary
        )
    current_listing_ids = [
        item["listing_id"] for item in current_run_listings
    ]
    print(
        f"[RECOMMENDATION RUN] folio_id={folio_id} "
        f"current_listing_count={len(current_listing_ids)}",
        flush=True,
    )
    log_timing("match_lead TOTAL", match_started)
    return MatchingResult(
        customer_response,
        recommendations_available=True,
        recommendations=current_run_listings,
        listing_ids=current_listing_ids,
        folio_item_ids=new_folio_item_ids,
    )


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


def _normalized_geo_name(value):
    """Normalize harmless Geo formatting without treating arbitrary names as valid."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def remove_search_areas_from_regular_destinations(destinations, search_areas):
    area_keys = {_normalized_geo_name(value) for value in search_areas or []}
    return [
        value for value in destinations or []
        if _normalized_geo_name(value) not in area_keys
    ]


def get_valid_geo_names(bubble_env="live", now=None):
    """Return cached canonical names from Bubble's actual Geo objects."""
    current = time.monotonic() if now is None else float(now)
    with _geo_name_cache_lock:
        cached = _geo_name_cache.get(bubble_env)
        if cached and current - cached[0] < GEO_NAME_CACHE_TTL_SECONDS:
            return list(cached[1])
    base_url = get_bubble_base_url(bubble_env)
    names = []
    seen = set()
    for record in _bubble_records(base_url, "geo"):
        name = " ".join(str(
            record.get("name") or record.get("Name") or ""
        ).split())
        key = _normalized_geo_name(name)
        if name and key and key not in seen:
            names.append(name)
            seen.add(key)
    with _geo_name_cache_lock:
        _geo_name_cache[bubble_env] = (current, tuple(names))
    return names


def resolve_geo_name(candidate, valid_geo_names):
    """Resolve one candidate conservatively to a canonical Bubble Geo name."""
    original = " ".join(str(candidate or "").split())
    normalized = _normalized_geo_name(original)
    if not original or not normalized:
        return {"candidate": original, "resolved": None, "method": None,
                "score": None, "suggestion": None}
    canonical = [
        " ".join(str(name or "").split()) for name in valid_geo_names or []
        if str(name or "").strip()
    ]
    exact = next(
        (name for name in canonical if _normalized_geo_name(name) == normalized),
        None,
    )
    if exact:
        method = "exact" if original.casefold() == exact.casefold() else "normalized"
        return {"candidate": original, "resolved": exact, "method": method,
                "score": 1.0, "suggestion": exact}
    scored = sorted((
        (difflib.SequenceMatcher(None, normalized, _normalized_geo_name(name)).ratio(), name)
        for name in canonical
    ), reverse=True)
    best_score, best_name = scored[0] if scored else (0.0, None)
    second_score = scored[1][0] if len(scored) > 1 else 0.0
    if (
        best_name and best_score >= GEO_FUZZY_MATCH_THRESHOLD
        and best_score - second_score >= 0.08
    ):
        return {"candidate": original, "resolved": best_name, "method": "fuzzy",
                "score": best_score, "suggestion": best_name}
    suggestion = (
        best_name if best_name and best_score >= GEO_SUGGESTION_THRESHOLD
        and best_score - second_score >= 0.08 else None
    )
    return {"candidate": original, "resolved": None, "method": None,
            "score": best_score, "suggestion": suggestion}


def resolve_geo_names(candidates, valid_geo_names):
    resolved, unresolved, suggestions = [], [], {}
    seen = set()
    for candidate in candidates or []:
        result = resolve_geo_name(candidate, valid_geo_names)
        canonical = result["resolved"]
        if canonical:
            key = canonical.casefold()
            if key not in seen:
                resolved.append(canonical)
                seen.add(key)
            score = result["score"]
            suffix = f" score={score:.3f}" if result["method"] == "fuzzy" else ""
            print(
                f"[GEO RESOLUTION] candidate={result['candidate']!r} "
                f"resolved={canonical!r} method={result['method']}{suffix}",
                flush=True,
            )
        elif result["candidate"]:
            unresolved.append(result["candidate"])
            if result["suggestion"]:
                suggestions[result["candidate"]] = result["suggestion"]
            print(
                f"[GEO RESOLUTION] candidate={result['candidate']!r} "
                "status=unresolved", flush=True,
            )
    return {"resolved": resolved, "unresolved": unresolved,
            "suggestions": suggestions}


def unresolved_geo_response(resolution):
    unresolved = resolution.get("unresolved") or []
    suggestions = resolution.get("suggestions") or {}
    if len(unresolved) == 1 and suggestions.get(unresolved[0]):
        return f"Did you mean {suggestions[unresolved[0]]}?"
    rendered = ", ".join(unresolved)
    return f"I couldn't match {rendered} to an area I search. Which area did you mean?"


def structured_lead_update(
    update, base_url, existing_transaction_values=None,
):
    """Translate model-extracted values into Bubble's real structured Lead fields."""
    payload = {}
    transaction = update.get("transaction_type")
    if transaction and transaction != "unchanged":
        values = bubble_transaction_values(transaction)
        if update.get("_transaction_interest_mode") == "add":
            values = list(dict.fromkeys(
                bubble_transaction_values(existing_transaction_values) + values
            ))
        payload["TransactionType"] = values
        print(
            f"[TRANSACTION] raw={transaction!r} "
            f"internal_mode={sorted(_transaction_modes(transaction))!r}", flush=True,
        )
        print(f"[TRANSACTION] lead_persist_values={values!r}", flush=True)
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


def save_property_search_state(*args):
    """Persist non-critical search memory without aborting executable retrieval."""
    try:
        save_search_state(*args)
        return True
    except Exception as error:
        print(
            "[SEARCH PERSISTENCE] status=failed continuing_current_search "
            f"error={type(error).__name__}", flush=True,
        )
        return False


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
    if geo_names or area_mode in {"replace", "reset"}:
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
    relevant_budget = (
        scalar_update.get("budget_rent")
        if scalar_update.get("budget_rent") is not None
        else scalar_update.get("budget_buy")
    )
    if scalar_update.get("budget_rent") is not None:
        scalar_update["budget_rent"] = str(scalar_update["budget_rent"])
    if scalar_update.get("budget_buy") is not None:
        scalar_update["budget_buy"] = str(scalar_update["budget_buy"])
    if relevant_budget is not None:
        scalar_update["budget_requirement"] = str(relevant_budget)
    transaction = scalar_update.get("transaction_type")
    prior_modes = _transaction_modes(state["property_types"])
    incoming_modes = _transaction_modes(transaction)
    if incoming_modes == {"buy"}:
        state["budget_rent"] = ""
    elif incoming_modes == {"rent"}:
        state["budget_buy"] = ""
    if (incoming_modes and prior_modes and incoming_modes != prior_modes
            and relevant_budget is None):
        state["budget_requirement"] = ""
    incoming_property_types = _unique_search_values(
        scalar_update.get("property_types") or []
    )
    existing_transaction_types = [
        value for value in state["property_types"]
        if value.casefold() in {"rent", "buy", "both"}
    ]
    existing_home_types = [
        value for value in state["property_types"]
        if value.casefold() not in {"rent", "buy", "both"}
    ]
    if transaction and transaction != "unchanged":
        scalar_update["property_types"] = _unique_search_values(
            (incoming_property_types or existing_home_types) + [transaction]
        )
    elif incoming_property_types:
        scalar_update["property_types"] = _unique_search_values(
            incoming_property_types + existing_transaction_types
        )
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
    prior_state = load_search_state(cumulative_state)
    cumulative_update = dict(update)
    new_areas = _unique_search_values(update.get("geo_names") or [])
    if new_areas:
        cumulative_update["area_status"] = "known"
        cumulative_update["areas"] = _unique_search_values(
            load_search_state(cumulative_state)["areas"] + new_areas
        )
    if cumulative_update.get("bedrooms_min") is not None:
        cumulative_update["bedroom_requirement"] = str(cumulative_update["bedrooms_min"])
    relevant_budget = (
        cumulative_update.get("budget_rent")
        if cumulative_update.get("budget_rent") is not None
        else cumulative_update.get("budget_buy")
    )
    if relevant_budget is not None:
        cumulative_update["budget_requirement"] = str(relevant_budget)
    if cumulative_update.get("budget_rent") is not None:
        cumulative_update["budget_rent"] = str(cumulative_update["budget_rent"])
    if cumulative_update.get("budget_buy") is not None:
        cumulative_update["budget_buy"] = str(cumulative_update["budget_buy"])
    transaction = cumulative_update.get("transaction_type")
    if transaction and transaction != "unchanged":
        home_types = [
            value for value in (cumulative_update.get("property_types") or prior_state["property_types"])
            if value.casefold() not in {"rent", "buy", "both"}
        ]
        cumulative_update["property_types"] = _unique_search_values(
            home_types + [transaction]
        )
    state = apply_search_update(cumulative_state, cumulative_update)
    preferred = _unique_search_values(update.get("preferred_condo_names") or [])
    if preferred:
        state["recommended_condos"] = _unique_search_values(
            state["recommended_condos"] + preferred
        )
    return state


def lead_with_active_search_filters(lead, base_url):
    """Overlay explicit searchActive refinements on the durable Lead baseline."""
    state = load_active_search_state(lead)
    bubble_env = "development" if "/version-test/" in base_url else "live"
    valid_geo_names = get_valid_geo_names(bubble_env)
    state["areas"] = resolve_geo_names(
        state["areas"], valid_geo_names
    )["resolved"]
    has_state_filters = bool(
        state["areas"] or state["selected_condos"]
        or state["bedroom_requirement"] or state["budget_requirement"]
        or state["budget_rent"] or state["budget_buy"]
        or state["property_types"]
    )
    if not has_state_filters:
        print(
            "[SEARCH ACTIVE] no usable saved state; using existing Lead filters safely",
            flush=True,
        )
        return dict(lead)
    active = dict(lead)
    if state["areas"]:
        active["Geo"] = get_named_object_ids(base_url, "geo", state["areas"])
    if state["selected_condos"]:
        active["preferredCondos"] = get_named_object_ids(
            base_url, "condo", state["selected_condos"]
        )
        active["Geo"] = []
    if state["bedroom_requirement"]:
        active["bedroomsMin"] = _as_number(state["bedroom_requirement"])
    transaction_modes = _transaction_modes(state["property_types"])
    if transaction_modes:
        active["TransactionType"] = bubble_transaction_values(transaction_modes)
    effective_modes = transaction_modes or _transaction_modes(
        active.get("TransactionType") or []
    )
    if effective_modes == {"buy"}:
        active.pop("budgetRent", None)
        budget = _as_number(
            state["budget_buy"] or (
                state["budget_requirement"] if not transaction_modes else None
            )
        )
    elif effective_modes == {"rent"}:
        active.pop("budgetBuy", None)
        budget = _as_number(
            state["budget_rent"] or (
                state["budget_requirement"] if not transaction_modes else None
            )
        )
    else:
        budget = None
    if budget is not None:
        if effective_modes == {"rent"}:
            active["budgetRent"] = budget
        elif effective_modes == {"buy"}:
            active["budgetBuy"] = budget
    print("[SEARCH ACTIVE] using active filters for recommendation", flush=True)
    return active


def advance_property_search(folio_id, bubble_env, update):
    """Persist search facts and execute the useful action chosen for this turn."""
    print(
        "[SEARCH UPDATE] "
        f"transaction_type={update.get('transaction_type')!r} "
        f"bedrooms_min={update.get('bedrooms_min')!r} "
        f"budget_rent={update.get('budget_rent')!r} "
        f"budget_buy={update.get('budget_buy')!r} "
        f"preferred_condo_names={update.get('preferred_condo_names')!r} "
        f"search_listings={update.get('search_listings')!r}",
        flush=True,
    )
    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    lead_id = folio["lead"]
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    valid_geo_names = get_valid_geo_names(bubble_env)
    update = dict(update)
    condo_candidates = resolve_condo_names(update.get("geo_names") or [])
    if condo_candidates["resolved"]:
        update["geo_names"] = condo_candidates["unresolved"]
        update["preferred_condo_names"] = _unique_search_values(
            (update.get("preferred_condo_names") or [])
            + condo_candidates["resolved"]
        )
        update["area_update_mode"] = "reset"
        update["condo_update_mode"] = "replace"
        for canonical in condo_candidates["resolved"]:
            print(
                f"[PROPERTY RESOLUTION] candidate={canonical!r} "
                f"type=condo resolved={canonical!r}", flush=True,
            )
    if (update.get("preferred_condo_names")
            and update.get("condo_update_mode") in (None, "replace")):
        update["area_update_mode"] = "reset"
    geo_resolution = resolve_geo_names(
        update.get("geo_names") or [], valid_geo_names
    )
    safe_update = dict(update)
    safe_update["geo_names"] = geo_resolution["resolved"]
    safe_update["regular_destinations"] = remove_search_areas_from_regular_destinations(
        safe_update.get("regular_destinations"), safe_update["geo_names"]
    )
    cumulative_source = load_search_state(lead.get("searchBriefJSON"))
    cumulative_source["areas"] = resolve_geo_names(
        cumulative_source["areas"], valid_geo_names
    )["resolved"]
    if not cumulative_source["areas"] and cumulative_source["area_status"] == "known":
        cumulative_source["area_status"] = "unknown"
    active_source = load_active_search_state(lead)
    active_source["areas"] = resolve_geo_names(
        active_source["areas"], valid_geo_names
    )["resolved"]
    if not active_source["areas"] and active_source["area_status"] == "known":
        active_source["area_status"] = "unknown"
    cumulative_update = dict(safe_update)
    if safe_update.get("_transaction_interest_mode") == "add":
        cumulative_modes = _transaction_modes(lead.get("TransactionType") or [])
        cumulative_modes.update(_transaction_modes(safe_update.get("transaction_type")))
        cumulative_update["transaction_type"] = (
            "both" if cumulative_modes == {"rent", "buy"}
            else next(iter(cumulative_modes), safe_update.get("transaction_type"))
        )
    cumulative_state = apply_cumulative_search_update(
        cumulative_source, cumulative_update
    )
    active_state = apply_active_search_update(
        active_source, safe_update
    )
    preferred_names = [
        str(name).strip() for name in update.get("preferred_condo_names", [])
        if str(name).strip()
    ]
    if preferred_names:
        cumulative_state = set_recommended_condos(cumulative_state, _unique_search_values(
            cumulative_state["recommended_condos"] + preferred_names
        ))
    print(
        f"[SEARCH ACTIVE] condos={active_state['selected_condos']!r} "
        f"areas={active_state['areas']!r} "
        f"beds={active_state['bedroom_requirement'] or None} "
        f"budget_rent={active_state['budget_rent'] or None} "
        f"budget_buy={active_state['budget_buy'] or None}", flush=True,
    )
    lead_fields = structured_lead_update(
        safe_update, base_url, lead.get("TransactionType") or []
    )
    for field in ("Geo", "preferredCondos"):
        if field in lead_fields:
            lead_fields[field] = list(dict.fromkeys(
                list(lead.get(field) or []) + list(lead_fields[field] or [])
            ))

    if geo_resolution["unresolved"]:
        save_property_search_state(
            lead_id, cumulative_state, base_url, lead_fields, active_state
        )
        return {
            "action": "ask",
            "text": unresolved_geo_response(geo_resolution),
            "state": cumulative_state, "active_state": active_state,
            "lead_id": lead_id, "geo_resolution": geo_resolution,
        }

    scope = listing_search_scope(
        active_state,
        selected_condos=preferred_names or None,
        use_full_shortlist=bool(
            update.get("use_full_shortlist") or update.get("search_listings")
        ),
    )
    if update.get("search_listings") and (scope or not active_state["recommended_condos"]):
        active_state["selected_condos"] = list(scope or [])
        save_property_search_state(
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
        save_property_search_state(
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
        save_property_search_state(
            lead_id, cumulative_state, base_url, lead_fields, active_state
        )
        return {
            "action": "condo_shortlist",
            "text": response_text.rstrip() + "\n\nWhich would you like to explore?",
            "state": cumulative_state, "active_state": active_state,
            "lead_id": lead_id,
            "recommendations": recommendations,
        }

    save_property_search_state(
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


def _message_refers_to_reply_listing(message):
    text = normalize_condo_name(message)
    return bool(re.search(
        r"\b(this(?: one| property| house| home| condo)?|here|around here|near this|"
        r"nearby|closest|within (?:walking|driving) distance)\b",
        text,
    ))


def _explicit_nearby_place(message):
    """Extract a place the customer explicitly pins after nearby phrasing."""
    text = " ".join(str(message or "").split())
    explicit = re.search(
        r"\b(?:around|near|close to|close by)\s+"
        r"(?!this\b|here\b)([^?,.]+?)"
        r"(?=\s+(?:from|to|within)\b|[?,.]|$)",
        text, flags=re.IGNORECASE,
    )
    if not explicit:
        explicit = re.search(
            r"\b(?:closest|nearest)\b.*?\bto\s+"
            r"(?!this\b|here\b)([^?,.]+?)(?=[?,.]|$)",
            text, flags=re.IGNORECASE,
        )
    if explicit:
        return " ".join(explicit.group(1).split())
    return None


def _explicit_property_search_location(message):
    """Extract only a location explicitly scoped by the current search request."""
    text = " ".join(str(message or "").split())
    patterns = (
        r"\b(?:find|show) me\b.*?\bin\s+(.+?)(?:\s+(?:then|instead))?[?.!]*$",
        r"\bwhat (?:have|do) you got\b.*?\bin\s+(.+?)[?.!]*$",
        r"\bshow me\s+(.+?)\s+instead[?.!]*$",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return " ".join(match.group(1).strip(" ,.!?").split())
    return None


def _apply_current_search_location(user_message, bubble_env, tool_args):
    """Make this turn's explicit location authoritative over model history."""
    candidate = _explicit_property_search_location(user_message)
    condos = resolve_condo_mentions(candidate or user_message)
    if condos:
        updated = dict(tool_args)
        updated.update({
            "preferred_condo_names": condos,
            "condo_update_mode": "replace",
            "geo_names": [],
            "area_update_mode": "reset",
        })
        print(
            f"[PROPERTY RESOLUTION] candidate={candidate or condos[0]!r} "
            f"type=condo resolved={condos[0]!r}", flush=True,
        )
        print(
            "[SEARCH OVERRIDE] source=current_message field=location "
            f"condos={condos!r} areas=[]", flush=True,
        )
        return updated
    if not candidate:
        return tool_args
    normalized_message = normalize_condo_name(user_message)
    for model_geo in _unique_search_values(tool_args.get("geo_names") or []):
        normalized_geo = normalize_condo_name(model_geo)
        if normalized_geo and re.search(
            rf"(?<!\w){re.escape(normalized_geo)}(?!\w)", normalized_message
        ):
            candidate = model_geo
            break
    updated = dict(tool_args)
    updated["preferred_condo_names"] = []
    updated["condo_update_mode"] = "reset"
    updated["geo_names"] = [candidate]
    updated["area_update_mode"] = "replace"
    print(
        f"[PROPERTY RESOLUTION] candidate={candidate!r} "
        "type=area_candidate resolution=pending", flush=True,
    )
    print(
        "[SEARCH OVERRIDE] source=current_message field=location "
        f"condos=[] areas={updated['geo_names']!r}", flush=True,
    )
    return updated


def _apply_current_transaction_intent(user_message, tool_args):
    """Normalize explicit current-turn intent before Bubble persistence."""
    text = normalize_condo_name(user_message)
    buy_intent = bool(re.search(r"\b(buy|buying|purchase|purchasing|for sale)\b", text))
    rent_intent = bool(re.search(r"\b(rent|renting|rental|to let)\b", text))
    if not buy_intent and not rent_intent:
        return tool_args
    updated = dict(tool_args)
    active_mode = "both" if buy_intent and rent_intent else "buy" if buy_intent else "rent"
    updated["transaction_type"] = active_mode
    if re.search(r"\b(as well|also|too)\b", text):
        updated["_transaction_interest_mode"] = "add"
    print(
        f"[TRANSACTION] raw={user_message!r} internal_mode={active_mode}",
        flush=True,
    )
    return updated


def _exact_property_name_for_location(message, conversation_context=None):
    """Return only a high-confidence exact name from this turn or trusted context."""
    explicit = _explicit_nearby_place(message)
    if explicit:
        return explicit
    if not _message_refers_to_reply_listing(message):
        return None
    context = str(conversation_context or "")
    trusted = re.search(r"(?m)^- (?:Property|Subject):\s*(.+?)\s*$", context)
    return " ".join(trusted.group(1).split()) if trusted else None


def _preserve_exact_property_name(location, property_name):
    location = " ".join(str(location or "").split())
    property_name = " ".join(str(property_name or "").split())
    if not property_name:
        return location
    normalized_location = _normalized_location_entity_name(location)
    normalized_property = _normalized_location_entity_name(property_name)
    if normalized_property and normalized_property in normalized_location:
        return location
    return ", ".join(part for part in (property_name, location) if part)


def _trusted_location_from_context(message, conversation_context=None):
    if not _message_refers_to_reply_listing(message):
        return None
    context = str(conversation_context or "")
    values = []
    for label in ("Property", "Address", "Location"):
        match = re.search(rf"(?m)^- {label}:\s*(.+?)\s*$", context)
        if not match:
            continue
        value = " ".join(match.group(1).split())
        normalized = _normalized_location_entity_name(value)
        if any(normalized in _normalized_location_entity_name(item) for item in values):
            continue
        values.append(value)
    return ", ".join(values) if values else None


def _trusted_reply_listing_location(listing_id, bubble_env="live",
                                    include_coordinates=True):
    """Load the exact reply Listing and build its most-specific trusted location."""
    listing_id = conversation_store.relationship_id(listing_id)
    if not listing_id:
        return None
    try:
        listing = bubble(f"{get_bubble_base_url(bubble_env)}/obj/listing/{listing_id}")
    except Exception as error:
        print(
            "[LOCATION REPLY CONTEXT] action=listing_lookup_failed "
            f"listing_id={listing_id} error={type(error).__name__}", flush=True,
        )
        return None
    components = []
    for field in (
        "name", "title", "condoName", "Address", "address", "Full Address",
        "street", "Street", "Location", "area", "Area",
    ):
        value = " ".join(str(listing.get(field) or "").split())
        if not value:
            continue
        normalized = _normalized_location_entity_name(value)
        if any(normalized in _normalized_location_entity_name(item) for item in components):
            continue
        components.append(value)
    if not components:
        return None
    coordinates = None
    if include_coordinates:
        latitude = next((listing.get(field) for field in
                         ("latitude", "Latitude", "lat", "Lat")
                         if listing.get(field) not in (None, "")), None)
        longitude = next((listing.get(field) for field in
                          ("longitude", "Longitude", "lng", "Lng", "lon", "Lon")
                          if listing.get(field) not in (None, "")), None)
        try:
            coordinates = (float(latitude), float(longitude))
        except (TypeError, ValueError):
            pass
    return {"listing_id": listing_id, "location": ", ".join(components),
            "coordinates": coordinates}


def execute_chat_tool(tool_call, folio_id, bubble_env, message_id,
                      user_message=None, reply_listing_id=None,
                      conversation_context=None):
    """Execute one supported chat tool and return neutral continuation grounding."""
    tool_args = parse_completed_tool_arguments(tool_call)
    explicit_nearby_place = _explicit_nearby_place(user_message)
    trusted_listing = None
    if (tool_call.name in {"find_nearby_places", "get_travel_time", "compare_locations"}
            and reply_listing_id and not explicit_nearby_place
            and _message_refers_to_reply_listing(user_message)):
        trusted_listing = _trusted_reply_listing_location(
            reply_listing_id, bubble_env,
            include_coordinates=tool_call.name != "find_nearby_places",
        )
    trusted_context_location = (
        None if explicit_nearby_place or trusted_listing
        else _trusted_location_from_context(user_message, conversation_context)
    )
    exact_property_name = _exact_property_name_for_location(
        user_message, conversation_context
    )
    has_match_results = False
    recommendations = []
    if tool_call.name == "advance_property_search":
        tool_args = _apply_current_transaction_intent(user_message, tool_args)
        tool_args = _apply_current_search_location(
            user_message, bubble_env, tool_args
        )
        search_result = advance_property_search(folio_id, bubble_env, tool_args)
        if search_result["action"] == "search_listings":
            matching_result = execute_match_lead_silently(
                folio_id, bubble_env, message_id, search_result["scope"]
            )
            save_property_search_state(
                search_result["lead_id"], search_result["state"],
                get_bubble_base_url(bubble_env),
            )
            has_match_results = bool(getattr(
                matching_result, "recommendations_available", False
            ))
            recommendations = list(getattr(matching_result, "recommendations", []))
            output = str(matching_result)
        else:
            output = search_result["text"]
        instructions = (
            "The supplied result is grounded property-search output. Use it if it answers "
            "the customer's request. If another supported tool is genuinely required, call "
            "that tool. Never expose search arguments or internal state."
        )
    elif tool_call.name == "match_lead":
        matching_result = execute_match_lead_silently(
            folio_id, bubble_env, message_id
        )
        has_match_results = bool(getattr(
            matching_result, "recommendations_available", False
        ))
        recommendations = list(getattr(matching_result, "recommendations", []))
        output = str(matching_result)
        instructions = (
            "The supplied result contains grounded current-listing output. Return its "
            "customer-facing answer faithfully unless another supported tool is genuinely "
            "required. Never expose tool arguments or internal state."
        )
    elif tool_call.name == "get_property_details":
        output = get_property_details(
            folio_id, tool_args["property_reference"], bubble_env
        )
        instructions = (
            "The supplied result contains authoritative details for a current listing. Use it "
            "only to the extent it answers the request. For a missing public external fact, "
            "web search may be used; never search for or guess missing unit-specific facts. "
            "Call another supported tool if the actual request requires it."
        )
    elif tool_call.name == "get_current_recommendations":
        output = get_current_recommendations(folio_id, bubble_env)
        instructions = (
            "The supplied result contains current recommendations. Use customer-facing names "
            "and say when a field is unavailable. Call another supported tool only if the "
            "customer's actual request requires it; do not imply this read-only result changed "
            "the shortlist."
        )
    elif tool_call.name == "get_condo_info":
        condo_names = tool_args.get("condo_names")
        output = get_condo_infos(condo_names if isinstance(condo_names, list) else [])
        instructions = (
            "The supplied result contains condo information. Use it only if it actually "
            "answers the customer's request. If the request instead concerns current "
            "properties, listings, units, options, houses, landed homes, rentals, sales, or "
            "availability, call advance_property_search. Treat Persona as qualitative insight, "
            "do not invent missing facts, and never expose tool or state fields."
        )
    elif tool_call.name == "get_travel_time":
        origin = tool_args.get("origin")
        destination = tool_args.get("destination")
        origin_coordinates = destination_coordinates = None
        if trusted_listing:
            if re.search(r"\b(?:from|to) (?:this|here)\b",
                         normalize_condo_name(user_message)):
                destination = trusted_listing["location"]
                destination_coordinates = trusted_listing["coordinates"]
            else:
                origin = trusted_listing["location"]
                origin_coordinates = trusted_listing["coordinates"]
        elif exact_property_name:
            if re.search(r"\b(?:from|to) (?:this|here)\b",
                         normalize_condo_name(user_message)):
                destination = _preserve_exact_property_name(
                    destination, exact_property_name
                )
            else:
                origin = _preserve_exact_property_name(origin, exact_property_name)
        if origin_coordinates or destination_coordinates:
            output = get_travel_time(
                origin, destination, tool_args.get("mode", "driving"),
                origin_coordinates=origin_coordinates,
                destination_coordinates=destination_coordinates,
            )
        else:
            output = get_travel_time(
                origin, destination, tool_args.get("mode", "driving")
            )
        instructions = (
            "Use the supplied grounded result for distance, driving time, place identity, and "
            "traffic basis. Surface ambiguity and area-level approximation. Call another "
            "supported tool only when genuinely required. Never invent ranges or ask for a "
            "unit number, floor, or apartment details."
        )
    elif tool_call.name == "find_nearby_places":
        origin = (
            explicit_nearby_place
            or (trusted_listing or {}).get("location")
            or trusted_context_location
            or tool_args.get("origin")
        )
        nearby_args = (
            origin, tool_args.get("categories") or [],
            tool_args.get("travel_mode", "walking"),
            tool_args.get("max_travel_minutes"), tool_args.get("max_results"),
        )
        output = find_nearby_places(*nearby_args)
        instructions = (
            "Use only the supplied nearby-place results for business identity, address, "
            "distance, and travel time. Summarize confirmed results naturally and do not "
            "invent additional businesses. If the origin is area-level, explain that results "
            "are approximate. For a strict time threshold, describe only places in `places`, "
            "never unrouted candidates. If results are partial, state briefly that routing "
            "could not verify the requested threshold."
        )
    elif tool_call.name == "compare_locations":
        candidate_locations = list(tool_args.get("candidate_locations") or [])
        coordinate_map = None
        if trusted_listing:
            if candidate_locations:
                candidate_locations[0] = trusted_listing["location"]
            else:
                candidate_locations = [trusted_listing["location"]]
            if trusted_listing["coordinates"]:
                coordinate_map = {
                    _normalized_location_entity_name(trusted_listing["location"]):
                    trusted_listing["coordinates"]
                }
        elif exact_property_name:
            if candidate_locations:
                candidate_locations[0] = _preserve_exact_property_name(
                    candidate_locations[0], exact_property_name
                )
            else:
                candidate_locations = [exact_property_name]
        if coordinate_map:
            output = compare_locations(
                candidate_locations, tool_args.get("destinations") or [],
                trusted_coordinates=coordinate_map,
            )
        else:
            output = compare_locations(
                candidate_locations, tool_args.get("destinations") or []
            )
        instructions = (
            "Use the supplied grounded matrix for every geographic claim. Surface unresolved "
            "or ambiguous places and area-level approximations. Combine it with the customer's "
            "needs, or call another supported tool if the actual request genuinely requires it."
        )
    else:
        raise ValueError(f"Unsupported tool: {tool_call.name}")
    return {
        "output": output, "instructions": instructions,
        "has_match_results": has_match_results,
        "recommendations": recommendations,
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
        reply_listing_id = conversation_store.relationship_id(
            data.get("reply_listing_id")
        )
        conversation_context = str(
            data.get("conversation_context") or ""
        ).strip()

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
                    function_call_started = False
                    web_search_started = False
                    web_search_completed = False
                    output_text_started = False
                    output_text_done = False
                    incomplete_reason = None
                    response_id_seen = None
                    last_response_object = None
                    completed_output_items = []
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
                                    if response_object is not None:
                                        last_response_object = response_object
                                    response_id_seen = (
                                        getattr(response_object, "id", None)
                                        or getattr(event, "response_id", None)
                                        or response_id_seen
                                    )
                                    item = getattr(event, "item", None)
                                    item_type = getattr(item, "type", None)
                                    if "function_call" in event_type or item_type == "function_call":
                                        function_call_started = True
                                    if "web_search_call" in event_type or item_type == "web_search_call":
                                        web_search_started = True
                                    if event_type == "response.web_search_call.completed":
                                        web_search_completed = True
                                    if event_type == "response.output_item.done" and item is not None:
                                        completed_output_items.append(item)
                                    if event_type == "response.incomplete":
                                        details = (
                                            getattr(response_object, "incomplete_details", None)
                                            or getattr(event, "incomplete_details", None)
                                        )
                                        incomplete_reason = getattr(details, "reason", None)
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
                                        output_text_started = True
                                        if not initial_first_delta_logged:
                                            log_timing("Initial OpenAI FIRST DELTA", initial_started)
                                            initial_first_delta_logged = True
                                        # Buffer until completion proves no function call follows.
                                        buffered_text_deltas.append(event.delta)
                                    elif event_type == "response.output_text.done":
                                        output_text_done = True
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
                            f"application_function_call_started={function_call_started}; "
                            f"web_search_started={web_search_started}; "
                            f"web_search_completed={web_search_completed}; "
                            f"output_text_started={output_text_started}; "
                            f"output_text_done={output_text_done}; "
                            f"incomplete_reason={incomplete_reason or 'unknown'}; "
                            f"response_id={response_id_seen}; elapsed={elapsed:.2f}s; "
                            f"iterator_ended_naturally={iterator_ended_naturally}"
                        )
                        print(f"[OPENAI STREAM WARNING] {diagnostic}", flush=True)
                        recoverable_text = bool(
                            buffered_text_deltas
                            and not function_call_started
                            and (not web_search_started or web_search_completed)
                            and (output_text_done or not web_search_started)
                        )
                        if recoverable_text:
                            text_chars = sum(len(delta) for delta in buffered_text_deltas)
                            print(
                                "[OPENAI STREAM INCOMPLETE] "
                                f"reason={incomplete_reason or 'unknown'} "
                                "application_function_call_started=false "
                                f"web_search_started={str(web_search_started).lower()} "
                                f"web_search_completed={str(web_search_completed).lower()} "
                                f"output_text_done={str(output_text_done).lower()} "
                                f"buffered_text_chars={text_chars} action=preserve_text",
                                flush=True,
                            )
                            recovered_output = list(
                                getattr(last_response_object, "output", None) or
                                completed_output_items
                            )
                            final_response = SimpleNamespace(
                                id=response_id_seen, output=recovered_output, usage=None,
                                status="incomplete",
                                incomplete_details=getattr(
                                    last_response_object, "incomplete_details", None
                                ),
                            )
                        else:
                            print(
                                "[OPENAI STREAM INCOMPLETE] "
                                f"reason={incomplete_reason or 'unknown'} "
                                "application_function_call_started="
                                f"{str(function_call_started).lower()} "
                                f"web_search_started={str(web_search_started).lower()} "
                                f"web_search_completed={str(web_search_completed).lower()} "
                                f"output_text_done={str(output_text_done).lower()} "
                                f"buffered_text_chars={sum(len(x) for x in buffered_text_deltas)} "
                                "action=fail_safe",
                                flush=True,
                            )
                            if function_call_started:
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

                # Every model round is buffered until its completed response proves that
                # no function call follows. This prevents orchestration prose from leaking.
                initial_args = build_response_args(
                    message, previous, conversation_context
                )
                normal_tools = initial_args["tools"]
                try:
                    response, buffered_text = stream_initial_response(
                        initial_args, "Initial OpenAI/tool selection"
                    )
                except Exception as error:
                    if "No tool output found for function call" not in str(error):
                        raise
                    print(
                        "Broken previous_response_id detected; starting a fresh conversation",
                        flush=True,
                    )
                    retry_args = build_response_args(
                        message, None, conversation_context
                    )
                    normal_tools = retry_args["tools"]
                    response, buffered_text = stream_initial_response(
                        retry_args, "Initial OpenAI/tool selection retry"
                    )

                tool_round = 0
                has_match_results = False
                current_run_recommendations = []
                while True:
                    if any(
                        getattr(item, "type", None) == "web_search_call"
                        for item in response.output
                    ) and not web_search_status_sent:
                        print("Web search used", flush=True)
                        web_search_status_sent = True
                    tool_call = next(
                        (item for item in response.output
                         if getattr(item, "type", None) == "function_call"),
                        None,
                    )
                    if tool_call is None:
                        final_text = "".join(buffered_text)
                        if resembles_internal_orchestration_payload(final_text):
                            print(
                                "[ORCHESTRATION GUARD] "
                                "suppressed_internal_payload=true",
                                flush=True,
                            )
                            buffered_text = [
                                "Sorry, I couldn’t complete that property request safely. "
                                "Please try again."
                            ]
                            has_match_results = False
                            current_run_recommendations = []
                        print(
                            f"[TOOL LOOP] round={tool_round + 1} "
                            f"final_text chars={sum(len(x) for x in buffered_text)}",
                            flush=True,
                        )
                        for delta in buffered_text:
                            if not first_delta_sent:
                                log_timing("FIRST DELTA", request_started)
                                first_delta_sent = True
                            yield f"data: {json.dumps({'delta': delta})}\n\n"
                        citations = get_web_citations(response)
                        if citations:
                            yield f"data: {json.dumps({'citations': citations})}\n\n"
                        log_timing("TOTAL REQUEST", request_started)
                        yield (
                            f"data: {json.dumps({'done': True, 'response_id': response.id, 'recommendations_relevant': has_match_results, 'recommendations': current_run_recommendations})}\n\n"
                        )
                        return

                    if tool_round >= MAX_TOOL_ROUNDS:
                        print(
                            f"[TOOL LOOP] max_rounds_reached={MAX_TOOL_ROUNDS}",
                            flush=True,
                        )
                        yield f"data: {json.dumps({'delta': 'Sorry, I couldn’t complete that request safely. Please try again.'})}\n\n"
                        yield f"data: {json.dumps({'done': True, 'response_id': response.id, 'recommendations_relevant': False, 'recommendations': []})}\n\n"
                        return

                    tool_round += 1
                    print(
                        f"[TOOL LOOP] round={tool_round} selected={tool_call.name}",
                        flush=True,
                    )
                    buffered_chars = sum(len(delta) for delta in buffered_text)
                    if buffered_chars:
                        if tool_round == 1:
                            print(
                                "WARNING: Suppressed initial OpenAI text emitted before tool "
                                f"{tool_call.name}; internal orchestration was not sent to Bubble.",
                                flush=True,
                            )
                        print(
                            f"[TOOL LOOP] round={tool_round} "
                            f"buffered_text_discarded chars={buffered_chars}",
                            flush=True,
                        )
                    execution = execute_chat_tool(
                        tool_call, folio_id, bubble_env, message_id,
                        user_message=message, reply_listing_id=reply_listing_id,
                        conversation_context=conversation_context,
                    )
                    if execution["has_match_results"]:
                        has_match_results = True
                    if execution["recommendations"]:
                        current_run_recommendations = execution["recommendations"]
                    tool_output = execution["output"]
                    if not isinstance(tool_output, str):
                        tool_output = json.dumps(tool_output, ensure_ascii=False)
                    continuation_args = {
                        "model": "gpt-5-mini",
                        "reasoning": {"effort": "low"},
                        "max_output_tokens": FINAL_MAX_OUTPUT_TOKENS,
                        "previous_response_id": response.id,
                        "instructions": (
                            rentee_instructions() + "\n\n" + execution["instructions"]
                            + "\nThe tool result does not determine customer intent. If "
                            "another supported tool is needed, call it. Emit customer-facing "
                            "text only when the task is complete."
                        ),
                        "input": [{
                            "type": "function_call_output",
                            "call_id": tool_call.call_id,
                            "output": tool_output,
                        }],
                    }
                    if tool_round < MAX_TOOL_ROUNDS:
                        continuation_args["tools"] = normal_tools
                        continuation_args["tool_choice"] = "auto"
                    else:
                        continuation_args["instructions"] += (
                            " No further function calls are available in this turn; provide "
                            "only a safe customer-facing answer based on completed results."
                        )
                    response, buffered_text = stream_initial_response(
                        continuation_args,
                        f"Tool continuation round {tool_round}",
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


def conversation_model_context(conversation, lead=None, bubble_env="live"):
    """Build compact role grounding without exposing Bubble relationship IDs."""
    conversation = conversation or {}
    details = []
    for label, field in (
        ("Counterparty role", "CounterParty Role"),
        ("Rentee role", "Rentee Role"),
        ("Subject", "Subject"),
    ):
        value = " ".join(str(conversation.get(field) or "").split())
        if value:
            details.append(f"- {label}: {value[:160]}")
    if conversation_store.relationship_id(conversation.get("Enquiry")):
        details.append("- Enquiry: active")
    if conversation_store.relationship_id(conversation.get("Listing")):
        details.append("- Listing: linked to this enquiry")
    if not details:
        details.append("- Context: general property-search conversation")
    if lead and not conversation_store.relationship_id(conversation.get("Enquiry")):
        baseline = build_prefilled_search_confirmation(lead, bubble_env)
        if baseline:
            details.append(
                "- Durable customer search baseline: "
                + baseline.removesuffix(" Is that still right?")
            )
    return (
        "Current conversation context:\n" + "\n".join(details)
        + "\nStay within this role and business context."
    )


def whatsapp_reply_listing_context(listing_id, bubble_env="live"):
    """Build compact trusted model context for an exact WhatsApp listing reply."""
    listing_id = conversation_store.relationship_id(listing_id)
    if not listing_id:
        return None
    base_url = get_bubble_base_url(bubble_env)
    try:
        listing = bubble(f"{base_url}/obj/listing/{listing_id}")
        condo_id = conversation_store.relationship_id(listing.get("condo"))
        condo_names = (
            get_relationship_names(base_url, "condo", [condo_id])
            if condo_id else {}
        )
        facts = listing_facts(listing, condo_names=condo_names)
    except Exception as error:
        print(
            "[WHATSAPP REPLY CONTEXT] action=listing_lookup_failed "
            f"listing_id={listing_id} error={type(error).__name__}", flush=True,
        )
        return None
    labels = (
        ("Property", "property_name"), ("Bedrooms", "beds"),
        ("Bathrooms", "baths"), ("Rent", "priceRent"),
        ("Sale price", "priceSale"), ("Property type", "propertyType"),
        ("Furnishing", "Furnishing"), ("Furnishing", "furnished"),
        ("Availability", "availability"), ("Size", "Sq Ft"),
        ("Address", "Address"), ("Address", "address"), ("Location", "Location"),
        ("Latitude", "latitude"), ("Latitude", "Latitude"),
        ("Latitude", "lat"), ("Latitude", "Lat"),
        ("Longitude", "longitude"), ("Longitude", "Longitude"),
        ("Longitude", "lng"), ("Longitude", "Lng"),
        ("Land size", "Landed_sqft"), ("Key facts", "keyFacts"),
        ("Description", "Description"), ("Notes", "Notes"),
    )
    rendered = []
    used_labels = set()
    for label, field in labels:
        value = facts.get(field)
        if value in (None, "", []) or label in used_labels:
            continue
        rendered.append(f"- {label}: {' '.join(str(value).split())[:500]}")
        used_labels.add(label)
    if not rendered:
        rendered.append("- Property: exact previously recommended property")
    return (
        "WHATSAPP REPLY CONTEXT (trusted application context)\n"
        "The customer used WhatsApp Reply on a message specifically referring "
        "to this previously recommended property:\n"
        + "\n".join(rendered)
        + "\nInterpret references such as this, it, that, this one, that one, or "
        "the property as this exact property unless the customer clearly changes "
        "subject. For location tools, preserve the most specific identity available: "
        "stored coordinates first, then the full property name plus address; never "
        "drop a house number or building name in favour of its broader area. "
        "This exact reply context takes priority over fuzzy property "
        "reference resolution. Do not expose internal record IDs."
    )


def run_rentee_turn(message, folio_id, previous_response_id=None, message_id=None,
                    bubble_env="live", conversation_context=None,
                    reply_listing_id=None):
    """Run the existing customer turn lifecycle and collect its clean final text."""
    payload = {
        "message": message, "folio_id": folio_id, "bubble_env": bubble_env,
        "message_id": message_id,
    }
    if previous_response_id:
        payload["previous_response_id"] = previous_response_id
    if conversation_context:
        payload["conversation_context"] = conversation_context
    if reply_listing_id:
        payload["reply_listing_id"] = reply_listing_id
    # Keep one implementation of prompts, tools, retries, and event filtering: the
    # same streaming endpoint is consumed internally and collapsed for WhatsApp.
    with app.test_client() as test_client:
        response = test_client.post("/chat_stream", json=payload)
        if response.status_code != 200:
            raise RuntimeError(f"Rentee turn returned HTTP {response.status_code}")
        text_parts = []
        response_id = None
        recommendations_relevant = False
        current_run_recommendations = []
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
            if isinstance(event.get("recommendations"), list):
                current_run_recommendations = event["recommendations"]
    answer = "".join(text_parts).strip()
    if not answer:
        raise RuntimeError("Rentee turn returned no customer-facing text")
    return (
        answer, response_id,
        current_run_recommendations or recommendations_relevant,
    )


def _process_whatsapp_message(message):
    message_id = str(message["id"])
    typing_keepalive = whatsapp_typing_keepalive(message_id)
    phone = normalize_phone(message["from"])
    message_type = str(message.get("type") or "text").strip().casefold()
    raw_text = str((message.get("text") or {}).get("body") or "")
    text = raw_text.strip()
    phone_lock = _whatsapp_phone_locks.setdefault(phone, threading.Lock())
    reply_sent = False
    try:
        with phone_lock:
            identity_error = None
            whatsapp_user = None
            bubble_user_id = None
            try:
                whatsapp_user = resolve_whatsapp_user(
                    phone, message.get("customer_name"), "live"
                )
                bubble_user_id = conversation_store.relationship_id(
                    whatsapp_user.get("_id")
                )
                message["bubble_user_id"] = bubble_user_id
            except WhatsAppIdentityCollision as error:
                # Do not enter User/Lead processing for an ambiguous identity. We
                # still perform Conversation routing below so the inbound Message
                # can be retained safely when an existing context is available.
                identity_error = error
            if message_type == "audio":
                media_id = str((message.get("audio") or {}).get("id") or "").strip()
                print(
                    f"[WHATSAPP AUDIO] message_id={message_id} "
                    f"sender={_masked_whatsapp_phone(phone)} "
                    f"media_id={media_id or 'missing'} received", flush=True,
                )
                text = transcribe_whatsapp_audio(media_id, message_id)
                print(
                    f"[WHATSAPP AUDIO] message_id={message_id} "
                    "processing_transcript_as_text", flush=True,
                )
            base_url = get_bubble_base_url("live")
            inbound_message_id = None
            routed_conversation = None
            routed_conversation_id = None
            reply_listing_id = None
            ambiguous_candidates = []
            try:
                reply_context = find_reply_context(message, "live")
                routed_conversation = (
                    reply_context.get("conversation") if reply_context else None
                )
                reply_listing_id = (
                    conversation_store.relationship_id(
                        reply_context.get("listing_id")
                    ) if reply_context else None
                )
                routed_conversation_id = conversation_store.relationship_id(
                    (routed_conversation or {}).get("_id")
                )
                if routed_conversation_id:
                    print(
                        "[CONVERSATION ROUTING] direction=inbound "
                        f"resolution=reply_to conversation_id={routed_conversation_id} "
                        f"listing_id={reply_listing_id or 'none'}",
                        flush=True,
                    )
                    inbound_message_id, _inbound_created = (
                        persist_inbound_whatsapp_message(
                            phone, message_id, text,
                            conversation_id=routed_conversation_id,
                            bubble_env="live",
                            listing_id=reply_listing_id,
                        )
                    )
            except Exception as error:
                print(
                    "[CONVERSATION ROUTING] direction=inbound "
                    "resolution=reply_to_failed "
                    f"message_id={message_id} error={type(error).__name__}",
                    flush=True,
                )
            if not routed_conversation_id:
                try:
                    phone_resolution = find_active_conversation_by_phone(
                        phone, text, "live", include_candidates=True
                    )
                    routed_conversation, resolution, candidate_count = (
                        phone_resolution[:3]
                    )
                    if (
                        len(phone_resolution) > 3
                        and resolution == "ambiguous"
                        and candidate_count > 1
                    ):
                        ambiguous_candidates = phone_resolution[3]
                    if candidate_count:
                        print(
                            "[CONVERSATION ROUTING] direction=inbound "
                            f"candidate_count={candidate_count}", flush=True,
                        )
                    if resolution == "ambiguous":
                        clue_conversation, clue, clue_candidates = (
                            route_conversation_by_message_clue(
                                text, ambiguous_candidates, "live"
                            )
                        )
                        if clue_conversation:
                            routed_conversation = clue_conversation
                            resolution = "message_clue"
                            ambiguous_candidates = []
                            print(
                                "[CONVERSATION ROUTING] direction=inbound "
                                f"resolution=message_clue clue={clue} "
                                f"conversation_id={conversation_store.relationship_id(clue_conversation.get('_id'))}",
                                flush=True,
                            )
                        else:
                            ambiguous_candidates = clue_candidates
                            print(
                                "[CONVERSATION ROUTING] direction=inbound "
                                "resolution=ambiguous "
                                f"candidate_count={len(ambiguous_candidates)} "
                                "action=clarify",
                                flush=True,
                            )
                    routed_conversation_id = conversation_store.relationship_id(
                        (routed_conversation or {}).get("_id")
                    )
                    if routed_conversation_id:
                        enquiry_id = conversation_store.relationship_id(
                            routed_conversation.get("Enquiry")
                        )
                        print(
                            "[CONVERSATION ROUTING] direction=inbound "
                            f"resolution={resolution} "
                            f"conversation_id={routed_conversation_id}"
                            + (f" enquiry_id={enquiry_id}" if enquiry_id else ""),
                            flush=True,
                        )
                        inbound_message_id, _inbound_created = (
                            persist_inbound_whatsapp_message(
                                phone, message_id, text,
                                lead_id=conversation_store.relationship_id(
                                    routed_conversation.get("Lead")
                                ),
                                conversation_id=routed_conversation_id,
                                bubble_env="live",
                            )
                        )
                except Exception as error:
                    print(
                        "[CONVERSATION ROUTING] direction=inbound "
                        "resolution=active_phone_lookup_failed "
                        f"error={type(error).__name__}", flush=True,
                    )
            if len(ambiguous_candidates) > 1:
                clarification, ambiguity_labels = (
                    build_conversation_ambiguity_message(
                        ambiguous_candidates, "live"
                    )
                )
                print(
                    "[CONVERSATION ROUTING] ambiguity_labels_generated "
                    f"count={len(ambiguity_labels)}", flush=True,
                )
                _stop_whatsapp_typing(typing_keepalive)
                send_whatsapp_text(phone, clarification)
                print(
                    "[CONVERSATION ROUTING] direction=outbound "
                    "resolution=ambiguous action=clarification_sent "
                    "persistence=skipped_no_exact_conversation",
                    flush=True,
                )
                reply_sent = True
                return
            owner_check_response = raw_text if message_type == "text" else text
            owner_check_acknowledgement = handle_owner_check_reply(
                routed_conversation, owner_check_response, "live"
            )
            if owner_check_acknowledgement:
                if not routed_conversation_id:
                    raise RuntimeError(
                        "Owner-check reply resolved without a Conversation ID"
                    )
                if not inbound_message_id:
                    inbound_message_id, _inbound_created = (
                        persist_inbound_whatsapp_message(
                            phone, message_id, text,
                            lead_id=conversation_store.relationship_id(
                                routed_conversation.get("Lead")
                            ),
                            conversation_id=routed_conversation_id,
                            bubble_env="live",
                        )
                    )
                print(
                    "[CONVERSATION ROUTING] direction=inbound "
                    "resolution=owner_check "
                    f"conversation_id={routed_conversation_id}", flush=True,
                )
                _stop_whatsapp_typing(typing_keepalive)
                sent_ids = send_whatsapp_text(
                    phone, owner_check_acknowledgement
                )
                persist_sent_whatsapp_text(
                    phone, owner_check_acknowledgement, sent_ids,
                    routed_conversation_id,
                    lead_id=conversation_store.relationship_id(
                        routed_conversation.get("Lead")
                    ),
                    bubble_env="live",
                )
                reply_sent = True
                return
            if identity_error:
                if not inbound_message_id:
                    existing_general = find_existing_general_conversation_by_phone(
                        phone, "live"
                    )
                    existing_general_id = conversation_store.relationship_id(
                        (existing_general or {}).get("_id")
                    )
                    if existing_general_id:
                        inbound_message_id, _created = persist_inbound_whatsapp_message(
                            phone, message_id, text,
                            conversation_id=existing_general_id,
                            bubble_env="live",
                        )
                raise identity_error
            internal_user = find_internal_user(
                phone, base_url, _bubble_records, bubble, normalize_phone
            )
            safe_phone = f"...{phone[-4:]}" if phone else "unknown"
            if extract_handoff_code(text):
                lead_conversation_id = routed_conversation_id
                linked_handoff_lead_id = None
                handoff_result = handle_external_handoff_message(
                    phone, text, base_url, _bubble_records, bubble,
                    _bubble_patch, normalize_phone,
                    sender_user_id=(
                        internal_user.get("_id") if internal_user else None
                    ),
                    find_or_create_lead=find_or_create_whatsapp_lead,
                    find_or_create_folio=lambda lead_id: (
                        find_or_create_lead_folio(lead_id, "live")
                    ),
                    whatsapp_profile_name=message.get("customer_name"),
                )
                linked_handoff_lead_id = getattr(handoff_result, "lead_id", None)
                if linked_handoff_lead_id:
                    try:
                        attach_whatsapp_message_lead(
                            inbound_message_id, linked_handoff_lead_id, "live"
                        )
                    except Exception as error:
                        print(
                            "[WHATSAPP MESSAGE] inbound_lead_link_failed "
                            f"message_id={message_id} error={type(error).__name__}",
                            flush=True,
                        )
                    try:
                        lead_conversation = routed_conversation
                        if not lead_conversation:
                            handoff_lead = {
                                "_id": linked_handoff_lead_id,
                                "ActiveForwardedEnquiry": getattr(
                                    handoff_result, "enquiry_id", None
                                ),
                            }
                            lead_conversation = find_forwarded_lead_conversation(
                                handoff_lead, phone, "live"
                            )
                        lead_conversation_id = conversation_store.relationship_id(
                            (lead_conversation or {}).get("_id")
                        )
                        if lead_conversation_id:
                            inbound_message_id, _created = persist_inbound_whatsapp_message(
                                phone, message_id, text,
                                lead_id=linked_handoff_lead_id,
                                conversation_id=lead_conversation_id,
                                bubble_env="live",
                            )
                            if not routed_conversation_id:
                                print(
                                    "[CONVERSATION ROUTING] direction=inbound "
                                    f"resolution=enquiry conversation_id={lead_conversation_id} "
                                    f"enquiry_id={getattr(handoff_result, 'enquiry_id', None)}",
                                    flush=True,
                                )
                    except Exception as error:
                        print(
                            "[CONVERSATION] side=lead action=failed "
                            f"operation=handoff_association "
                            f"error={type(error).__name__}", flush=True,
                        )
                _stop_whatsapp_typing(typing_keepalive)
                sent_ids = send_whatsapp_text(phone, handoff_result.response_text)
                if lead_conversation_id:
                    persist_sent_whatsapp_text(
                        phone, handoff_result.response_text, sent_ids,
                        lead_conversation_id, linked_handoff_lead_id, "live",
                    )
                followup_text = getattr(handoff_result, "followup_text", None)
                if isinstance(followup_text, str) and followup_text.strip():
                    enquiry_id = getattr(handoff_result, "enquiry_id", None)
                    try:
                        sent_ids = send_whatsapp_text(phone, followup_text)
                        if lead_conversation_id:
                            persist_sent_whatsapp_text(
                                phone, followup_text, sent_ids,
                                lead_conversation_id, linked_handoff_lead_id, "live",
                            )
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
                    general_conversation = routed_conversation or find_general_conversation(
                        internal_user.get("_id"), phone,
                        counterparty_user_id=internal_user.get("_id"),
                        counterparty_role="Principal",
                        rentee_role="Principal Assistant", bubble_env="live",
                    )
                    general_conversation_id = conversation_store.relationship_id(
                        (general_conversation or {}).get("_id")
                    )
                    if not inbound_message_id and general_conversation_id:
                        inbound_message_id, _created = persist_inbound_whatsapp_message(
                            phone, message_id, text,
                            conversation_id=general_conversation_id,
                            bubble_env="live",
                        )
                        print(
                            "[CONVERSATION ROUTING] direction=inbound "
                            f"resolution=general conversation_id={general_conversation_id}",
                            flush=True,
                        )
                    _stop_whatsapp_typing(typing_keepalive)
                    sent_ids = send_whatsapp_text(phone, workflow_result.response_text)
                    if general_conversation_id:
                        persist_sent_whatsapp_text(
                            phone, workflow_result.response_text, sent_ids,
                            general_conversation_id, bubble_env="live",
                        )
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
            routed_lead_id = conversation_store.relationship_id(
                (routed_conversation or {}).get("Lead")
            )
            if routed_lead_id:
                lead = bubble(f"{base_url}/obj/lead/{routed_lead_id}")
                lead.setdefault("_id", routed_lead_id)
                lead_created = False
                print(
                    "[LEAD ROUTING] resolution=conversation "
                    f"lead_id={routed_lead_id} "
                    f"conversation_id={routed_conversation_id}",
                    flush=True,
                )
            else:
                linked_lead = capture_linked_tenant_profile(phone, text, "live")
                if linked_lead:
                    lead, lead_created = linked_lead, False
                else:
                    lead, lead_created = find_or_create_whatsapp_lead(
                        phone, message.get("customer_name"), "live"
                    )
                print(
                    "[LEAD ROUTING] resolution=phone_fallback "
                    f"lead_id={lead.get('_id')}",
                    flush=True,
                )
            lead_id = lead["_id"]
            lead_conversation = routed_conversation
            lead_conversation_id = routed_conversation_id
            try:
                if not lead_conversation_id:
                    lead_conversation = find_forwarded_lead_conversation(
                        lead, phone, "live"
                    )
                if not lead_conversation:
                    principal_id = conversation_store.relationship_id(
                        lead.get("owner")
                    )
                    if principal_id:
                        lead_conversation = find_general_conversation(
                            principal_id, phone, lead_id=lead_id,
                            counterparty_role="Lead", rentee_role="Lead Advisor",
                            bubble_env="live",
                        )
                    else:
                        lead_conversation = find_existing_general_conversation_by_phone(
                            phone, "live"
                        )
                lead_conversation_id = conversation_store.relationship_id(
                    (lead_conversation or {}).get("_id")
                )
                if lead_conversation_id:
                    if not inbound_message_id:
                        inbound_message_id, _created = persist_inbound_whatsapp_message(
                            phone, message_id, text, lead_id=lead_id,
                            conversation_id=lead_conversation_id, bubble_env="live",
                        )
                    enquiry_id = conversation_store.relationship_id(
                        (lead_conversation or {}).get("Enquiry")
                    )
                    print(
                        "[CONVERSATION ROUTING] direction=inbound "
                        f"resolution={'enquiry' if enquiry_id else 'general'} "
                        f"conversation_id={lead_conversation_id}"
                        + (f" enquiry_id={enquiry_id}" if enquiry_id else ""),
                        flush=True,
                    )
                else:
                    print(
                        "[CONVERSATION ROUTING] direction=inbound "
                        f"action=unresolved reason=missing_principal lead_id={lead_id}",
                        flush=True,
                    )
            except Exception as error:
                print(
                    "[CONVERSATION] side=lead action=failed "
                    f"operation=inbound_association error={type(error).__name__}",
                    flush=True,
                )
            try:
                attach_whatsapp_message_lead(inbound_message_id, lead_id, "live")
            except Exception as error:
                print(
                    "[WHATSAPP MESSAGE] inbound_lead_link_failed "
                    f"message_id={message_id} error={type(error).__name__}",
                    flush=True,
                )
            is_search_conversation = not conversation_store.relationship_id(
                (lead_conversation or {}).get("Enquiry")
            )
            if is_search_conversation and not _has_strong_property_search_clue(
                text, "live"
            ):
                try:
                    confirmation = build_prefilled_search_confirmation(lead, "live")
                    if confirmation and not conversation_has_outbound_message(
                        lead_conversation_id, "live"
                    ):
                        _stop_whatsapp_typing(typing_keepalive)
                        sent_ids = send_whatsapp_text(phone, confirmation)
                        persist_sent_whatsapp_text(
                            phone, confirmation, sent_ids,
                            lead_conversation_id, lead_id, "live",
                        )
                        print(
                            "[SEARCH ONBOARDING] action=baseline_confirmation_sent "
                            f"conversation_id={lead_conversation_id} lead_id={lead_id}",
                            flush=True,
                        )
                        reply_sent = True
                        return
                except Exception as error:
                    print(
                        "[SEARCH ONBOARDING] action=baseline_confirmation_failed "
                        f"conversation_id={lead_conversation_id} "
                        f"error={type(error).__name__}",
                        flush=True,
                    )
            folio_id, folio_created = find_or_create_lead_folio(lead_id, "live")
            if not lead_conversation_id:
                raise RuntimeError("Cannot persist WhatsApp turn without Conversation")
            previous_response_id = (
                conversation_store.get_conversation_previous_response_id(
                    lead_conversation
                )
            )
            current_message_id = create_whatsapp_ai_message(
                lead_id, phone, "live", lead_conversation_id
            )
            print(
                "[CONVERSATION ROUTING] direction=outbound "
                f"conversation_id={lead_conversation_id} "
                f"enquiry_id={conversation_store.relationship_id((lead_conversation or {}).get('Enquiry')) or 'none'}",
                flush=True,
            )
            print(
                "[WHATSAPP CONVERSATION] "
                f"phone={safe_phone} lead_id={lead_id} lead_created={lead_created} "
                f"folio_id={folio_id} folio_created={folio_created} "
                f"previous_response_id={previous_response_id}",
                flush=True,
            )
            print(
                f"[WHATSAPP CONVERSATION] current_message_id={current_message_id}",
                flush=True,
            )
            model_context = conversation_model_context(
                lead_conversation, lead, "live"
            )
            exact_listing_context = whatsapp_reply_listing_context(
                reply_listing_id, "live"
            )
            if exact_listing_context:
                model_context += "\n\n" + exact_listing_context
            location_listing_id = reply_listing_id or conversation_store.relationship_id(
                (lead_conversation or {}).get("Listing")
            )
            answer, response_id, recommendation_run = run_rentee_turn(
                text, folio_id, previous_response_id=previous_response_id,
                message_id=current_message_id, bubble_env="live",
                conversation_context=model_context,
                **({"reply_listing_id": location_listing_id}
                   if location_listing_id else {}),
            )
            if _requests_owner_check(answer):
                owner_check = prepare_owner_check(lead, "live")
                if owner_check:
                    send_pending_owner_check(lead, owner_check, "live")
            recommendation_listings = []
            if isinstance(recommendation_run, list):
                recommendation_listings = recommendation_run
                recommendation_summary = build_whatsapp_recommendation_summary(
                    folio_id, "live", listings=recommendation_listings
                )
                if recommendation_summary:
                    answer = recommendation_summary
                print(
                    f"[RECOMMENDATION RUN] folio_id={folio_id} "
                    f"summary_listing_count={len(recommendation_listings)}",
                    flush=True,
                )
            save_whatsapp_ai_message(
                current_message_id, answer, response_id, "live"
            )
            if lead_conversation_id and response_id:
                try:
                    conversation_store.set_conversation_previous_response_id(
                        lead_conversation_id, response_id, "live"
                    )
                    print(
                        "[CONVERSATION ROUTING] direction=outbound "
                        "action=previous_response_saved "
                        f"conversation_id={lead_conversation_id}", flush=True,
                    )
                except Exception as error:
                    print(
                        "[CONVERSATION] side=lead action=failed "
                        "operation=previous_response_update "
                        f"error={type(error).__name__}", flush=True,
                    )
            print(
                f"[WHATSAPP AI SEND] lead_id={lead_id} "
                f"destination=enquirer phone={_masked_whatsapp_phone(phone)}",
                flush=True,
            )
            _stop_whatsapp_typing(typing_keepalive)
            if recommendation_listings:
                outbound_deliveries = send_whatsapp_recommendation_batch(
                    phone, folio_id, recommendation_listings
                )
                if isinstance(outbound_deliveries, list) and outbound_deliveries:
                    persist_recommendation_whatsapp_ids(
                        current_message_id, outbound_deliveries, phone,
                        lead_conversation_id, lead_id, "live",
                    )
            else:
                outbound_ids = send_whatsapp_text(phone, answer)
                if isinstance(outbound_ids, list) and outbound_ids:
                    try:
                        if lead_conversation_id:
                            save_whatsapp_message_id(
                                current_message_id, outbound_ids[0], "live",
                                lead_conversation_id,
                            )
                        else:
                            save_whatsapp_message_id(
                                current_message_id, outbound_ids[0], "live"
                            )
                    except Exception as error:
                        print(
                            "[WHATSAPP MESSAGE] outbound_id_persistence_failed "
                            f"message_id={current_message_id} "
                            f"error={type(error).__name__}", flush=True,
                        )
            reply_sent = True
            print(
                "[WHATSAPP CONVERSATION] "
                f"current_message_id={current_message_id} "
                f"saved_response_id={response_id}",
                flush=True,
            )
    except Exception as error:
        if message_type == "audio":
            print(
                f"[WHATSAPP AUDIO] message_id={message_id} "
                f"action=failed error={type(error).__name__}", flush=True,
            )
        else:
            print(f"WhatsApp message {message_id} failed: {error}", flush=True)
        if not reply_sent:
            try:
                _stop_whatsapp_typing(typing_keepalive)
                send_whatsapp_text(
                    phone,
                    WHATSAPP_AUDIO_ERROR_RESPONSE if message_type == "audio" else
                    "Sorry, I had trouble checking that just now. Please try again."
                )
            except Exception as send_error:
                print(f"WhatsApp fallback send failed: {send_error}", flush=True)
    finally:
        _stop_whatsapp_typing(typing_keepalive)
        with _whatsapp_processing_lock:
            _whatsapp_processing_ids.discard(message_id)
            if message_id not in _whatsapp_processed_ids:
                if len(_whatsapp_processed_order) == _whatsapp_processed_order.maxlen:
                    _whatsapp_processed_ids.discard(_whatsapp_processed_order[0])
                _whatsapp_processed_order.append(message_id)
                _whatsapp_processed_ids.add(message_id)


def _whatsapp_text_messages(payload):
    """Extract supported inbound text/audio messages; ignore statuses and others."""
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
                message_type = item.get("type")
                if message_type not in {"text", "audio"}:
                    continue
                if not item.get("id") or not item.get("from"):
                    continue
                if message_type == "text":
                    body = (item.get("text") or {}).get("body")
                    if not isinstance(body, str) or not body.strip():
                        continue
                else:
                    media_id = (item.get("audio") or {}).get("id")
                    if not isinstance(media_id, str) or not media_id.strip():
                        # Keep a genuine audio event so the worker can log and send
                        # the standard transcription failure response.
                        item = {**item, "audio": {**(item.get("audio") or {})}}
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
