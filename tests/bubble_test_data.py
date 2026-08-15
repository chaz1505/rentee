import json
import os

import requests


DEFAULT_BUBBLE_DEV_BASE = "https://www.rentee.asia/version-test/api/1.1"
DEFAULT_BUBBLE_LIVE_BASE = "https://www.rentee.asia/api/1.1"
REQUEST_TIMEOUT = 30


class BubbleTestDataError(RuntimeError):
    pass


def get_bubble_base(environment="development"):
    if environment == "development":
        base_url = os.environ.get("BUBBLE_DEV_BASE", DEFAULT_BUBBLE_DEV_BASE).rstrip("/")
        if "/version-test/" not in f"{base_url}/":
            raise BubbleTestDataError(
                "BUBBLE_DEV_BASE must point to Bubble development and contain /version-test/."
            )
        return base_url
    if environment == "live":
        base_url = os.environ.get("BUBBLE_LIVE_BASE", DEFAULT_BUBBLE_LIVE_BASE).rstrip("/")
        if "/version-test/" in f"{base_url}/" or base_url != DEFAULT_BUBBLE_LIVE_BASE:
            raise BubbleTestDataError(
                "BUBBLE_LIVE_BASE must be the Rentee live Data API URL."
            )
        return base_url
    raise BubbleTestDataError(f"Unsupported benchmark environment: {environment}")


def get_bubble_dev_base():
    return get_bubble_base("development")


def _headers():
    return {
        "Authorization": f"Bearer {os.environ['BUBBLE_API_TOKEN']}",
        "Content-Type": "application/json"
    }


def _response_body(response):
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _request(method, path, payload=None, environment="development"):
    base_url = get_bubble_base(environment)
    url = f"{base_url}/{path.lstrip('/')}"
    response = requests.request(
        method,
        url,
        headers=_headers(),
        json=payload,
        timeout=REQUEST_TIMEOUT
    )
    body = _response_body(response)
    if not response.ok:
        formatted = json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body
        raise BubbleTestDataError(
            f"Bubble development {method} {url} failed: HTTP {response.status_code}\n"
            f"Response: {formatted}"
        )
    return body


def bubble_get(path, environment="development"):
    body = _request("GET", path, environment=environment)
    if isinstance(body, dict) and "response" in body:
        return body["response"]
    return body


def bubble_post(path, payload, environment="development"):
    body = _request("POST", path, payload, environment=environment)
    created_id = body.get("id") if isinstance(body, dict) else None
    if not created_id and isinstance(body, dict) and isinstance(body.get("response"), dict):
        created_id = body["response"].get("id")
    if not created_id:
        raise BubbleTestDataError(
            "Bubble development create succeeded but returned no object ID. "
            f"Response: {json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body}"
        )
    return created_id


def bubble_patch(path, payload, environment="development"):
    return _request("PATCH", path, payload, environment=environment)


def bubble_delete(path, environment="development"):
    return _request("DELETE", path, environment=environment)
