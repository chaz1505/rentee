import json
import os

import requests


DEFAULT_BUBBLE_DEV_BASE = "https://www.rentee.asia/version-test/api/1.1"
REQUEST_TIMEOUT = 30


class BubbleTestDataError(RuntimeError):
    pass


def get_bubble_dev_base():
    base_url = os.environ.get("BUBBLE_DEV_BASE", DEFAULT_BUBBLE_DEV_BASE).rstrip("/")
    if "/version-test/" not in f"{base_url}/":
        raise BubbleTestDataError(
            "BUBBLE_DEV_BASE must point to Bubble development and contain /version-test/."
        )
    return base_url


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


def _request(method, path, payload=None):
    base_url = get_bubble_dev_base()
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


def bubble_get(path):
    body = _request("GET", path)
    if isinstance(body, dict) and "response" in body:
        return body["response"]
    return body


def bubble_post(path, payload):
    body = _request("POST", path, payload)
    created_id = body.get("id") if isinstance(body, dict) else None
    if not created_id and isinstance(body, dict) and isinstance(body.get("response"), dict):
        created_id = body["response"].get("id")
    if not created_id:
        raise BubbleTestDataError(
            "Bubble development create succeeded but returned no object ID. "
            f"Response: {json.dumps(body, indent=2) if isinstance(body, (dict, list)) else body}"
        )
    return created_id


def bubble_patch(path, payload):
    return _request("PATCH", path, payload)


def bubble_delete(path):
    return _request("DELETE", path)
