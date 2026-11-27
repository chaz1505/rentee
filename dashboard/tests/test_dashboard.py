import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bubble_api import BubbleAPI, BubbleAPIError, TTLCache
from dashboard_app import create_app


class DashboardTestCase(unittest.TestCase):
    def setUp(self):
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DASHBOARD_PASSWORD": "correct horse",
            "BUBBLE_API_BASE_URL": "https://bubble.test/api/1.1",
            "BUBBLE_API_TOKEN": "token",
            "SESSION_COOKIE_SECURE": False,
        })
        self.client = self.app.test_client()

    def csrf(self):
        self.client.get("/login")
        with self.client.session_transaction() as session:
            return session["csrf_token"]

    def login(self):
        return self.client.post("/login", data={
            "password": "correct horse", "csrf_token": self.csrf(),
        })

    def test_authentication_and_logout_csrf(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        self.assertEqual(self.client.post("/login", data={"password": "correct horse"}).status_code, 400)
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_unauthorized_apis(self):
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
        self.assertEqual(self.client.get("/api/alerts").status_code, 401)

    def test_invalid_date_filter(self):
        self.login()
        self.assertEqual(self.client.get("/api/stats?range=year").status_code, 400)
        self.assertEqual(self.client.get("/api/alerts?range=year").status_code, 400)

    @patch("dashboard_app.parallel_counts")
    @patch("dashboard_app.BubbleAPI")
    def test_stats_counts_and_date_filters(self, client_class, counts):
        counts.return_value = {"leads": 2, "listings": 3, "matches": 4,
                               "alerts_sent": 1, "total_users": 8,
                               "users_alerts_enabled": 5}
        self.login()
        response = self.client.get("/api/stats?range=7d")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["leads"], 2)
        queries = counts.call_args.args[1]
        self.assertEqual(queries["leads"][1][0]["key"], "Created Date")
        self.assertEqual(queries["alerts_sent"][1][0]["key"], "alert_sent")
        self.assertEqual(queries["alerts_sent"][1][1]["key"], "alert_sent_at")
        self.assertIn("+08:00", queries["leads"][1][0]["value"])

    @patch("dashboard_app.parallel_counts", side_effect=BubbleAPIError("unavailable"))
    def test_api_error_is_not_zero(self, _counts):
        self.login()
        response = self.client.get("/api/stats")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("leads", response.get_json())

    @patch("dashboard_app.BubbleAPI")
    def test_recent_alerts_batch_fetch_and_missing_data(self, client_class):
        client = client_class.return_value
        client.records.return_value = [{
            "_id": "m1", "lead": "l1", "listing": "x1",
            "lead_owner": "u1", "listing_owner": "u2",
            "alert_recipient": "u1", "alert_sent_at": "2026-11-27T01:00:00Z",
        }]
        client.records_by_ids.side_effect = [
            [{"_id": "u1", "name": "Gwen"}],
            [{"_id": "l1"}], [{"_id": "x1"}], [],
        ]
        self.login()
        payload = self.client.get("/api/alerts?range=all").get_json()["alerts"][0]
        self.assertEqual(payload["recipient"], "Gwen")
        self.assertEqual(payload["other_agent"], "Unknown agent")
        self.assertEqual(payload["area"], "Not specified")
        self.assertEqual(payload["status"], "Sent")
        self.assertEqual(payload["lead_url"], "https://www.rentee.asia/lead/l1")
        self.assertEqual(client.records_by_ids.call_count, 4)

    def test_missing_credentials_fail_closed_and_health_stays_lightweight(self):
        app = create_app({"TESTING": True, "SECRET_KEY": None,
                          "DASHBOARD_PASSWORD": None,
                          "BUBBLE_API_TOKEN": None,
                          "BUBBLE_API_BASE_URL": None})
        client = app.test_client()
        self.assertEqual(client.get("/login").status_code, 503)
        self.assertEqual(client.get("/health").get_json(), {"status": "ok"})


class BubbleClientTests(unittest.TestCase):
    @patch("bubble_api.requests.Session.get")
    def test_count_uses_metadata_without_full_download(self, get):
        response = Mock()
        response.json.return_value = {"response": {"results": [{"_id": "1"}], "remaining": 41}}
        get.return_value = response
        self.assertEqual(BubbleAPI("https://bubble.test", "token").count("lead"), 42)
        self.assertEqual(get.call_args.kwargs["params"]["limit"], 1)

    @patch("bubble_api.requests.Session.get")
    def test_pagination(self, get):
        first, second = Mock(), Mock()
        first.json.return_value = {"response": {"results": [{"_id": "1"}], "remaining": 1}}
        second.json.return_value = {"response": {"results": [{"_id": "2"}], "remaining": 0}}
        get.side_effect = [first, second]
        rows = BubbleAPI("https://bubble.test", "token").records("match")
        self.assertEqual([row["_id"] for row in rows], ["1", "2"])
        self.assertEqual(get.call_args_list[1].kwargs["params"]["cursor"], 1)

    @patch("bubble_api.requests.Session.get")
    def test_http_errors_are_sanitized(self, get):
        get.side_effect = __import__("requests").Timeout("secret details")
        with self.assertRaisesRegex(BubbleAPIError, "temporarily unavailable"):
            BubbleAPI("https://bubble.test", "token").count("lead")

    def test_cache_coalesces_concurrent_refresh_and_expires(self):
        cache = TTLCache(.03)
        calls = []
        def factory():
            calls.append(1); time.sleep(.02); return 7
        values = []
        threads = [threading.Thread(target=lambda: values.append(cache.get_or_create("x", factory))) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual(values, [7] * 4)
        self.assertEqual(len(calls), 1)
        time.sleep(.04)
        self.assertEqual(cache.get_or_create("x", factory), 7)
        self.assertEqual(len(calls), 2)

    def test_client_exposes_no_mutation_methods(self):
        client = BubbleAPI("https://bubble.test", "token")
        for name in ("post", "put", "patch", "delete"):
            self.assertFalse(hasattr(client, name))


class RepositoryIsolationTests(unittest.TestCase):
    def test_dashboard_files_are_self_contained(self):
        dashboard = Path(__file__).resolve().parents[1]
        app_source = (dashboard / "dashboard_app.py").read_text()
        self.assertNotIn("import app", app_source)
        required = {
            "dashboard_app.py", "bubble_api.py", "requirements.txt", "README.md",
            "templates/dashboard.html", "templates/login.html",
            "static/dashboard.css", "static/dashboard.js", "tests/test_dashboard.py",
        }
        actual = {str(path.relative_to(dashboard)) for path in dashboard.rglob("*") if path.is_file()}
        self.assertTrue(required.issubset(actual))


if __name__ == "__main__":
    unittest.main()
