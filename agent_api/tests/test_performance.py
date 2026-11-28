import unittest

from flask import Flask
from unittest.mock import patch

from agent_api.cache import TTLCache, dashboard_cache, network_stats_cache
from agent_api.routes import blueprint
from agent_api.tests.test_routes import FakeBubble


class PerformanceTests(unittest.TestCase):
    def setUp(self):
        dashboard_cache.clear(); network_stats_cache.clear()

    def test_ttl_cache_expires_and_keeps_keys_isolated(self):
        now = [10.0]
        cache = TTLCache(maxsize=2, timer=lambda: now[0])
        cache.set(("agent-a", "home"), {"count": 1}, 5)
        cache.set(("agent-b", "home"), {"count": 2}, 5)
        self.assertEqual(cache.get(("agent-a", "home")), ({"count": 1}, True))
        self.assertEqual(cache.get(("agent-b", "home")), ({"count": 2}, True))
        now[0] = 15.0
        self.assertEqual(cache.get(("agent-a", "home")), (None, False))

    def test_inventory_default_pagination_is_applied_by_bubble_search(self):
        app = Flask(__name__); app.register_blueprint(blueprint)
        bubble = FakeBubble()
        bubble.rows["listing"] = {
            f"l{i}": {"_id": f"l{i}", "owner": "u1", "Created Date": f"2026-01-{i + 1:02d}T00:00:00Z"}
            for i in range(25)
        }
        calls = []
        original = bubble.search
        def search(*args, **kwargs):
            calls.append((args, kwargs)); return original(*args, **kwargs)
        bubble.search = search
        with patch("agent_api.routes.authenticate", return_value=(bubble.rows["user"]["u1"], {}, bubble)):
            response = app.test_client().get("/api/agent/listings")
        body = response.get_json()
        self.assertEqual(len(body["items"]), 20)
        self.assertTrue(body["has_more"])
        self.assertEqual(body["next_page"], 2)
        self.assertEqual(calls[0][0][2:4], (20, 0))
        self.assertEqual(calls[0][0][1][0]["value"], "u1")

    def test_home_has_no_unconstrained_network_scan(self):
        app = Flask(__name__); app.register_blueprint(blueprint)
        bubble = FakeBubble(); calls=[]; original=bubble.all
        def all_rows(kind,constraints,maximum=1000):
            calls.append((kind,constraints,maximum)); return original(kind,constraints,maximum)
        bubble.all=all_rows
        with patch("agent_api.routes.authenticate", return_value=(bubble.rows["user"]["u1"], {}, bubble)):
            response=app.test_client().get("/api/agent/home")
        self.assertEqual(response.status_code,200)
        self.assertNotIn("network",response.get_json())
        self.assertFalse(any(kind in {"listing","lead","match"} and constraints==[] for kind,constraints,_maximum in calls))


if __name__ == "__main__":
    unittest.main()
