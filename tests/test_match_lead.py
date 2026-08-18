import io
import json
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

import requests

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


def consume_generator(generator):
    statuses = []
    while True:
        try:
            statuses.append(next(generator))
        except StopIteration as completed:
            return statuses, completed.value


class MatchLeadStaleFolioItemTests(unittest.TestCase):
    @patch("app.requests.post")
    def test_create_folio_items_includes_core_relationships_and_summary(
        self, mocked_post
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"id": "folio-item-1"}
        mocked_post.return_value = response
        reco_summary = "  Exact personalised summary — unchanged.  "

        result = app_module.create_folio_items(
            [{
                "listing_id": "listing-1",
                "reco_summary": reco_summary,
            }],
            "folio-1",
            "lead-1",
            "https://www.rentee.asia/api/1.1",
        )

        self.assertEqual(result, ["folio-item-1"])
        self.assertEqual(mocked_post.call_args.kwargs["json"], {
            "listing": "listing-1",
            "folio": "folio-1",
            "lead": "lead-1",
            "newlyAdded": True,
            "RecoSummary": reco_summary,
        })

    def bubble_side_effect(self, url, **_kwargs):
        if url.endswith("/obj/folio/folio-1"):
            return {
                "lead": "lead-1",
                "folioItems": ["stale-item", "valid-item"],
            }
        if url.endswith("/obj/folioItem/stale-item"):
            response = Mock(status_code=404)
            raise requests.HTTPError("not found", response=response)
        if url.endswith("/obj/folioItem/valid-item"):
            return {"listing": "existing-listing", "newlyAdded": False}
        if url.endswith("/obj/lead/lead-1"):
            return {"AIsearchtext": "Two bedrooms near work"}
        raise AssertionError(f"Unexpected Bubble URL: {url}")

    @patch("app.update_folio_items")
    @patch("app.create_folio_items", return_value=["new-item"])
    @patch("app.get_all_listings")
    @patch("app.bubble")
    def test_stale_item_is_skipped_and_matching_continues(
        self, mocked_bubble, mocked_listings, mocked_create, mocked_update
    ):
        mocked_bubble.side_effect = self.bubble_side_effect
        mocked_listings.return_value = [{
            "_id": "new-listing", "beds": 2, "priceRent": 5000,
            "AIsearchtext": "Two-bedroom home near work",
        }]
        model_response = SimpleNamespace(
            output_text=json.dumps({
                "recommendations": [{
                    "listing_id": "new-listing",
                    "reco_summary": "A strong fit.",
                }],
                "customer_response": "I found a strong option.",
            }),
            usage=None,
        )

        output = io.StringIO()
        with patch.object(
            app_module.client.responses, "create", return_value=model_response
        ) as mocked_match, redirect_stdout(output):
            statuses, result = consume_generator(
                app_module.match_lead("folio-1", "live")
            )

        self.assertEqual(result, "I found a strong option.")
        self.assertIn("Ranking the best matches...", statuses)
        self.assertIn(
            "Skipping stale FolioItem reference: stale-item",
            output.getvalue(),
        )
        self.assertIn(
            call("https://www.rentee.asia/api/1.1/obj/folioItem/valid-item"),
            mocked_bubble.call_args_list,
        )
        mocked_match.assert_called_once()
        mocked_create.assert_called_once_with(
            [{
                "listing_id": "new-listing",
                "reco_summary": "A strong fit.",
            }],
            "folio-1",
            "lead-1",
            "https://www.rentee.asia/api/1.1",
        )
        mocked_update.assert_called_once_with(
            "folio-1", ["valid-item", "new-item"],
            "https://www.rentee.asia/api/1.1",
        )

    @patch("app.bubble")
    def test_non_404_folio_item_error_is_not_swallowed(self, mocked_bubble):
        response = Mock(status_code=500)
        mocked_bubble.side_effect = [
            {"lead": "lead-1", "folioItems": ["broken-item"]},
            requests.HTTPError("server error", response=response),
        ]

        generator = app_module.match_lead("folio-1", "live")
        self.assertEqual(next(generator), "Checking your preferences...")
        with self.assertRaises(requests.HTTPError):
            next(generator)


class MatchLeadCurrentRequestScopeTests(unittest.TestCase):
    def capture_matching_prompt(self, current_request, customer_response="No matches."):
        def bubble_side_effect(url, **_kwargs):
            if url.endswith("/obj/folio/folio-1"):
                return {"lead": "lead-1", "folioItems": []}
            if url.endswith("/obj/lead/lead-1"):
                return {
                    "AIsearchtext": (
                        "Budget: maximum RM8,000; bedrooms: 3; "
                        "furnishing: fully furnished"
                    )
                }
            raise AssertionError(f"Unexpected Bubble URL: {url}")

        model_response = SimpleNamespace(
            output_text=json.dumps({
                "recommendations": [],
                "customer_response": customer_response,
            }),
            usage=None,
        )
        listings = [
            {
                "_id": "dc-listing", "beds": 3, "priceRent": 7500,
                "AIsearchtext": "DC Residensi, fully furnished",
            },
            {
                "_id": "other-listing", "beds": 3, "priceRent": 7000,
                "AIsearchtext": "Unrelated Bangsar condo, fully furnished",
            },
        ]
        with (
            patch("app.bubble", side_effect=bubble_side_effect),
            patch("app.get_all_listings", return_value=listings),
            patch.object(
                app_module.client.responses, "create", return_value=model_response
            ) as mocked_match,
        ):
            _statuses, result = consume_generator(
                app_module.match_lead("folio-1", "live", current_request)
            )
        return mocked_match.call_args.kwargs["input"], result

    def test_named_condo_requests_are_separate_hard_turn_scopes(self):
        for request_text, scope in (
            ("Show me units in DC Residensi", "DC Residensi"),
            ("Show me units in One Menerung", "One Menerung"),
            ("Show me condos in Mont Kiara", "Mont Kiara"),
        ):
            with self.subTest(scope=scope):
                prompt, _result = self.capture_matching_prompt(request_text)
                self.assertIn("CURRENT CUSTOMER REQUEST", prompt)
                self.assertIn(request_text, prompt)
                self.assertIn("that named scope is a hard constraint", prompt)
                self.assertIn("Recommend only", prompt)
                self.assertIn("explicitly in that scope", prompt)

    def test_persistent_requirements_are_applied_within_current_scope(self):
        prompt, _result = self.capture_matching_prompt(
            "Show me units in DC Residensi"
        )
        self.assertIn("PERSISTENT HOME SEEKER REQUIREMENTS", prompt)
        # This exact content comes from Lead.AIsearchtext in capture_matching_prompt.
        self.assertIn("Budget: maximum RM8,000", prompt)
        self.assertIn("bedrooms: 3", prompt)
        self.assertIn("furnishing: fully furnished", prompt)
        self.assertIn("rank and filter within it", prompt)

    def test_no_scoped_listings_requires_clear_no_match_without_substitution(self):
        prompt, result = self.capture_matching_prompt(
            "Show me units in One Menerung",
            "There are no current matching listings in One Menerung.",
        )
        self.assertEqual(
            result, "There are no current matching listings in One Menerung."
        )
        self.assertIn("return an empty recommendations array", prompt)
        self.assertIn("Do not substitute other", prompt)

    def test_general_follow_up_has_no_inherited_turn_scope(self):
        prompt, _result = self.capture_matching_prompt("What else do you have?")
        self.assertIn("What else do you have?", prompt)
        self.assertIn("has no hard location scope", prompt)

    def test_open_to_statement_is_not_automatically_a_hard_scope(self):
        prompt, _result = self.capture_matching_prompt(
            "I'm also open to DC Residensi"
        )
        self.assertIn("does not create a hard scope", prompt)
        self.assertIn("I'm also open to DC Residensi", prompt)


if __name__ == "__main__":
    unittest.main()
