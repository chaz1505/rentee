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
from dashboard_app import _intended_recipient, _unsent_reason, create_app


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
        self.assertEqual(self.client.get("/api/matches?status=failed").status_code, 400)

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
        date_filter = queries["leads"][1][0]
        self.assertEqual(date_filter["constraint_type"], "greater than")
        self.assertRegex(date_filter["value"], r"T23:59:59\.999000\+08:00$")

    @patch("dashboard_app.parallel_counts", side_effect=BubbleAPIError("unavailable"))
    def test_api_error_is_not_zero(self, _counts):
        self.login()
        response = self.client.get("/api/stats")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("leads", response.get_json())

    @patch("dashboard_app.BubbleAPI")
    def test_recent_matches_batch_fetch_and_missing_data(self, client_class):
        client = client_class.return_value
        client.records.return_value = [{
            "_id": "m1", "lead": "l1", "listing": "x1",
            "lead_owner": "u1", "listing_owner": "u2",
            "alert_recipient": "u1", "alert_sent": True,
            "Created Date": "2026-11-27T01:00:00Z",
        }]
        client.records_by_ids.side_effect = [
            [{"_id": "u1", "name": "Gwen"}],
            [{"_id": "l1"}], [{"_id": "x1"}], [],
        ]
        self.login()
        response = self.client.get("/api/matches?range=all")
        payload = response.get_json()["matches"][0]
        self.assertEqual(payload["alert_recipient"], "Gwen")
        self.assertEqual(payload["listing_owner"], "Unknown agent")
        self.assertEqual(payload["area"], "Not specified")
        self.assertEqual(payload["status"], "Sent")
        self.assertEqual(payload["lead_url"], "https://www.rentee.asia/lead/l1")
        self.assertEqual(client.records_by_ids.call_count, 4)
        _, constraints = client.records.call_args.args[:2]
        self.assertEqual(constraints, [])
        self.assertEqual(client.records.call_args.kwargs["sort_field"], "Created Date")

    @patch("dashboard_app.BubbleAPI")
    def test_inbox_uses_effective_latest_timestamp_across_mixed_records(self, client_class):
        client = client_class.return_value
        records = [
            {"_id": "old", "phone": "+60 12-345 6789", "direction": "outbound",
             "messageContent": "Older timestamped message",
             "whatsapp_timestamp": "2026-11-10T23:00:00+08:00",
             "Created Date": "2026-11-10T15:00:00Z"},
            {"_id": "other", "phone": "60111111111", "direction": "inbound",
             "messageContent": "Later in local time, earlier in UTC",
             "whatsapp_timestamp": "2026-11-26T10:00:00+08:00"},
            {"_id": "new", "phone": "60123456789", "direction": "inbound",
             "messageContent": "Newest message without WhatsApp timestamp",
             "Created Date": "2026-11-26T03:00:00Z"},
        ]
        client.records.side_effect = [
            records, records,
            [{"phone": "60123456789", "name": "Marcus Yeoh"}],
        ]
        client.bounded_page.side_effect = [
            (records, False),
            (records, False),
        ]
        self.login()

        conversations = self.client.get("/api/inbox/conversations").get_json()["conversations"]

        self.assertEqual(conversations[0]["name"], "Marcus Yeoh")
        self.assertEqual(conversations[0]["preview"],
                         "Newest message without WhatsApp timestamp")
        self.assertEqual(conversations[0]["timestamp"], "2026-11-26T03:00:00Z")
        self.assertEqual(conversations[0]["direction"], "inbound")

        messages = self.client.get(
            "/api/inbox/messages?phone=60123456789"
        ).get_json()["messages"]
        self.assertEqual([message["id"] for message in messages], ["old", "new"])
        self.assertEqual(messages[-1]["timestamp"], conversations[0]["timestamp"])
        self.assertEqual(client.bounded_page.call_count, 2)
        self.assertTrue(all(call.args[1] for call in client.bounded_page.call_args_list))
        self.assertTrue(all(call.kwargs["limit"] == 30 for call in client.bounded_page.call_args_list))
        self.assertEqual(client.bounded_page.call_args_list[-1].args[1][0]["key"], "phone")

    @patch("dashboard_app.BubbleAPI")
    def test_inbox_paginates_thirty_with_stable_ties_and_search(self, client_class):
        client = client_class.return_value
        rows = [{
            "_id": f"m{i:02d}", "phone": f"6012000{i:04d}",
            "messageContent": f"Message {i}", "direction": "inbound",
            "whatsapp_timestamp": "2026-11-27T01:00:00Z",
            "Created Date": "2026-11-27T01:00:00Z",
        } for i in range(35)]
        client.records.side_effect = [rows, rows, [], rows, rows, []]
        client.bounded_page.side_effect = [
            ([rows[0]], False),
            ([rows[0]], False),
        ]
        self.login()

        first = self.client.get("/api/inbox/conversations").get_json()
        self.assertEqual(len(first["conversations"]), 30)
        self.assertTrue(first["has_more"])
        second = self.client.get(
            f'/api/inbox/conversations?cursor={first["next_cursor"]}'
        ).get_json()
        self.assertEqual(len(second["conversations"]), 5)
        self.assertFalse(second["has_more"])
        found = self.client.get("/api/inbox/conversations?search=6012").get_json()
        self.assertEqual(len(found["conversations"]), 30)

        # A separate app avoids sharing the conversation cache with this phone.
        phone = rows[0]["phone"]
        messages = self.client.get(f"/api/inbox/messages?phone={phone}").get_json()
        self.assertEqual(len(messages["messages"]), 1)

    @patch("dashboard_app.BubbleAPI")
    def test_message_pagination_is_chronological_deduplicated_and_stable(self, client_class):
        client = client_class.return_value
        phone = "60123456789"
        rows = [{
            "_id": f"m{i:02d}", "phone": "+60 12-345 6789",
            "messageContent": str(i), "direction": "outbound",
            "whatsapp_timestamp": "2026-11-27T01:00:00Z",
            "Created Date": "2026-11-27T01:00:00Z",
        } for i in range(65)]
        # The duplicate stream simulates the same rows returned by both Bubble sorts.
        chunks = [rows[35:], rows[35:], rows[5:35], rows[5:35], rows[:5], rows[:5]]
        client.bounded_page.side_effect = [
            (chunk, index < 4)
            for index, chunk in enumerate(chunks)
        ]
        self.login()
        first = self.client.get(f"/api/inbox/messages?phone={phone}").get_json()
        second = self.client.get(
            f'/api/inbox/messages?phone={phone}&cursor={first["next_cursor"]}'
        ).get_json()
        third = self.client.get(
            f'/api/inbox/messages?phone={phone}&cursor={second["next_cursor"]}'
        ).get_json()
        ids = [item["id"] for page in (first, second, third) for item in page["messages"]]
        self.assertEqual(len(ids), 65)
        self.assertEqual(len(set(ids)), 65)
        self.assertFalse(third["has_more"])
        self.assertEqual(ids[:2], ["m35", "m36"])

    @patch("dashboard_app.BubbleAPI")
    def test_not_sent_filter_applies_before_twenty_record_limit(self, client_class):
        client = client_class.return_value
        client.records.return_value = [
            {"_id": f"m{i}", "alert_sent": i < 5, "Created Date": f"2026-11-{27-i:02d}T01:00:00Z"}
            for i in range(25)
        ]
        client.records_by_ids.return_value = []
        self.login()
        payload = self.client.get("/api/matches?range=all&status=not_sent").get_json()
        self.assertEqual(len(payload["matches"]), 20)
        self.assertTrue(all(item["status"] == "Not sent" for item in payload["matches"]))
        self.assertIsNone(client.records.call_args.kwargs["limit"])

    def test_unsent_reason_precedence_and_threshold(self):
        self.assertEqual(_unsent_reason({}, None, 80), "Match not scored")
        self.assertEqual(_unsent_reason({"match_score": 79}, None, 80),
                         "Below minimum alert score (80)")
        exact = {"match_score": 80, "lead_owner": "u1", "listing_owner": "u1"}
        self.assertEqual(_unsent_reason(exact, None, 80),
                         "Same agent owns lead and listing")
        match = {"match_score": 90, "lead_owner": "u1", "listing_owner": "u2"}
        self.assertEqual(_unsent_reason(match, {"alerts": False, "phone": "60123456789"}, 80),
                         "Agent alerts disabled")
        self.assertEqual(_unsent_reason(match, {"alerts": True, "phone": "12"}, 80),
                         "Missing or invalid phone number")
        self.assertEqual(_unsent_reason(match, None, 80), "Reason not recorded")

    def test_recipient_inference_is_conservative(self):
        match = {"Created Date": "2026-11-27T01:02:00Z", "lead_owner": "lead-owner",
                 "listing_owner": "listing-owner"}
        lead = {"Created Date": "2026-11-27T01:00:00Z"}
        listing = {"Created Date": "2026-10-01T01:00:00Z"}
        self.assertEqual(_intended_recipient(match, lead, listing), "listing-owner")
        self.assertIsNone(_intended_recipient(match, {}, listing))
        match["alert_recipient"] = "persisted"
        self.assertEqual(_intended_recipient(match, {}, {}), "persisted")

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
        with self.assertLogs("bubble_api", level="WARNING") as logs:
            with self.assertRaisesRegex(BubbleAPIError, "temporarily unavailable"):
                BubbleAPI("https://bubble.test", "secret-token").count("lead")
        message = logs.output[0]
        self.assertIn("object_type=lead", message)
        self.assertIn("reason=timeout", message)
        self.assertIn("timeout=(3.05, 12)", message)
        self.assertNotIn("secret details", message)
        self.assertNotIn("secret-token", message)

    @patch("bubble_api.requests.Session.get")
    def test_http_status_and_reason_are_logged_without_response_contents(self, get):
        response = Mock(status_code=503, reason="Service Unavailable")
        response.raise_for_status.side_effect = __import__("requests").HTTPError(
            "private record contents", response=response
        )
        get.return_value = response
        with self.assertLogs("bubble_api", level="WARNING") as logs:
            with self.assertRaisesRegex(BubbleAPIError, "temporarily unavailable"):
                BubbleAPI("https://bubble.test", "secret-token").count("listing")
        message = logs.output[0]
        self.assertIn("object_type=listing", message)
        self.assertIn("status=503", message)
        self.assertIn("reason=Service Unavailable", message)
        self.assertNotIn("private record contents", message)
        self.assertNotIn("secret-token", message)

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
