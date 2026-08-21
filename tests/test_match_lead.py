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
        mocked_create.assert_called_once()
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


if __name__ == "__main__":
    unittest.main()
