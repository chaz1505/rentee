from flask import Flask, request, jsonify
from openai import OpenAI
import os
import requests
import json

app = Flask(__name__)

client = OpenAI(
    api_key=os.environ["OPENAI_API_KEY"]
)

SEARCH_URL = "https://www.rentee.asia/version-test/api/1.1/wf/search_listings"

@app.route("/")
def home():
    return jsonify({
        "status": "running"
    })

@app.route("/chat", methods=["POST"])
def chat():

    data = request.get_json() or {}

    user_message = data.get(
        "message",
        "Show me 3 bed condos in One Menerung for rent less than 10k a month"
    )

    previous_response_id = data.get("previous_response_id")

    response = client.responses.create(
        model="gpt-5-mini",
        input=user_message,
        previous_response_id=previous_response_id if previous_response_id else None,
        instructions="""
        You are Rentee AI, a Kuala Lumpur property assistant.

        Always remember the conversation context.

        Never expose internal listing IDs.

        When the user says things like:
        - yes
        - no
        - tell me more
        - which is cheapest
        - expand the search

        interpret them in the context of the previous conversation.
        """,
        tool_choice="auto",
        tools=[
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
            }
        ]
    )

    tool_call = next(
        (item for item in response.output if item.type == "function_call"),
        None
    )

    # No tool call required
    if tool_call is None:

        return jsonify({
            "message": response.output_text,
            "response_id": response.id,
            "listings": []
        })

    args = json.loads(tool_call.arguments)

    min_beds = args.get("min_beds", 0)

    r = requests.get(
        f"{SEARCH_URL}?min_beds={min_beds}"
    )

    search_data = r.json()

    listings = search_data["response"]["listing"]

    condo_cache = {}

    ui_results = []
    gpt_results = []

    for listing in listings:

        condo_id = listing["condo"]

        if condo_id not in condo_cache:

            condo_response = requests.get(
                f"https://www.rentee.asia/version-test/api/1.1/obj/condo/{condo_id}"
            )

            condo_data = condo_response.json()

            condo_name = condo_data["response"]["name"]

            condo_cache[condo_id] = condo_name

        condo_name = condo_cache[condo_id]

        ui_results.append({
            "listing_id": listing["_id"],
            "condo": condo_name,
            "beds": listing.get("beds"),
            "baths": listing.get("baths"),
            "price_rent": listing.get("priceRent"),
            "price_sale": listing.get("priceSale"),
            "transactionType": listing.get("transactionType")
        })

        gpt_results.append({
            "condo": condo_name,
            "beds": listing.get("beds"),
            "baths": listing.get("baths"),
            "price_rent": listing.get("priceRent"),
            "price_sale": listing.get("priceSale")
        })

    response2 = client.responses.create(
        model="gpt-5-mini",
        previous_response_id=response.id,
        input=[
            {
                "type": "function_call_output",
                "call_id": tool_call.call_id,
                "output": json.dumps(gpt_results)
            }
        ]
    )

    return jsonify({
        "message": response2.output_text,
        "response_id": response2.id,
        "listings": ui_results
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)