"""Small Bubble client with constrained queries and redacted failures."""

import json
import os
import requests


class BubbleUnavailable(RuntimeError):
    pass


class BubbleClient:
    def __init__(self):
        base = os.getenv("BUBBLE_API_BASE_URL") or "https://rentee.bubbleapps.io/api/1.1"
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {os.environ['BUBBLE_API_TOKEN']}", "Content-Type": "application/json"}
        self.timeout = (3.05, float(os.getenv("AGENT_BUBBLE_TIMEOUT_SECONDS", "12")))

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(method, f"{self.base}/{path.lstrip('/')}", headers=self.headers, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except (requests.RequestException, ValueError) as exc:
            raise BubbleUnavailable("Bubble data is temporarily unavailable") from exc

    def search(self, object_type, constraints, limit=100, cursor=0, sort_field=None, descending=False):
        params = {"constraints": json.dumps(constraints, separators=(",", ":")), "limit": min(max(int(limit), 1), 100), "cursor": max(int(cursor), 0)}
        if sort_field:
            params.update({"sort_field": sort_field, "descending": str(bool(descending)).lower()})
        payload = self._request("GET", f"obj/{object_type}", params=params).json().get("response", {})
        rows = payload.get("results")
        if not isinstance(rows, list):
            raise BubbleUnavailable("Bubble returned invalid data")
        return rows, int(payload.get("remaining") or 0)

    def one(self, object_type, record_id):
        try:
            return self._request("GET", f"obj/{object_type}/{record_id}").json().get("response") or None
        except BubbleUnavailable as exc:
            cause = exc.__cause__
            if getattr(getattr(cause, "response", None), "status_code", None) == 404:
                return None
            raise

    def create(self, object_type, payload):
        data = self._request("POST", f"obj/{object_type}", json=payload).json()
        record_id = data.get("id")
        if not record_id:
            raise BubbleUnavailable("Bubble did not confirm creation")
        return record_id

    def patch(self, object_type, record_id, payload):
        self._request("PATCH", f"obj/{object_type}/{record_id}", json=payload)

    def all(self, object_type, constraints, maximum=1000):
        output, cursor = [], 0
        while len(output) < maximum:
            rows, remaining = self.search(object_type, constraints, min(100, maximum-len(output)), cursor)
            output.extend(rows)
            if not rows or remaining <= 0:
                break
            cursor += len(rows)
        return output
