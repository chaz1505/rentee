"""Small Bubble client with constrained queries and redacted failures."""

import json
import logging
import os
import requests
import time

logger = logging.getLogger(__name__)


class BubbleUnavailable(RuntimeError):
    pass


class BubbleClient:
    def __init__(self):
        # Keep the fallback aligned with the existing live Rentee integrations.
        base = os.getenv("BUBBLE_API_BASE_URL") or "https://www.rentee.asia/api/1.1"
        self.base = base.rstrip("/")
        self.headers = {"Authorization": f"Bearer {os.environ['BUBBLE_API_TOKEN']}", "Content-Type": "application/json"}
        self.timeout = (3.05, float(os.getenv("AGENT_BUBBLE_TIMEOUT_SECONDS", "12")))
        self.request_count = 0
        self.request_seconds = 0.0

    @staticmethod
    def _log_failure(operation, object_type, status, reason):
        # reason is deliberately a fixed classification, never an exception
        # message or response body, since either may contain sensitive data.
        logger.warning(
            "Agent Bubble request failed: operation=%s object_type=%s status=%s reason=%s",
            operation, object_type, status if status is not None else "none", reason,
        )

    def _request(self, method, path, object_type, operation, **kwargs):
        started = time.perf_counter()
        self.request_count += 1
        try:
            response = requests.request(method, f"{self.base}/{path.lstrip('/')}", headers=self.headers, timeout=self.timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.Timeout as exc:
            self._log_failure(operation, object_type, None, "timeout")
            raise BubbleUnavailable("Bubble data is temporarily unavailable") from exc
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            reason = "http_error" if status is not None else "connection_error"
            self._log_failure(operation, object_type, status, reason)
            raise BubbleUnavailable("Bubble data is temporarily unavailable") from exc
        finally:
            self.request_seconds += time.perf_counter() - started

    def _json(self, response, operation, object_type):
        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            self._log_failure(operation, object_type, response.status_code, "invalid_json")
            raise BubbleUnavailable("Bubble returned invalid data") from exc
        if not isinstance(data, dict):
            self._log_failure(operation, object_type, response.status_code, "invalid_response")
            raise BubbleUnavailable("Bubble returned invalid data")
        return data

    def search(self, object_type, constraints, limit=100, cursor=0, sort_field=None, descending=False):
        params = {"constraints": json.dumps(constraints, separators=(",", ":")), "limit": min(max(int(limit), 1), 100), "cursor": max(int(cursor), 0)}
        if sort_field:
            params.update({"sort_field": sort_field, "descending": str(bool(descending)).lower()})
        response = self._request("GET", f"obj/{object_type}", object_type, "search", params=params)
        payload = self._json(response, "search", object_type).get("response", {})
        if not isinstance(payload, dict):
            self._log_failure("search", object_type, response.status_code, "invalid_response")
            raise BubbleUnavailable("Bubble returned invalid data")
        rows = payload.get("results")
        if not isinstance(rows, list):
            self._log_failure("search", object_type, response.status_code, "invalid_response")
            raise BubbleUnavailable("Bubble returned invalid data")
        try:
            remaining = int(payload.get("remaining") or 0)
        except (TypeError, ValueError) as exc:
            self._log_failure("search", object_type, response.status_code, "invalid_response")
            raise BubbleUnavailable("Bubble returned invalid data") from exc
        return rows, remaining

    def one(self, object_type, record_id):
        try:
            response = self._request("GET", f"obj/{object_type}/{record_id}", object_type, "get")
            record = self._json(response, "get", object_type).get("response")
            if record is not None and not isinstance(record, dict):
                self._log_failure("get", object_type, response.status_code, "invalid_response")
                raise BubbleUnavailable("Bubble returned invalid data")
            return record or None
        except BubbleUnavailable as exc:
            cause = exc.__cause__
            if getattr(getattr(cause, "response", None), "status_code", None) == 404:
                return None
            raise

    def create(self, object_type, payload):
        response = self._request("POST", f"obj/{object_type}", object_type, "create", json=payload)
        data = self._json(response, "create", object_type)
        # Bubble normally returns {"status":"success","id":"..."}; accept
        # the wrapped form too so a successful write is never misclassified.
        wrapped = data.get("response")
        record_id = data.get("id") or (wrapped.get("id") if isinstance(wrapped, dict) else None)
        if not record_id:
            self._log_failure("create", object_type, response.status_code, "missing_id")
            raise BubbleUnavailable("Bubble did not confirm creation")
        return record_id

    def patch(self, object_type, record_id, payload):
        self._request("PATCH", f"obj/{object_type}/{record_id}", object_type, "patch", json=payload)

    def all(self, object_type, constraints, maximum=1000):
        output, cursor = [], 0
        while len(output) < maximum:
            rows, remaining = self.search(object_type, constraints, min(100, maximum-len(output)), cursor)
            output.extend(rows)
            if not rows or remaining <= 0:
                break
            cursor += len(rows)
        return output

    def many(self, object_type, record_ids, chunk_size=50):
        """Load a deduplicated set of Bubble records in bounded ID batches."""
        ids = list(dict.fromkeys(str(value) for value in record_ids or [] if value))
        output = []
        for offset in range(0, len(ids), chunk_size):
            group = ids[offset:offset + chunk_size]
            constraint = {
                "key": "_id",
                "constraint_type": "equals" if len(group) == 1 else "in",
                "value": group[0] if len(group) == 1 else group,
            }
            output.extend(self.all(object_type, [constraint], maximum=len(group)))
        return output
