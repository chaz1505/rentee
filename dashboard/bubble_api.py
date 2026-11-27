"""Small, read-only Bubble Data API client for the Rentee dashboard."""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Iterable

import requests

logger = logging.getLogger(__name__)


class BubbleAPIError(RuntimeError):
    """Raised when Bubble cannot provide trustworthy dashboard data."""


class BubbleAPI:
    def __init__(self, base_url: str, token: str, timeout=(3.05, 12)):
        if not base_url or not token:
            raise ValueError("Bubble API credentials are required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({"Authorization": f"Bearer {token}"})

    def _get(
        self, path: str, params: dict | None = None, object_type: str = "unknown"
    ) -> dict:
        try:
            response = self._session.get(
                f"{self.base_url}/{path.lstrip('/')}", params=params,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json().get("response")
            if not isinstance(payload, dict):
                raise BubbleAPIError("Bubble returned an invalid response")
            return payload
        except requests.Timeout as exc:
            logger.warning(
                "Bubble API request failed: object_type=%s status=%s reason=%s timeout=%s",
                object_type, "none", "timeout", self.timeout,
            )
            raise BubbleAPIError("Bubble data is temporarily unavailable") from exc
        except (requests.RequestException, ValueError, TypeError) as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None)
            reason = getattr(response, "reason", None) or type(exc).__name__
            logger.warning(
                "Bubble API request failed: object_type=%s status=%s reason=%s",
                object_type, status if status is not None else "none", reason,
            )
            raise BubbleAPIError("Bubble data is temporarily unavailable") from exc

    @staticmethod
    def _params(constraints=None, **extra):
        params = {key: value for key, value in extra.items() if value is not None}
        if constraints:
            params["constraints"] = json.dumps(constraints, separators=(",", ":"))
        return params

    def page(self, object_type: str, constraints=None, **params) -> dict:
        return self._get(
            f"obj/{object_type}", self._params(constraints, **params), object_type
        )

    def bounded_page(self, object_type: str, constraints=None, cursor=0, limit=30,
                     **params) -> tuple[list[dict], bool]:
        """Return one validated Bubble page; never follow `remaining` implicitly."""
        if not 1 <= limit <= 100 or cursor < 0:
            raise ValueError("Invalid Bubble page bounds")
        payload = self.page(
            object_type, constraints, cursor=cursor, limit=limit, **params
        )
        results = payload.get("results") or []
        remaining = payload.get("remaining", 0)
        if not isinstance(results, list) or not isinstance(remaining, int):
            raise BubbleAPIError("Bubble returned invalid pagination metadata")
        return results, remaining > 0

    def count(self, object_type: str, constraints=None) -> int:
        # Bubble's `remaining` is the number after this page. Requesting one row
        # keeps counts cheap while remaining + returned rows gives the exact total.
        page = self.page(object_type, constraints, cursor=0, limit=1)
        results = page.get("results") or []
        remaining = page.get("remaining", 0)
        if not isinstance(results, list) or not isinstance(remaining, int):
            raise BubbleAPIError("Bubble returned invalid count metadata")
        return len(results) + remaining

    def records(self, object_type: str, constraints=None, limit=None, **params):
        cursor = 0
        output = []
        while limit is None or len(output) < limit:
            page_limit = min(100, limit - len(output)) if limit else 100
            page = self.page(
                object_type, constraints, cursor=cursor, limit=page_limit, **params
            )
            results = page.get("results") or []
            if not isinstance(results, list):
                raise BubbleAPIError("Bubble returned invalid records")
            output.extend(results)
            if not results or not page.get("remaining"):
                break
            cursor += len(results)
        return output[:limit] if limit else output

    def records_by_ids(self, object_type: str, ids: Iterable[str]) -> list[dict]:
        unique = list(dict.fromkeys(str(item) for item in ids if item))
        if not unique:
            return []
        output = []
        for offset in range(0, len(unique), 50):
            group = unique[offset:offset + 50]
            output.extend(self.records(object_type, [{
                "key": "_id",
                "constraint_type": "equals" if len(group) == 1 else "in",
                "value": group[0] if len(group) == 1 else group,
            }]))
        return output


class TTLCache:
    """Thread-safe TTL cache with per-key refresh coalescing."""

    def __init__(self, ttl=60):
        self.ttl = ttl
        self._values: dict[str, tuple[float, Any]] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def get_or_create(self, key: str, factory):
        now = time.monotonic()
        with self._guard:
            current = self._values.get(key)
            if current and now - current[0] < self.ttl:
                return current[1]
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            now = time.monotonic()
            with self._guard:
                current = self._values.get(key)
                if current and now - current[0] < self.ttl:
                    return current[1]
            value = factory()
            with self._guard:
                self._values[key] = (time.monotonic(), value)
            return value

    def clear(self):
        with self._guard:
            self._values.clear()


def parallel_counts(client: BubbleAPI, queries: dict[str, tuple[str, list]]) -> dict:
    """Run a small bounded group of independent count queries."""
    with ThreadPoolExecutor(max_workers=min(4, len(queries))) as executor:
        futures = {
            name: executor.submit(client.count, object_type, constraints)
            for name, (object_type, constraints) in queries.items()
        }
        return {name: future.result() for name, future in futures.items()}
