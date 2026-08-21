import json
import os
import unittest
from unittest.mock import Mock, patch

import requests

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


class FakeViewingBubble:
    def __init__(self, folio_items=None, viewing_requests=None, availability=None):
        self.plan = {
            "lead": "lead-1",
            "startDateTime": "2026-08-29T10:00:00+08:00",
            "endDateTime": "2026-08-29T14:00:00+08:00",
            "defaultViewingDurationMinutes": 30,
            "travelBufferMinutes": 15,
        }
        self.folio_items = folio_items if folio_items is not None else [
            {
                "_id": "folio-item-1",
                "lead": "lead-1",
                "listing": "listing-1",
                "ViewingRequested": True,
                "priority": 5,
            },
            {
                "_id": "folio-item-false",
                "lead": "lead-1",
                "listing": "listing-false",
                "ViewingRequested": False,
            },
        ]
        self.listings = {
            "listing-1": {
                "_id": "listing-1", "name": "One Menerung",
                "agent": "agent-1", "agentPhone": "60111111111",
            },
            "listing-false": {"_id": "listing-false", "name": "Excluded"},
        }
        self.viewing_requests = list(viewing_requests or [])
        self.availability = availability or {}
        self.calls = []
        self.created_payloads = []

    def bubble(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith("/obj/viewingPlan/plan-1"):
            return self.plan
        if url.endswith("/obj/folioItem"):
            return {"results": list(self.folio_items), "remaining": 0}
        if "/obj/listing/" in url:
            return self.listings[url.rsplit("/", 1)[-1]]
        if url.endswith("/obj/viewingRequest"):
            return {"results": list(self.viewing_requests), "remaining": 0}
        if url.endswith("/obj/agentAvailability"):
            constraints = json.loads(kwargs["params"]["constraints"])
            request_id = next(
                item["value"] for item in constraints
                if item["key"] == "viewingRequest"
            )
            return {
                "results": list(self.availability.get(request_id, [])),
                "remaining": 0,
            }
        raise AssertionError(f"Unexpected Bubble URL: {url}")

    def post(self, url, **kwargs):
        if not url.endswith("/obj/viewingRequest"):
            raise AssertionError(f"Unexpected POST URL: {url}")
        payload = dict(kwargs["json"])
        self.created_payloads.append(payload)
        request_id = f"request-{len(self.viewing_requests) + 1}"
        self.viewing_requests.append({"_id": request_id, **payload})
        response = Mock()
        response.ok = True
        response.status_code = 201
        response.json.return_value = {"id": request_id}
        response.text = ""
        return response


class ViewingPlanEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def optimise(self, fake):
        with patch("app.bubble", side_effect=fake.bubble), patch(
            "app.requests.post", side_effect=fake.post
        ):
            return self.client.post(
                "/viewing_plan/plan-1/optimise?environment=development"
            )

    def test_plan_lead_lookup_and_exact_viewing_requested_query(self):
        fake = FakeViewingBubble()

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["folio_items_requested"], 1)
        self.assertEqual(body["requests_created"], 1)
        folio_query = next(
            kwargs for url, kwargs in fake.calls if url.endswith("/obj/folioItem")
        )
        self.assertEqual(json.loads(folio_query["params"]["constraints"]), [
            {"key": "lead", "constraint_type": "equals", "value": "lead-1"},
            {
                "key": "ViewingRequested",
                "constraint_type": "equals",
                "value": True,
            },
        ])

    def test_false_folio_item_is_excluded_and_listing_is_resolved(self):
        fake = FakeViewingBubble()

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 200)
        listing_urls = [url for url, _kwargs in fake.calls if "/obj/listing/" in url]
        self.assertEqual(listing_urls, [
            "https://www.rentee.asia/version-test/api/1.1/obj/listing/listing-1"
        ])
        self.assertNotIn("listing-false", json.dumps(fake.created_payloads))

    def test_missing_request_is_created_with_relationships_and_status(self):
        fake = FakeViewingBubble()

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake.created_payloads, [{
            "viewingPlan": "plan-1",
            "listing": "listing-1",
            "status": "Needs Contact",
            "agent": "agent-1",
            "agentPhone": "60111111111",
            "priority": 5,
        }])

    def test_repeated_optimise_does_not_duplicate_viewing_request(self):
        fake = FakeViewingBubble()

        first = self.optimise(fake)
        second = self.optimise(fake)

        self.assertEqual(first.get_json()["requests_created"], 1)
        self.assertEqual(second.get_json()["requests_created"], 0)
        self.assertEqual(second.get_json()["requests_existing"], 1)
        self.assertEqual(len(fake.created_payloads), 1)

    def test_existing_request_is_reused_and_availability_builds_schedule(self):
        existing = {
            "_id": "request-existing",
            "viewingPlan": "plan-1",
            "listing": "listing-1",
            "priority": 5,
        }
        availability = {
            "request-existing": [{
                "_id": "availability-1",
                "viewingRequest": "request-existing",
                "startDateTime": "2026-08-29T10:00:00+08:00",
                "endDateTime": "2026-08-29T12:00:00+08:00",
                "status": "Available",
                "sourceText": "Any time between ten and twelve",
            }]
        }
        fake = FakeViewingBubble([fake_item := {
            "_id": "folio-item-1", "lead": "lead-1",
            "listing": "listing-1", "ViewingRequested": True,
        }], [existing], availability)

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["requests_created"], 0)
        self.assertEqual(body["requests_existing"], 1)
        self.assertEqual(body["metrics"]["scheduled_count"], 1)
        self.assertEqual(body["appointments"][0]["listing_name"], "One Menerung")
        self.assertEqual(body["appointments"][0]["start"], "2026-08-29T10:00:00+08:00")
        self.assertEqual(fake_item["listing"], "listing-1")

    def test_no_qualifying_folio_items_returns_valid_empty_schedule(self):
        fake = FakeViewingBubble(folio_items=[{
            "_id": "not-requested", "lead": "lead-1",
            "listing": "listing-1", "ViewingRequested": False,
        }])

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["folio_items_requested"], 0)
        self.assertEqual(body["appointments"], [])
        self.assertEqual(body["metrics"]["total_requested"], 0)
        self.assertEqual(fake.created_payloads, [])

    def test_no_agent_availability_is_reported_without_failure(self):
        existing = {
            "_id": "request-existing", "viewingPlan": "plan-1",
            "listing": "listing-1",
        }
        fake = FakeViewingBubble(viewing_requests=[existing])

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["unscheduled"], [{
            "viewing_request_id": "request-existing",
            "reason": "no_agent_availability",
        }])

    def test_viewing_plan_not_found_returns_404(self):
        http_response = Mock(status_code=404)
        error = requests.HTTPError("not found", response=http_response)
        with patch("app.bubble", side_effect=error):
            response = self.client.post("/viewing_plan/missing/optimise")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "ViewingPlan not found.")

    def test_viewing_plan_without_lead_returns_400(self):
        fake = FakeViewingBubble()
        fake.plan.pop("lead")

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.get_json()["error"], "ViewingPlan has no linked Lead."
        )

    def test_invalid_tenant_window_returns_400(self):
        fake = FakeViewingBubble()
        fake.plan["endDateTime"] = fake.plan["startDateTime"]

        response = self.optimise(fake)

        self.assertEqual(response.status_code, 400)
        self.assertIn("must be after", response.get_json()["error"])

    def test_bubble_failure_is_safe(self):
        http_response = Mock(status_code=500, text="internal failure")
        error = requests.HTTPError("server error", response=http_response)
        with patch("app.bubble", side_effect=error):
            response = self.client.post("/viewing_plan/plan-1/optimise")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.get_json()["error"],
            "Bubble data is temporarily unavailable.",
        )


if __name__ == "__main__":
    unittest.main()
