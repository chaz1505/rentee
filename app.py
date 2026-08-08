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
            "When the user asks about properties, recommendations, or suitable listings, "
            "use the property matching tool to identify the best available options. "
            "When the user provides new, changed, removed, or additional information "
            "that could affect which properties suit them, use the update_preferences tool. "
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
            "Use this tool whenever the user asks for suitable properties, "
            "property recommendations, matching listings, ranking listings, "
            "or identifying the best properties for the current home seeker."
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
            "Use this whenever the customer provides new, changed, removed, or "
            "additional information that could affect property suitability. This "
            "includes any home-search preference, such as budget, location, property "
            "type, bedrooms, school or commute needs, family needs, parking, pets, "
            "furnishing, size, facilities, or timing. Do not use it for general "
            "questions or a request to show properties."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "preference_update": {
                    "type": "string",
                    "description": "The relevant new or changed customer preference."
                }
            },
            "required": ["preference_update"],
            "additionalProperties": False
        }
    }
]
    }

    if previous_response_id:
        args["previous_response_id"] = previous_response_id

    return args


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
        json={"folioItems": folio_item_ids},
        timeout=30
    )
    response.raise_for_status()


def match_lead(folio_id, bubble_env):

    base_url = get_bubble_base_url(bubble_env)
    folio = bubble(f"{base_url}/obj/folio/{folio_id}")
    lead_id = folio["lead"]
    print(f"Folio {folio_id} -> Lead {lead_id}", flush=True)
    lead = bubble(f"{base_url}/obj/lead/{lead_id}")

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

"""

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

    folio_item_ids = create_folio_items(recommended_listing_ids, base_url)

    if folio_item_ids is not None:
        try:
            update_folio_items(folio_id, folio_item_ids, base_url)
        except Exception as error:
            print(f"Failed to update Folio Items: {error}", flush=True)

    return result["customer_response"]


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
                # The initial turn carries the incoming response ID, preserving
                # the user's existing conversation history.
                response = client.responses.create(
                    **build_response_args(message, previous)
                )
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
                    yield (
                        f"data: {json.dumps({'done': True, 'response_id': response.id})}\n\n"
                    )
                    return

                tool_args = json.loads(tool_call.arguments)

                if tool_call.name == "match_lead":
                    tool_result = match_lead(folio_id, bubble_env)
                elif tool_call.name == "update_preferences":
                    tool_result = update_preferences(
                        folio_id,
                        tool_args["preference_update"],
                        bubble_env
                    )
                else:
                    raise ValueError(f"Unsupported tool: {tool_call.name}")

                # Continue the same response chain with the function result,
                # then stream the final assistant answer back to Bubble.
                with client.responses.stream(
                        model="gpt-5-mini",
                        previous_response_id=response.id,
                        input=[{
                            "type": "function_call_output",
                            "call_id": tool_call.call_id,
                            "output": tool_result
                        }]
                ) as stream:
                    for event in stream:
                        if event.type == "response.output_text.delta":
                            yield f"data: {json.dumps({'delta': event.delta})}\n\n"

                    final = stream.get_final_response()

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
