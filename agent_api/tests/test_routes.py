import unittest
from unittest.mock import patch

from flask import Flask

from agent_api.auth import AuthError
from agent_api.routes import blueprint


class FakeBubble:
    def __init__(self):
        self.rows = {
            "match": {"m1": {"_id": "m1", "lead": "q1", "listing": "l1", "match_score": 92, "match_reason": "Good fit", "lead_review_status": "reviewed"}},
            "lead": {"q1": {"_id": "q1", "owner": "u1", "Geo": {"name": "Bangsar"}, "waMessage": "Need a family home", "Notes": "private lead note"}},
            "listing": {"l1": {"_id": "l1", "owner": "u2", "Geo": {"name": "Bangsar"}, "Sq Ft": 1200, "waMessage": "Condo available", "phone": "listing-private", "Notes": "private listing note"}},
            "user": {"u1": {"_id": "u1", "name": "Aisha", "phone": "+60111111111"}, "u2": {"_id": "u2", "name": "Daniel", "Agency": "North & Co", "phone": "+60122222222"}},
        }
        self.patches = []

    def one(self, kind, record_id):
        return self.rows.get(kind, {}).get(record_id)

    def patch(self, kind, record_id, payload):
        self.patches.append((kind, record_id, payload))
        self.rows[kind][record_id].update(payload)

    def all(self, kind, constraints, maximum=1000):
        rows=list(self.rows.get(kind, {}).values())
        return [row for row in rows if all(row.get(c["key"]) == c["value"] for c in constraints)][:maximum]


class MatchRouteTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.register_blueprint(blueprint)
        self.client = self.app.test_client()
        self.bubble = FakeBubble()
        self.auth = patch("agent_api.routes.authenticate", return_value=(self.bubble.rows["user"]["u1"], {}, self.bubble))
        self.auth.start()

    def tearDown(self):
        self.auth.stop()

    def test_match_detail_allows_approved_counterparty_fields_only(self):
        response = self.client.get("/api/agent/matches/m1")
        self.assertEqual(response.status_code, 200)
        listing = response.get_json()["listing"]
        self.assertEqual(listing["original_message"], "Condo available")
        self.assertEqual(listing["owner"], {"id": "u2", "name": "Daniel", "agency": "North & Co"})
        self.assertEqual(listing["size"], 1200)
        self.assertNotIn("phone", listing)
        self.assertNotIn("Notes", listing)

    def test_contact_uses_counterparty_user_phone(self):
        response = self.client.get("/api/agent/matches/m1/contact")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["phone"], "+60122222222")
        self.assertNotEqual(response.get_json()["phone"], "listing-private")

    def test_nonparticipant_cannot_access_match_or_contact(self):
        outsider = {"_id": "u3", "name": "Outsider"}
        with patch("agent_api.routes.authenticate", return_value=(outsider, {}, self.bubble)):
            self.assertEqual(self.client.get("/api/agent/matches/m1").status_code, 404)
            self.assertEqual(self.client.get("/api/agent/matches/m1/contact").status_code, 404)

    def test_read_only_pilot_has_no_review_mutation_route(self):
        response = self.client.post("/api/agent/matches/m1/review", json={"status": "dismissed"}, headers={"Origin": "https://app.rentee.asia"})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(self.bubble.patches, [])

    def test_authorized_post_preflight_includes_required_cors_headers(self):
        response = self.client.options(
            "/api/agent/auth/redeem",
            headers={
                "Origin": "https://app.rentee.asia",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://app.rentee.asia")
        self.assertEqual(response.headers["Access-Control-Allow-Credentials"], "true")
        self.assertEqual(response.headers["Access-Control-Allow-Headers"], "Content-Type")
        self.assertEqual(response.headers["Access-Control-Allow-Methods"], "GET, POST, OPTIONS")
        self.assertIn("Origin", response.headers.getlist("Vary"))

    def test_unauthorized_origin_preflight_remains_blocked(self):
        response = self.client.options(
            "/api/agent/auth/redeem",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_authentication_errors_include_cors_for_authorized_origin(self):
        with patch("agent_api.routes.authenticate", side_effect=AuthError()):
            response = self.client.get("/api/agent/me", headers={"Origin": "https://app.rentee.asia"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://app.rentee.asia")
        self.assertEqual(response.headers["Access-Control-Allow-Credentials"], "true")

    def test_home_and_inbox_use_consistent_deduplicated_match_count(self):
        match=self.bubble.rows["match"]["m1"]
        match.update({"lead_owner":"u1","listing_owner":"u2","Created Date":"2099-01-01T00:00:00Z"})
        self.bubble.rows["match"]["older"]={**match,"_id":"older","Created Date":"2020-01-01T00:00:00Z"}
        inbox=self.client.get("/api/agent/matches").get_json()["items"]
        home=self.client.get("/api/agent/home").get_json()
        self.assertEqual(len(inbox),1)
        self.assertEqual(home["lifetime_matches"],1)
        self.assertEqual(home["actionable_matches"],0)


if __name__ == "__main__":
    unittest.main()
