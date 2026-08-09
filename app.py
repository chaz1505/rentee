from flask import Flask, request, jsonify, Response, stream_with_context
from flask_cors import CORS
from openai import OpenAI
import os
import requests
import json

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
MATCH_LISTING_LIMIT = 200


def get_bubble_base_url(bubble_env):
    if bubble_env == "development":
        return "https://www.rentee.asia/version-test/api/1.1"
    return "https://www.rentee.asia/api/1.1"


@app.route("/")
def home():
    return jsonify({"status": "running"})


def build_response_args(user_message, previous_response_id=None):
    args = {
        "model": "gpt-5-mini",
        "input": user_message,
        "instructions": (
            "You are Rentee, a friendly and highly capable personal property "
            "assistant helping a home seeker find their ideal property in Kuala Lumpur. "
            "You are speaking directly to the property seeker, not to an estate agent. "
            "Always address the user naturally using 'you' and 'your'. Be helpful, "
            "conversational, concise, and proactive. "
            "When the user asks about properties, recommendations, or suitable listings "
            "based on their current requirements, use the property matching tool to identify "
            "the best available options. "
            "Use web search when answering questions that depend on current, changing, "
            "external, or internet-based information, or when you are materially uncertain "
            "about an external factual claim that can be verified online. Prefer searching "
            "rather than guessing for neighbourhood developments, infrastructure, transport, "
            "schools, amenities, regulations, taxes, market information, and developer or "
            "project news. Do not use web search as a source of current Rentee listing "
            "availability: current available properties and recommendations must come from "
            "match_lead. Facts about a currently available unit, including price, bedrooms, "
            "bathrooms, size, furnishing, parking, facilities, availability, photos, and "
            "floorplans, must come only from current Rentee listing data. Web search may be "
            "used for external context about a building, neighbourhood, or surrounding area, "
            "but must not overwrite or contradict authoritative listing data. "
            "After every get_property_details result, check whether it actually answers the "
            "user's question. If the requested fact is missing and is a publicly discoverable "
            "building, development, location, neighbourhood, transport, school, amenity, "
            "regulatory, developer, historical, or other external fact, immediately use web "
            "search before answering. The user should never need to ask you to search the web. "
            "If a missing fact is specific to the individual available unit, such as its "
            "balcony, facing direction, owner decisions, current availability, or parking "
            "allocation, do not use web search or guess; explain that the current listing "
            "information does not specify it. "
            "Use match_lead for current availability and recommendations, update_preferences "
            "for stored home-search requirements, and get_property_details for factual "
            "questions about a specific listing, unit, condo, building, or development. Do "
            "not call match_lead merely to answer a factual property question. When a property "
            "is already being discussed, use get_property_details rather than rerunning "
            "matching. Prefer authoritative Rentee property data over web search whenever the "
            "requested information exists in Rentee. "
            "Whenever the user asks about currently available properties, suitable listings, "
            "options, recommendations, or what is available for them, you MUST call the "
            "match_lead tool. Never answer current property availability or recommendations "
            "from conversation history; only a CURRENT match_lead result may be used to "
            "describe available properties. Even if properties were mentioned earlier, do "
            "not repeat, recall, summarise, or rely on them when the user asks what is "
            "currently available: always call match_lead again. "
            "When the user tells you something that adds to, changes, replaces, narrows, "
            "removes, or otherwise modifies their home-search requirements, you MUST call "
            "the update_preferences tool rather than merely acknowledging the change in chat. "
            "The user does not need to ask to update their preferences explicitly. Statements "
            "such as 'I'm only interested in Serai now', 'My budget is now RM20k', 'We need "
            "four bedrooms', 'We're also open to Mont Kiara', 'My children will attend the "
            "British School', and 'We no longer need a pool' MUST call update_preferences. "
            "Ordinary chat questions must not call update_preferences. A preference change "
            "by itself must use update_preferences only; do not run property matching unless "
            "the user explicitly asks to see, find, compare, match, rank, shortlist, or "
            "receive recommendations for properties. "
            "If a property-search request itself introduces or changes a specific condo, "
            "building, area, or location preference, you MUST call update_preferences first, "
            "not match_lead. For example, 'find me units in Serai', 'show me condos in Mont "
            "Kiara', 'find me One Menerung units', 'show me properties in Bangsar', and 'I "
            "want to see units in Damansara Heights' are preference updates. General requests "
            "such as 'what do you have for me?', 'show me my best matches', 'what properties "
            "are available?', or 'find me something suitable' must call match_lead directly. "
            "Never treat property details in previous conversation history as authoritative "
            "listing information. Property-specific facts, including names, units, prices, "
            "bedrooms, bathrooms, size, furnishing, facilities, availability, parking, "
            "addresses, commute times, photos, or floorplans, may only be stated when they "
            "come from the CURRENT successful match_lead tool result. "
            "After update_preferences succeeds, the application refreshes recommendations; "
            "only describe fresh properties from that current tool result, never from "
            "conversation history. Never claim a property is currently available because it "
            "was mentioned earlier. Conversation history is for normal continuity, not a "
            "database of property facts. Do not offer actions the system "
            "cannot actually perform, such as contacting an owner or agent, arranging "
            "viewings, sending photos, obtaining a floorplan, confirming information "
            "privately, or checking exact commute times. "
            "Explain recommendations in clear, customer-friendly language. "
            "Do not expose internal listing IDs, Lead IDs, Folio IDs, database fields, "
            "tool names, or other internal system information. Do not talk about 'the lead' "
            "or 'the client', or sound like an internal estate-agent assistant. "
            "If a property has an important limitation or data issue, explain it clearly "
            "and calmly without exposing internal data structures. "
            "For general questions, answer normally without using the matching tool unless "
            "the user asks for recommendations or which available properties suit them. "
            "Never invent property details; only state property-specific facts present in "
            "the supplied data."
        ),
        "tool_choice": "auto",
        "tools": [
    {
        "type": "function",
        "name": "match_lead",
        "description": (
            "Use this whenever the user asks to see, find, recommend, list, show, "
            "shortlist, rank, compare, recall, or discuss currently available properties "
            "that may suit them. This includes requests such as 'What do you have for me?', "
            "'What have you got?', 'Show me some options', 'What properties are available?', "
            "'Anything suitable?', 'Can you find something for me?', 'What are my best "
            "options?', 'Show me what matches', and 'What can "
            "I see?'. The user does not need to explicitly ask to match listings or recommend "
            "properties. Do not use it for a statement that only changes preferences, or when "
            "the request introduces or changes a specific condo, building, area, or location; "
            "use update_preferences first in that case."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "update_preferences",
        "description": (
            "Use this whenever the user states new, changed, removed, additional, or "
            "clarified information that could affect which home is suitable for them. "
            "This includes budget, areas, condos, bedrooms, bathrooms, property type, "
            "buy/rent, schools, commute, parking, pets, furnishing, size, facilities, "
            "family requirements, lifestyle preferences, move-in timing, or any other "
            "information relevant to finding the right home. The user does not need to "
            "explicitly ask to save or update their preferences. If they say 'I'm only "
            "interested in Serai now', you MUST call this tool rather than simply "
            "acknowledging it in chat. Also use it when a property-search request introduces "
            "or changes a specific condo, building, area, or location, such as 'find me units "
            "in Serai' or 'show me condos in Mont Kiara'. Do not use it for ordinary questions "
            "or general property requests that do not introduce a new preference."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "preference_update": {
                    "type": "string",
                    "description": (
                        "A concise description of the new, changed, removed, or additional "
                        "home-search information stated by the user."
                    )
                }
            },
            "required": ["preference_update"],
            "additionalProperties": False
        }
    },
    {
        "type": "function",
        "name": "get_property_details",
        "description": (
            "Retrieve authoritative Rentee information about a specific listing, unit, "
            "condo, building, or development. Use this for factual questions about a "
            "property already being discussed. Do not use it to search for or recommend "
            "available properties."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "property_reference": {
                    "type": "string",
                    "description": (
                        "The property, condo, building, unit, or listing being referred to, "
                        "using the user's wording or conversational context."
                    )
                }
            },
            "required": ["property_reference"],
            "additionalProperties": False
        }
    },
    {
        "type": "web_search"
    }
]
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


def bubble(url, **kwargs):

    r = requests.get(url, timeout=30, **kwargs)

    r.raise_for_status()

    return r.json()["response"]


def get_all_listings(base_url):

    listings = []
    cursor = 0
    seen_cursors = set()

    while cursor not in seen_cursors:
        seen_cursors.add(cursor)
        page = bubble(f"{base_url}/obj/listing", params={"cursor": cursor})
        results = page.get("results", [])
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


def create_folio_items(listing_ids, base_url):

    folio_item_ids = []

    for listing_id in listing_ids:
        try:
            response = requests.post(
                f"{base_url}/obj/folioItem",
                headers={
                    "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
                    "Content-Type": "application/json"
                },
                json={"listing": listing_id},
                timeout=30
            )
            response.raise_for_status()
            data = response.json()
            folio_item_id = data.get("id")

            if not folio_item_id:
                raise ValueError("Bubble did not return a Folio Item ID.")

            folio_item_ids.append(folio_item_id)
        except Exception as error:
            print(f"Failed to create Folio Item: {error}", flush=True)
            return None

    return folio_item_ids


def update_folio_items(folio_id, folio_item_ids, base_url):

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


def match_lead(folio_id, bubble_env):

    yield "Checking your preferences..."
    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    existing_folio_item_ids = list(folio.get("folioItems", []) or [])
    existing_listing_ids = set()

    for existing_folio_item_id in existing_folio_item_ids:
        existing_folio_item = bubble(
            f"{base_url}/obj/folioItem/{existing_folio_item_id}"
        )
        existing_listing_id = existing_folio_item.get("listing")

        if existing_listing_id:
            existing_listing_ids.add(existing_listing_id)

    lead_id = folio["lead"]
    print(f"Folio {folio_id} -> Lead {lead_id}", flush=True)
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")

    yield "Searching available properties..."
    listings = get_all_listings(base_url)[:MATCH_LISTING_LIMIT]

    print(
        f"Scoring {len(listings)} listings (test limit: {MATCH_LISTING_LIMIT})",
        flush=True
    )

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
- recommended_listing_ids: an array containing only INTERNAL LISTING IDs from
  the supplied properties, in the same order as your ranking. Include only
  properties you genuinely recommend; never invent an ID or add properties to
  fill a list.
- customer_response: concise, natural, customer-facing recommendation prose.
  Never mention internal IDs, Folio IDs, Lead IDs, database fields, or the
  matching process.

The recommended_listing_ids are the source of truth. customer_response must
describe only the listings represented by those IDs, in the same order. Never
invent a property, unit, building name, or property detail. If a name or detail
is not in the supplied property information, do not mention it.

"""

    yield "Ranking the best matches..."
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
                        "recommended_listing_ids": {
                            "type": "array",
                            "items": {"type": "string"}
                        },
                        "customer_response": {"type": "string"}
                    },
                    "required": ["recommended_listing_ids", "customer_response"],
                    "additionalProperties": False
                }
            }
        }

    )

    print("Matching model response received", flush=True)
    try:
        result = json.loads(response.output_text)
    except json.JSONDecodeError as error:
        print(f"Failed to parse matching JSON: {error}", flush=True)
        return "I’m sorry, I couldn’t prepare your recommendations just now."

    available_listing_ids = {
        listing["_id"]
        for listing in listings
        if listing.get("_id")
    }
    recommended_listing_ids = []

    for listing_id in result["recommended_listing_ids"]:
        if listing_id not in available_listing_ids:
            print("Ignoring invalid recommended listing ID", flush=True)
        elif listing_id not in recommended_listing_ids:
            recommended_listing_ids.append(listing_id)

    new_listing_ids = [
        listing_id
        for listing_id in recommended_listing_ids
        if listing_id not in existing_listing_ids
    ]

    yield "Updating your shortlist..."

    if new_listing_ids:
        new_folio_item_ids = create_folio_items(new_listing_ids, base_url)

        if new_folio_item_ids is not None:
            final_folio_item_ids = existing_folio_item_ids + new_folio_item_ids
            try:
                update_folio_items(folio_id, final_folio_item_ids, base_url)
            except Exception as error:
                print(f"Failed to update Folio Items: {error}", flush=True)

    return result["customer_response"]


def stream_match_lead(folio_id, bubble_env):

    match_flow = match_lead(folio_id, bubble_env)

    while True:
        try:
            status = next(match_flow)
        except StopIteration as completed:
            return completed.value

        yield f"data: {json.dumps({'status': status})}\n\n"


def update_lead_ai_searchtext(lead_id, updated_text, base_url):

    print(f"Updating AIsearchtext for lead {lead_id}", flush=True)
    response = requests.patch(
        f"{base_url}/obj/lead/{lead_id}",
        headers={
            "Authorization": f"Bearer {BUBBLE_API_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"AIsearchtext": updated_text},
        timeout=30
    )
    response.raise_for_status()
    print("AIsearchtext updated successfully", flush=True)


def update_preferences(folio_id, preference_update, bubble_env):

    base_url = get_bubble_base_url(bubble_env)
    print(f"Updating preferences for folio: {folio_id}", flush=True)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    lead_id = folio["lead"]
    print(f"Resolved lead: {lead_id}", flush=True)
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")
    existing_ai_search_text = lead.get("AIsearchtext", "")

    update_prompt = f"""
You maintain a living home-search profile for one customer.

Return the complete updated AIsearchtext after applying the requested update.

Rules:
- Preserve all existing relevant home-search information.
- Change or remove a preference only when the customer explicitly says to do so.
- Add relevant new information, creating an appropriate structured category when needed.
- Do not invent or infer preferences.
- Do not rewrite, summarise, clean up, reorder, or delete any `secret notes` or
  dated conversation/history content. It is immutable and must remain exactly
  as written.
- Do not summarise away, delete, or rewrite unrelated preferences.

CURRENT AIsearchtext:
{existing_ai_search_text}

REQUESTED PREFERENCE UPDATE:
{preference_update}
"""

    response = client.responses.create(
        model="gpt-5-mini",
        input=update_prompt,
        instructions=(
            "Return JSON matching the supplied schema. The confirmation must be a "
            "short, natural sentence addressed directly to the customer and must not "
            "mention internal IDs, fields, APIs, or tools."
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
                        "confirmation": {"type": "string"}
                    },
                    "required": ["updated_ai_search_text", "confirmation"],
                    "additionalProperties": False
                }
            }
        }
    )
    result = json.loads(response.output_text)
    updated_ai_search_text = result["updated_ai_search_text"]

    if not updated_ai_search_text.strip():
        raise ValueError("The updated home-search profile was empty.")

    update_lead_ai_searchtext(lead_id, updated_ai_search_text, base_url)

    return result["confirmation"]


@app.route("/chat_stream", methods=["POST"])
def chat_stream():

    try:

        data = request.get_json(silent=True) or {}
        folio_id = data.get("folio_id")
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
                # The initial turn carries the incoming response ID, preserving
                # the user's existing conversation history.
                try:
                    response = client.responses.create(
                        **build_response_args(message, previous)
                    )
                except Exception as error:
                    if "No tool output found for function call" not in str(error):
                        raise

                    print(
                        "Broken previous_response_id detected; starting a fresh conversation",
                        flush=True
                    )
                    response = client.responses.create(
                        **build_response_args(message, None)
                    )
                if any(
                    output_item.type == "web_search_call"
                    for output_item in response.output
                ):
                    print("Web search used", flush=True)
                    yield (
                        f"data: {json.dumps({'status': 'Searching the web for the latest information...'})}\n\n"
                    )
                    web_search_status_sent = True
                tool_call = next(
                    (x for x in response.output if x.type == "function_call"),
                    None
                )

                if tool_call is None:
                    print("No tool call requested", flush=True)
                    for i in range(0, len(response.output_text), 25):
                        yield (
                            f"data: {json.dumps({'delta': response.output_text[i:i + 25]})}\n\n"
                        )
                    citations = get_web_citations(response)

                    if citations:
                        yield f"data: {json.dumps({'citations': citations})}\n\n"
                    yield (
                        f"data: {json.dumps({'done': True, 'response_id': response.id})}\n\n"
                    )
                    return

                original_response_id = response.id
                original_call_id = tool_call.call_id
                print(f"Tool selected: {tool_call.name}", flush=True)
                print(f"Original call_id: {original_call_id}", flush=True)
                tool_args = json.loads(tool_call.arguments)
                follow_up_tools = None

                if tool_call.name == "match_lead":
                    tool_result = yield from stream_match_lead(folio_id, bubble_env)
                    has_match_results = True
                    follow_up_instructions = (
                        "The tool output already contains the final customer-facing answer. "
                        "Return it faithfully. Do not add, remove, reinterpret, embellish, "
                        "or invent property information."
                    )
                elif tool_call.name == "update_preferences":
                    yield (
                        f"data: {json.dumps({'status': 'Updating your preferences...'})}\n\n"
                    )
                    preference_confirmation = update_preferences(
                        folio_id,
                        tool_args["preference_update"],
                        bubble_env
                    )
                    try:
                        print(
                            "Preference update complete; running automatic rematch",
                            flush=True
                        )
                        yield (
                            f"data: {json.dumps({'status': 'Preferences updated — refreshing your recommendations...'})}\n\n"
                        )
                        recommendations = yield from stream_match_lead(
                            folio_id,
                            bubble_env
                        )
                        print("Automatic rematch complete", flush=True)
                        has_match_results = True
                        tool_result = (
                            "Absolutely — I've updated your preferences. Based on that, "
                            "here's what I'd recommend now:\n\n"
                            f"{recommendations}"
                        )
                        follow_up_instructions = (
                            "The tool output already contains the final customer-facing "
                            "recommendations. Return it faithfully without adding, removing, "
                            "or inventing property information."
                        )
                    except Exception as error:
                        print(f"Matching after preference update failed: {error}", flush=True)
                        has_match_results = False
                        tool_result = preference_confirmation
                        follow_up_instructions = (
                            "Return the completed preference-update confirmation naturally. "
                            "Do not mention properties or internal errors."
                        )
                elif tool_call.name == "get_property_details":
                    has_match_results = False
                    yield (
                        f"data: {json.dumps({'status': 'Checking property details...'})}\n\n"
                    )
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
                else:
                    raise ValueError(f"Unsupported tool: {tool_call.name}")

                if has_match_results:
                    yield (
                        f"data: {json.dumps({'status': 'Found some options — putting them together...'})}\n\n"
                    )

                # Continue the same response chain with the function result,
                # then stream the final assistant answer back to Bubble.
                if tool_call.name == "update_preferences":
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

                with client.responses.stream(**continuation_args) as stream:
                    for event in stream:
                        if (
                            event.type.startswith("response.web_search_call.")
                            and not web_search_status_sent
                        ):
                            print("Web search used", flush=True)
                            status = (
                                "That detail isn’t in the listing — checking the web..."
                                if property_details_web_fallback
                                else "Searching the web for the latest information..."
                            )
                            yield (
                                f"data: {json.dumps({'status': status})}\n\n"
                            )
                            web_search_status_sent = True
                        if event.type == "response.output_text.delta":
                            yield f"data: {json.dumps({'delta': event.delta})}\n\n"

                    final = stream.get_final_response()

                print("Tool lifecycle completed", flush=True)

                citations = get_web_citations(final)

                if citations:
                    yield f"data: {json.dumps({'citations': citations})}\n\n"

                yield (
                    f"data: {json.dumps({'done': True, 'response_id': final.id})}\n\n"
                )
            except Exception as error:
                print(f"/chat_stream failed: {error}", flush=True)
                yield f"data: {json.dumps({'error': str(error), 'done': True})}\n\n"

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

        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":

    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 10000))
    )
