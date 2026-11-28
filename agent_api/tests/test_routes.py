import unittest
from unittest.mock import patch

from flask import Flask

from agent_api.auth import AuthError
from agent_api.bubble_client import BubbleUnavailable
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
        self.many_calls = []

    def one(self, kind, record_id):
        return self.rows.get(kind, {}).get(record_id)

    def patch(self, kind, record_id, payload):
        self.patches.append((kind, record_id, payload))
        self.rows[kind][record_id].update(payload)

    def all(self, kind, constraints, maximum=1000):
        rows=list(self.rows.get(kind, {}).values())
        def matches(row, constraint):
            expected=constraint["value"]
            if constraint["constraint_type"] == "in": return row.get(constraint["key"]) in expected
            if constraint["constraint_type"] == "greater than or equal": return str(row.get(constraint["key"]) or "") >= str(expected)
            return row.get(constraint["key"]) == expected
        filtered=[row for row in rows if all(matches(row,c) for c in constraints)]
        return filtered if maximum is None else filtered[:maximum]

    def many(self, kind, record_ids, chunk_size=50):
        ids=set(record_ids); self.many_calls.append((kind,ids))
        return [row for key,row in self.rows.get(kind,{}).items() if key in ids]

    def search(self, kind, constraints, limit=100, cursor=0, sort_field=None, descending=False):
        rows=self.all(kind,constraints,1000)
        return rows[cursor:cursor+limit],max(0,len(rows)-cursor-limit)


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

    def test_inventory_lists_omit_messages_until_owned_detail(self):
        self.bubble.rows["listing"]["l1"]["owner"] = "u1"
        listing_list = self.client.get("/api/agent/listings")
        self.assertEqual(listing_list.status_code, 200)
        self.assertEqual(len(listing_list.get_json()["items"]), 1)
        self.assertNotIn("original_message", listing_list.get_json()["items"][0])

        listing_detail = self.client.get("/api/agent/listings/l1")
        self.assertEqual(listing_detail.status_code, 200)
        self.assertEqual(listing_detail.get_json()["original_message"], "Condo available")

        lead_list = self.client.get("/api/agent/leads")
        self.assertEqual(lead_list.status_code, 200)
        self.assertNotIn("original_message", lead_list.get_json()["items"][0])
        lead_detail = self.client.get("/api/agent/leads/q1")
        self.assertEqual(lead_detail.get_json()["original_message"], "Need a family home")

    def test_match_list_omits_messages_but_detail_loads_them(self):
        response = self.client.get("/api/agent/matches")
        self.assertEqual(response.status_code, 200)
        row = response.get_json()["items"][0]
        self.assertNotIn("original_message", row["listing"])
        self.assertNotIn("original_message", row["lead"])
        detail = self.client.get("/api/agent/matches/m1")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.get_json()["listing"]["original_message"], "Condo available")
        self.assertEqual(detail.get_json()["lead"]["original_message"], "Need a family home")

    def test_unowned_listing_detail_cannot_disclose_message(self):
        response = self.client.get("/api/agent/listings/l1")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Condo available", response.get_data(as_text=True))

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

    def test_legacy_match_without_owner_fields_is_found_through_owned_inventory(self):
        match=self.bubble.rows["match"]["m1"]
        match.update({"Created Date":"2099-01-01T00:00:00Z", "lead_review_status":""})
        match.pop("lead_owner",None); match.pop("listing_owner",None)
        response=self.client.get("/api/agent/matches")
        self.assertEqual(response.status_code,200)
        self.assertEqual([item["id"] for item in response.get_json()["items"]],["m1"])
        self.assertTrue(response.get_json()["items"][0]["actionable"])

    def test_low_score_match_is_rejected_before_relationship_fetch(self):
        self.bubble.rows["match"]["low"]={"_id":"low","lead":"missing-lead","listing":"missing-listing","match_score":20,"lead_owner":"u1"}
        response=self.client.get("/api/agent/matches")
        self.assertEqual(response.status_code,200)
        fetched_ids=set().union(*(ids for kind,ids in self.bubble.many_calls if kind in {"lead","listing"}))
        self.assertNotIn("missing-lead",fetched_ids)
        self.assertNotIn("missing-listing",fetched_ids)

    def test_old_match_remains_historical_but_not_actionable(self):
        match=self.bubble.rows["match"]["m1"]
        match.update({"Created Date":"2020-01-01T00:00:00Z", "lead_review_status":""})
        items=self.client.get("/api/agent/matches").get_json()["items"]
        home=self.client.get("/api/agent/home").get_json()
        self.assertEqual(len(items),1)
        self.assertFalse(items[0]["actionable"])
        self.assertEqual((home["lifetime_matches"],home["actionable_matches"]),(1,0))

    def test_match_diagnostics_include_every_aggregate_stage(self):
        self.bubble.rows["match"]["m1"].update({"Created Date":"2099-01-01T00:00:00Z"})
        with self.assertLogs("agent_api.routes",level="INFO") as captured:
            self.client.get("/api/agent/matches")
        line=" ".join(captured.output)
        for field in ("raw_lead_owner", "raw_listing_owner", "inventory_discovered",
                      "combined_before_filtering", "ownership_excluded", "missing_score_excluded",
                      "below_score_excluded", "inactive_excluded", "age_excluded_actionable",
                      "review_excluded", "duplicates_removed", "final_historical", "final_actionable"):
            self.assertIn(f"{field}=",line)
        self.assertNotIn("u1",line)

    def test_bubble_failure_is_503_not_an_empty_match_list(self):
        self.bubble.all=lambda *_args,**_kwargs: (_ for _ in ()).throw(BubbleUnavailable("down"))
        response=self.client.get("/api/agent/matches")
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.get_json(),{"error":"Data is temporarily unavailable"})

    def test_relationship_ids_are_bulk_resolved_to_display_names(self):
        self.bubble.rows.update({
            "condo": {
                "1700000000001x100000000000001": {"_id":"1700000000001x100000000000001","name":"One Menerung"},
                "1700000000002x100000000000002": {"_id":"1700000000002x100000000000002","Condo name":"Serai"},
            },
            "geo": {
                "1700000000003x100000000000003": {"_id":"1700000000003x100000000000003","Name":"Bangsar"},
                "1700000000004x100000000000004": {"_id":"1700000000004x100000000000004","name":"Damansara Heights"},
            },
            "agency": {"1700000000005x100000000000005":{"_id":"1700000000005x100000000000005","Name":"Cedar Property"}},
        })
        self.bubble.rows["user"]["u1"]["Agency"]="1700000000005x100000000000005"
        self.bubble.rows["listing"]["l1"].update({"owner":"u1","condo":"1700000000001x100000000000001","Geo":"1700000000003x100000000000003"})
        self.bubble.rows["lead"]["q1"].update({
            "preferredDevelopments":["1700000000001x100000000000001","1700000000002x100000000000002"],
            "Geo":["1700000000003x100000000000003","1700000000004x100000000000004"],
        })

        listing=self.client.get("/api/agent/listings").get_json()["items"][0]
        lead=self.client.get("/api/agent/leads").get_json()["items"][0]
        self.assertEqual((listing["title"],listing["location"]),("One Menerung","Bangsar"))
        self.assertEqual(lead["developments"],["One Menerung","Serai"])
        self.assertEqual(lead["location"],"Bangsar, Damansara Heights")
        self.assertEqual(lead["owner"]["agency"],"Cedar Property")
        rendered=str((listing,lead))
        self.assertNotIn("170000000000",rendered)

    def test_unresolved_relationships_use_labels_not_raw_ids(self):
        raw="1700000000099x100000000000099"
        self.bubble.rows["listing"]["l1"].update({"owner":"u1","condo":raw,"Geo":raw})
        item=self.client.get("/api/agent/listings").get_json()["items"][0]
        self.assertEqual(item["title"],"Listing")
        self.assertEqual(item["location"],"Area not specified")
        self.assertNotIn(raw,str(item))


if __name__ == "__main__":
    unittest.main()
