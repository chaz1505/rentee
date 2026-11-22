import json
import hmac
import os
from pathlib import Path

from flask import Flask, request, jsonify

app = Flask(__name__)

LISTINGS_FILE = Path(__file__).parent / "one_menerung.json"


def load_listings():
    from dataclasses import asdict
    from propertyguru_scraper import (
        IPropertyClient,
        load_condo_configs,
        iproperty_project_search_url,
    )

    configs = load_condo_configs()
    condo = next(
        c for c in configs
        if c.requested_name.casefold() == "one menerung"
    )

    url = iproperty_project_search_url(
        condo.iproperty_search_name,
        condo.iproperty_project_id,
    )

    with IPropertyClient() as client:
        listings = client.discover_listings(
            search_url=url,
            max_pages=1,
            config=condo,
            limit=None,
        )

    return [asdict(listing) for listing in listings]


API_KEY_FILE = Path(__file__).parent / ".scraper_api_key"


@app.before_request
def authenticate():
    expected = API_KEY_FILE.read_text().strip()
    supplied = request.headers.get("X-API-Key", "")

    if not expected or not hmac.compare_digest(supplied, expected):
        return jsonify({"error": "Unauthorized"}), 401


@app.post("/api/external-matches")
def external_matches():
    data = request.get_json(silent=True) or {}

    if data.get("development", "").strip().casefold() != "one menerung":
        return jsonify({"error": "Development not supported yet"}), 400

    try:
        bedrooms = int(data.get("bedrooms_min", 0))
        budget = float(data["budget_rent"])
    except (ValueError, TypeError, KeyError):
        return jsonify({"error": "Invalid bedrooms or rental budget"}), 400

    if bedrooms < 0 or budget <= 0:
        return jsonify({"error": "Invalid requirements"}), 400

    matches = []
    for listing in load_listings():
        price = listing.get("price_rent")
        beds = listing.get("beds")

        if not isinstance(price, (int, float)) or not isinstance(beds, (int, float)):
            continue
        if beds < bedrooms:
            continue
        if not budget * 0.8 <= price <= budget * 1.2:
            continue

        matches.append({
            "source_listing_id": listing["source_listing_id"],
            "source_url": listing["source_url"],
            "agent": listing.get("source_agent_name"),
            "price_rent": price,
            "beds": beds,
            "sq_ft": listing.get("sq_ft"),
            "description": listing.get("description"),
        })

    matches.sort(key=lambda x: (abs(x["price_rent"] - budget), x["source_listing_id"]))

    return jsonify({
        "development": "One Menerung",
        "source": "iproperty",
        "total_matches": len(matches),
        "matches": matches[:3],
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)
