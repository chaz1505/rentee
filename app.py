from flask import Flask, request, jsonify
from openai import OpenAI
import requests
import json
import os

app = Flask(__name__)

client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

LEAD_URL = "https://www.rentee.asia/version-test/api/1.1/obj/lead"
LISTING_URL = "https://www.rentee.asia/version-test/api/1.1/obj/listing"


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


def match_lead(lead_id):

    lead = bubble(f"{LEAD_URL}/{lead_id}")

    listings = get_all_listings()

    property_text = ""

    for listing in listings:

        property_text += f"""

Listing ID: {listing["_id"]}

{listing["AIsearchtext"]}

------------------------------------------
"""

    prompt = f"""
You are an experienced Kuala Lumpur real estate agent.

Below is a buyer lead.

========================
BUYER
========================

{lead["AIsearchtext"]}

========================
AVAILABLE PROPERTIES
========================

{property_text}

========================

Your task:

Read EVERY listing.

Choose the TEN BEST properties.

Rank them from best to worst.

For each property explain:

• Why it matches

• Any compromises

At the end provide your overall recommendation and which property you would show first.

Write naturally like an experienced estate agent.

Do not mention properties outside the supplied list.
"""

    response = client.responses.create(
        model="gpt-5-mini",
        input=prompt
    )

    return response.output_text


@app.route("/chat", methods=["POST"])
def chat():

    user_message = request.json["message"]

    response = client.responses.create(

        model="gpt-5-mini",

        input=user_message,

        tools=[

            {
                "type": "function",

                "name": "match_lead",

                "description": """
Use this whenever the user wants recommendations,
matching properties to a buyer,
finding suitable listings,
ranking listings,
or asks for the best properties for a Lead.

The Lead ID must be supplied.
""",

                "parameters": {
                    "type": "object",
                    "properties": {
                        "lead_id": {
                            "type": "string"
                        }
                    },
                    "required": ["lead_id"]
                }
            }

        ]
    )

    tool_call = next(
        (x for x in response.output if x.type == "function_call"),
        None
    )

    if tool_call:

        args = json.loads(tool_call.arguments)

        tool_result = match_lead(args["lead_id"])

        final = client.responses.create(

            model="gpt-5-mini",

            previous_response_id=response.id,

            input=[
                {
                    "type": "function_call_output",
                    "call_id": tool_call.call_id,
                    "output": tool_result
                }
            ]
        )

        return jsonify({
            "message": final.output_text
        })

    return jsonify({
        "message": response.output_text
    })


if __name__ == "__main__":
    app.run(port=10000)