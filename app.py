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

SEARCH_URL = "https://www.rentee.asia/api/1.1/wf/search_listings"
CONDO_URL = "https://www.rentee.asia/api/1.1/obj/condo"
# Bubble Data API endpoints. These are configurable in case the live and
# development Bubble apps use different domains or API type slugs.
LEAD_URL = os.environ.get(
    "BUBBLE_LEAD_URL", "https://www.rentee.asia/api/1.1/obj/lead"
)
LISTING_URL = os.environ.get(
    "BUBBLE_LISTING_URL", "https://www.rentee.asia/api/1.1/obj/listing"
)
SCORING_BATCH_SIZE = 40


@app.route("/")
def home():
    return jsonify({"status": "running"})


def build_response_args(user_message, previous_response_id=None):
    args = {
        "model": "gpt-5-mini",
        "input": user_message,
        "instructions": (
            "You are Rentee AI, a Kuala Lumpur property assistant. "
            "Always remember the previous conversation. "
            "Never expose internal listing IDs."
        ),
        "tool_choice": "auto",
        "tools": [
            {
                "type": "function",
                "name": "search_listings",
                "description": "Search the Rentee property database.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "min_beds": {"type": "integer"},
                        "priceRent": {"type": "number"},
                        "priceSale": {"type": "number"},
                        "condoName": {"type": "string"},
                        "transactionType": {"type": "string"}
                    },
                    "additionalProperties": False
                }
            },
            {
                "type": "function",
                "name": "score_lead_listings",
                "description": (
                    "Rank every Listing against one Lead's requirements using "
                    "their AIsearchtext fields. Use this when the user asks for "
                    "matches or recommendations for a Lead and provides its Bubble "
                    "unique ID."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {
                            "type": "string",
                            "description": "The Bubble unique ID of the Lead."
                        }
                    },
                    "required": ["lead_id"],
                    "additionalProperties": False
                }
            }
        ]
    }

    if previous_response_id:
        args["previous_response_id"] = previous_response_id

    return args


def search_listings(tool_args):

    r = requests.get(
        SEARCH_URL,
        params={
            "min_beds": tool_args.get("min_beds", 0)
        },
        timeout=20
    )

    r.raise_for_status()

    listings = r.json()["response"]["listing"]

    condo_cache = {}

    ui = []
    gpt = []

    for listing in listings:

        condo_id = listing.get("condo")

        if not condo_id:
            continue

        if condo_id not in condo_cache:

            c = requests.get(
                f"{CONDO_URL}/{condo_id}",
                timeout=20
            )

            c.raise_for_status()

            condo_cache[condo_id] = (
                c.json()
                .get("response", {})
                .get("name", "Unknown Condo")
            )

        name = condo_cache[condo_id]

        ui.append({
            "listing_id": listing.get("_id"),
            "condo": name,
            "beds": listing.get("beds"),
            "baths": listing.get("baths"),
            "price_rent": listing.get("priceRent"),
            "price_sale": listing.get("priceSale"),
            "transactionType": listing.get("transactionType")
        })

        gpt.append({
            "condo": name,
            "beds": listing.get("beds"),
            "baths": listing.get("baths"),
            "price_rent": listing.get("priceRent"),
            "price_sale": listing.get("priceSale")
        })

    return ui, gpt


def bubble_response(url, **kwargs):
    """Get and unwrap a public Bubble Data API response."""
    response = requests.get(url, timeout=30, **kwargs)
    response.raise_for_status()
    return response.json().get("response", {})


def get_all_listings():
    """Load every Listing from Bubble, following its cursor pagination."""
    listings = []
    cursor = None

    while True:
        params = {"cursor": cursor} if cursor else {}
        page = bubble_response(LISTING_URL, params=params)
        results = page.get("results", page.get("listing", []))

        if not isinstance(results, list):
            raise ValueError("Bubble Listing API did not return a list of results.")

        listings.extend(results)
        next_cursor = page.get("cursor")

        if not page.get("remaining") or not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    return listings


def score_listing_batch(lead_search_text, listings):
    """Ask the LLM for consistent 0-100 match scores for one small batch."""
    candidates = [
        {"index": index, "AIsearchtext": listing["AIsearchtext"]}
        for index, listing in enumerate(listings)
    ]
    scoring_prompt = (
        "Score how well each property listing matches the lead's requirements. "
        "Use only the supplied AIsearchtext values. A score of 100 is an excellent "
        "match; 0 is clearly unsuitable. Return one result for every index.\n\n"
        f"Lead AIsearchtext:\n{lead_search_text}\n\n"
        f"Listings:\n{json.dumps(candidates, ensure_ascii=False)}"
    )
    response = client.responses.create(
        model="gpt-5-mini",
        input=scoring_prompt,
        instructions=(
            "You are a precise real-estate matching engine. Do not invent facts. "
            "Return JSON that exactly matches the requested schema."
        ),
        text={
            "format": {
                "type": "json_schema",
                "name": "listing_match_scores",
                "strict": True,
                "schema": {
                    "type": "object",
                    "properties": {
                        "scores": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "index": {"type": "integer"},
                                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                                    "reason": {"type": "string"}
                                },
                                "required": ["index", "score", "reason"],
                                "additionalProperties": False
                            }
                        }
                    },
                    "required": ["scores"],
                    "additionalProperties": False
                }
            }
        }
    )
    return json.loads(response.output_text)["scores"]


def score_lead_listings(tool_args):
    """Return all Bubble Listings ranked by their semantic match to a Lead."""
    lead_id = tool_args["lead_id"]
    lead = bubble_response(f"{LEAD_URL}/{lead_id}")
    lead_search_text = lead.get("AIsearchtext")

    if not isinstance(lead_search_text, str) or not lead_search_text.strip():
        raise ValueError("This Lead has no AIsearchtext to match against.")

    scored = []
    listings = get_all_listings()

    for start in range(0, len(listings), SCORING_BATCH_SIZE):
        batch = listings[start:start + SCORING_BATCH_SIZE]
        scoreable = [
            listing
            for listing in batch
            if isinstance(listing.get("AIsearchtext"), str)
            and listing["AIsearchtext"].strip()
        ]

        for listing in batch:
            if listing not in scoreable:
                scored.append({
                    "listing": listing,
                    "score": 0,
                    "reason": "No AIsearchtext is available for this listing."
                })

        if not scoreable:
            continue

        scores_by_index = {
            result["index"]: result
            for result in score_listing_batch(lead_search_text, scoreable)
            if 0 <= result.get("index", -1) < len(scoreable)
        }
        for index, listing in enumerate(scoreable):
            result = scores_by_index.get(index, {})
            scored.append({
                "listing": listing,
                "score": max(0, min(100, int(result.get("score", 0)))),
                "reason": result.get("reason", "The listing could not be scored.")
            })

    scored.sort(key=lambda item: item["score"], reverse=True)

    ui = [
        {
            **item["listing"],
            "listing_id": item["listing"].get("_id"),
            "score": item["score"],
            "reason": item["reason"]
        }
        for item in scored
    ]
    # The assistant only needs the best results to explain its recommendation;
    # keeping this small prevents a large catalogue from exhausting its context.
    gpt = [
        {
            "score": item["score"],
            "reason": item["reason"],
            "AIsearchtext": item["listing"].get("AIsearchtext", "")
        }
        for item in scored[:20]
    ]
    return ui, gpt


@app.route("/chat", methods=["POST"])
def chat():

    try:

        data = request.get_json(silent=True) or {}

        previous = data.get("previous_response_id")

        if previous in ("", "null"):
            previous = None

        response = client.responses.create(
            **build_response_args(
                data.get("message", "Show me 3 bed condos"),
                previous
            )
        )

        tool_call = next(
            (x for x in response.output if x.type == "function_call"),
            None
        )

        if tool_call is None:

            return jsonify({
                "message": response.output_text,
                "response_id": response.id,
                "listings": []
            })

        tool_args = json.loads(tool_call.arguments)

        if tool_call.name == "search_listings":
            ui, gpt = search_listings(tool_args)
        elif tool_call.name == "score_lead_listings":
            ui, gpt = score_lead_listings(tool_args)
        else:
            raise ValueError(f"Unsupported tool: {tool_call.name}")

        final = client.responses.create(
            model="gpt-5-mini",
            previous_response_id=response.id,
            input=[{
                "type": "function_call_output",
                "call_id": tool_call.call_id,
                "output": json.dumps(gpt)
            }]
        )

        return jsonify({
            "message": final.output_text,
            "response_id": final.id,
            "listings": ui
        })

    except Exception as e:

        return jsonify({"error": str(e)}), 500


########################################################################
# STREAMING TEST ENDPOINT
########################################################################

@app.route("/chat_stream", methods=["POST"])
def chat_stream():

    try:

        data = request.get_json(silent=True) or {}

        previous = data.get("previous_response_id")

        if previous in ("", "null"):
            previous = None

        @stream_with_context
        def generate():

            with client.responses.stream(
                model="gpt-5-mini",
                input=data.get("message", ""),
                previous_response_id=previous,
                instructions=(
                    "You are Rentee AI, a Kuala Lumpur property assistant. "
                    "Always remember the previous conversation."
                )
            ) as stream:

                for event in stream:

                    if event.type == "response.output_text.delta":

                        yield (
                            f"data: "
                            f"{json.dumps({'delta': event.delta})}\n\n"
                        )

                final = stream.get_final_response()

                yield (
                    f"data: "
                    f"{json.dumps({'done': True, 'response_id': final.id})}\n\n"
                )

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
