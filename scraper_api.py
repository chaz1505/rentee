"""Authenticated DigitalOcean API for on-demand iProperty rental matches."""

from __future__ import annotations

import hmac
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from propertyguru_scraper import (
    AccessBlockedError,
    AmbiguousProjectError,
    CondoConfig,
    IPropertyClient,
    ProjectNotFoundError,
    ScraperError,
    iproperty_project_search_url,
    iproperty_search_url,
    load_condo_configs,
    normalize_project_name,
)


app = Flask(__name__)
API_KEY_FILE = Path(__file__).parent / ".scraper_api_key"
MAX_DEVELOPMENT_LENGTH = 160
MAX_BEDROOMS = 50
MAX_BUDGET_RENT = 100_000_000


def error_response(code: str, message: str, status: int, **details: Any):
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if details:
        payload["error"]["details"] = details
    return jsonify(payload), status


def configured_development(development: str) -> CondoConfig:
    """Use an explicit config when available, otherwise create a safe dynamic one."""
    target = normalize_project_name(development)
    matches = []
    for config in load_condo_configs():
        configured_names = (
            config.requested_name,
            config.bubble_name,
            config.iproperty_search_name,
            config.iproperty_project_name or "",
        )
        if target in {normalize_project_name(name) for name in configured_names if name}:
            matches.append(config)

    # Duplicate aliases are a configuration ambiguity; never pick by file order.
    unique = {config.requested_name: config for config in matches}
    if len(unique) > 1:
        raise AmbiguousProjectError(
            f'Multiple configured developments match "{development}".'
        )
    if unique:
        config = next(iter(unique.values()))
        if not config.enabled:
            raise ProjectNotFoundError(
                f'Development "{development}" is configured but disabled.'
            )
        return config

    return CondoConfig(
        requested_name=development,
        bubble_name=development,
        iproperty_search_name=development,
        iproperty_project_name=development,
        iproperty_project_id=None,
        group=None,
        enabled=True,
    )


def load_listings(
    development: str, bedrooms_min: int, budget_rent: float
) -> tuple[str, list[dict[str, Any]]]:
    config = configured_development(development)
    if config.iproperty_project_id:
        search_url = iproperty_project_search_url(
            config.iproperty_search_name, config.iproperty_project_id,
            bedrooms_min, budget_rent, MAX_BEDROOMS,
        )
    else:
        search_url = iproperty_search_url(
            config.iproperty_search_name, bedrooms_min, budget_rent, MAX_BEDROOMS
        )

    app.logger.info("Searching iProperty development: %s", config.bubble_name)
    app.logger.info("iProperty search URL: %s", search_url)

    with IPropertyClient() as client:
        listings = client.discover_listings(
            search_url=search_url,
            max_pages=1,
            config=config,
            limit=None,
        )
    return config.bubble_name.strip(), [asdict(listing) for listing in listings]


def parse_requirements(data: Any) -> tuple[str, int, float]:
    if not isinstance(data, dict):
        raise ValueError("Request body must be a JSON object.")

    development_value = data.get("development")
    if not isinstance(development_value, str):
        raise ValueError("development must be a string.")
    development = " ".join(development_value.split())
    if not development:
        raise ValueError("development must not be empty.")
    if len(development) > MAX_DEVELOPMENT_LENGTH:
        raise ValueError(
            f"development must be at most {MAX_DEVELOPMENT_LENGTH} characters."
        )
    if not normalize_project_name(development):
        raise ValueError("development must contain letters or numbers.")

    bedrooms_value = data.get("bedrooms_min")
    if isinstance(bedrooms_value, bool):
        raise ValueError("bedrooms_min must be a whole number.")
    try:
        bedrooms_number = float(bedrooms_value)
    except (TypeError, ValueError):
        raise ValueError("bedrooms_min must be a whole number.") from None
    if not math.isfinite(bedrooms_number) or not bedrooms_number.is_integer():
        raise ValueError("bedrooms_min must be a whole number.")
    bedrooms = int(bedrooms_number)
    if not 0 <= bedrooms <= MAX_BEDROOMS:
        raise ValueError(f"bedrooms_min must be between 0 and {MAX_BEDROOMS}.")

    budget_value = data.get("budget_rent")
    if isinstance(budget_value, bool):
        raise ValueError("budget_rent must be a positive number.")
    try:
        budget = float(budget_value)
    except (TypeError, ValueError):
        raise ValueError("budget_rent must be a positive number.") from None
    if not math.isfinite(budget) or not 0 < budget <= MAX_BUDGET_RENT:
        raise ValueError(
            f"budget_rent must be greater than 0 and at most {MAX_BUDGET_RENT}."
        )
    return development, bedrooms, budget


@app.before_request
def authenticate():
    try:
        expected = API_KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return error_response(
            "authentication_unavailable", "API authentication is unavailable.", 503
        )
    supplied = request.headers.get("X-API-Key", "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return error_response("unauthorized", "Unauthorized.", 401)
    return None


@app.post("/api/external-matches")
def external_matches():
    try:
        development, bedrooms, budget = parse_requirements(request.get_json(silent=True))
    except ValueError as error:
        return error_response("invalid_request", str(error), 400)

    try:
        resolved_development, listings = load_listings(development, bedrooms, budget)
    except AmbiguousProjectError as error:
        return error_response("ambiguous_development", str(error), 409)
    except ProjectNotFoundError as error:
        return error_response("development_not_found", str(error), 404)
    except AccessBlockedError:
        app.logger.exception("iProperty blocked the scraper request")
        return error_response(
            "scraper_blocked", "iProperty temporarily blocked the scraper request.", 502
        )
    except ScraperError:
        app.logger.exception("iProperty scraper failed")
        return error_response("scraper_failure", "iProperty scraping failed.", 502)
    except Exception:
        app.logger.exception("Unexpected scraper API failure")
        return error_response("scraper_failure", "iProperty scraping failed.", 502)

    matches = []
    for listing in listings:
        price = listing.get("price_rent")
        beds = listing.get("beds")
        if (
            isinstance(price, bool)
            or isinstance(beds, bool)
            or not isinstance(price, (int, float))
            or not isinstance(beds, (int, float))
        ):
            continue
        if beds < bedrooms or not budget * 0.8 <= price <= budget * 1.2:
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

    matches.sort(key=lambda item: (
        abs(item["price_rent"] - budget), item["source_listing_id"]
    ))
    app.logger.info(
        "iProperty listings passing budget and bedroom filters: %d", len(matches)
    )
    return jsonify({
        "development": resolved_development,
        "source": "iproperty",
        "total_matches": len(matches),
        "matches": matches[:3],
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5050)
