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

LEAD_URL = "https://www.rentee.asia/version-test/api/1.1/obj/lead"
LISTING_URL = "https://www.rentee.asia/version-test/api/1.1/obj/listing"


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
        "name": "match_lead",
        "description": (
            "Use this tool whenever the user asks for suitable properties, "
            "property recommendations, matching listings, ranking listings, "
            "or identifying the best properties for a buyer lead. "
            "Always use this tool when the user provides a Lead ID and wants "
            "property recommendations."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lead_id": {
                    "type": "string",
                    "description": (
                        "The Bubble unique ID of the Lead. "
                        "Example: 1775642052446x819076856508842000"
                    )
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


def bubble(url, **kwargs):

    r = requests.get(url, timeout=30, **kwargs)

    r.raise_for_status()

    return r.json()["response"]


def get_all_listings():

    listings = []

    cursor = None

    while True:

        params = {}

        if cursor:
            params["cursor"] = cursor

        page = bubble(LISTING_URL, params=params)

        listings.extend(page.get("results", []))

        if not page.get("remaining"):
            break

        cursor = page.get("cursor")

    return listings


def match_lead(tool_args):

    lead = bubble(f"{LEAD_URL}/{tool_args['lead_id']}")

    listings = get_all_listings()

    prompt = f"""

You are one of Kuala Lumpur's best real estate agents.

Your job is to recommend the most suitable properties for the buyer.

=========================

BUYER

=========================

{lead["AIsearchtext"]}

=========================

AVAILABLE PROPERTIES

=========================

"""

    for listing in listings:

        prompt += f"""

Listing ID: {listing["_id"]}

Bedrooms: {listing.get("beds")}

Bathrooms: {listing.get("baths")}

Rent: {listing.get("priceRent")}

Sale: {listing.get("priceSale")}

{listing.get("AIsearchtext","")}

----------------------------------------

"""

    prompt += """

Read EVERY listing.

Choose the TEN best properties.

Rank them from best to worst.

For each property explain

- Why it suits the buyer

- Any compromises

- Why you ranked it there

Finally tell me which property you would show first.

Write naturally as if speaking to another estate agent.

Do not invent facts.

Only recommend supplied properties.

"""

    response = client.responses.create(

        model="gpt-5-mini",

        input=prompt

    )

    return response.output_text

@app.route("/chat", methods=["POST"])
def chat():

    try:

        data = request.get_json(silent=True) or {}

        print("=" * 60)
        print("CHAT_STREAM")
        print("Message:", data.get("message"))

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

        match_text = match_lead(tool_args)

        final = client.responses.create(
            model="gpt-5-mini",
            previous_response_id=response.id,
            input=[{
                "type": "function_call_output",
                "call_id": tool_call.call_id,
                "output": match_text
            }]
        )

        return jsonify({
            "message": final.output_text,
            "response_id": final.id,
            "listings": []
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

            # First response (may request a tool)
response = client.responses.create(
    **build_response_args(
        data.get("message", ""),
        previous
    )
)

# Did GPT request a tool?
tool_call = next(
    (x for x in response.output if x.type == "function_call"),
    None
)

if tool_call is None:
    final = response
else:
    tool_args = json.loads(tool_call.arguments)

    print("Running match_lead:", tool_args)

    match_text = match_lead(tool_args)

    print("match_lead finished")

    final = client.responses.create(
        model="gpt-5-mini",
        previous_response_id=response.id,
        input=[{
            "type": "function_call_output",
            "call_id": tool_call.call_id,
            "output": match_text
        }]
    )

# Stream the final text back to Bubble
text = final.output_text

for i in range(0, len(text), 25):
    chunk = text[i:i+25]

    yield (
        f"data: "
        f"{json.dumps({'delta': chunk})}\n\n"
    )

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
