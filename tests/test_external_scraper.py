import os
import unittest
from unittest.mock import MagicMock, patch

import requests

import external_scraper


DEVELOPMENTS = [
    {"_id": "dev-one", "name": "One Menerung"},
    {"_id": "dev-other", "name": "Serai"},
]


def eligible_lead(**updates):
    lead = {
        "TransactionType": ["Rent/Let"],
        "preferredDevelopments": ["dev-one"],
        "budgetRent": 15000,
        "bedroomsMin": 3,
    }
    lead.update(updates)
    return lead


class ExternalScraperEligibilityTests(unittest.TestCase):
    def lookup(self, lead=None, matches=None, classifier=None):
        return external_scraper.build_external_lookup(
            lead or eligible_lead(), matches or [], DEVELOPMENTS, "lead-1",
            classifier or (lambda _lead, _listing: 2),
        )

    def test_feature_flag_is_disabled_by_default_and_requires_true(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(external_scraper.external_scraper_enabled())
            self.assertIsNone(self.lookup())
        with patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": " TRUE "}, clear=True):
            self.assertTrue(external_scraper.external_scraper_enabled())

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_eligible_rental_lead_without_tier_one_builds_lookup(self):
        self.assertEqual(self.lookup(), {
            "lead_id": "lead-1", "development": "One Menerung",
            "bedrooms_min": 3, "budget_rent": 15000,
        })

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_tier_one_blocks_lookup_but_tier_two_and_three_do_not(self):
        listing = {"_id": "listing-1"}
        self.assertIsNone(self.lookup(eligible_lead(), [listing],
                                      lambda _lead, _listing: 1))
        for tier in (2, 3):
            self.assertIsNotNone(self.lookup(matches=[listing], classifier=(
                lambda _lead, _listing, value=tier: value
            )))

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_sale_or_mixed_transaction_blocks_lookup(self):
        for transactions in (["Buy/Sell"], ["Rent/Let", "Buy/Sell"]):
            self.assertIsNone(self.lookup(eligible_lead(
                TransactionType=transactions
            )))

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_development_must_be_single_resolved_one_menerung(self):
        for preferred in ([], ["dev-one", "dev-other"], ["missing"], ["dev-other"]):
            self.assertIsNone(self.lookup(eligible_lead(
                preferredDevelopments=preferred
            )))

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_budget_and_bedrooms_must_be_valid_numbers(self):
        for budget in (None, 0, -1, True, "15000"):
            self.assertIsNone(self.lookup(eligible_lead(budgetRent=budget)))
        for bedrooms in (None, -1, True, "3"):
            self.assertIsNone(self.lookup(eligible_lead(bedroomsMin=bedrooms)))
        self.assertIsNotNone(self.lookup(eligible_lead(bedroomsMin=0)))


class ExternalScraperClientTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {
            "EXTERNAL_SCRAPER_ENABLED": "true",
            "EXTERNAL_SCRAPER_URL": "https://scraper.rentee.asia/api/external-matches",
            "EXTERNAL_SCRAPER_API_KEY": "secret",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.lookup = {
            "lead_id": "lead-1", "development": "One Menerung",
            "bedrooms_min": 3, "budget_rent": 15000,
        }

    @staticmethod
    def response(body):
        response = MagicMock()
        response.json.return_value = body
        return response

    def body(self, matches):
        return {
            "development": "One Menerung", "source": "iproperty",
            "total_matches": len(matches), "matches": matches,
        }

    def listing(self, **updates):
        item = {
            "source_listing_id": "200540456",
            "source_url": "https://www.iproperty.com.my/property/bangsar/one-menerung/rent-200540456/",
            "agent": "Fateh AL", "price_rent": 15000, "beds": 3,
            "sq_ft": 3282,
        }
        item.update(updates)
        return item

    def test_successful_response_is_validated_deduplicated_and_limited(self):
        matches = [self.listing()]
        matches.append(self.listing(source_url="https://www.iproperty.com.my/duplicate"))
        for index in range(1, 5):
            matches.append(self.listing(
                source_listing_id=str(index),
                source_url=f"https://www.iproperty.com.my/listing/{index}",
                price_rent=14000,
            ))
        post = MagicMock(return_value=self.response(self.body(matches)))
        result = external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        )
        self.assertEqual(len(result), 3)
        self.assertEqual([item["source_listing_id"] for item in result],
                         ["200540456", "1", "2"])
        request = post.call_args
        self.assertEqual(request.kwargs["timeout"], 165)
        self.assertEqual(request.kwargs["headers"]["X-API-Key"], "secret")

    def test_empty_response_is_successful(self):
        post = MagicMock(return_value=self.response(self.body([])))
        self.assertEqual(external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        ), [])

    def test_timeout_http_error_and_malformed_json_fail_gracefully(self):
        for error in (requests.Timeout(), requests.HTTPError(), ValueError("json")):
            post = MagicMock()
            if isinstance(error, requests.HTTPError):
                response = self.response(self.body([]))
                response.raise_for_status.side_effect = error
                post.return_value = response
            else:
                post.side_effect = error
            self.assertIsNone(external_scraper.fetch_external_matches(
                self.lookup, request_post=post
            ))

    def test_invalid_metadata_and_listing_values_are_rejected(self):
        bad_metadata = self.body([])
        bad_metadata["source"] = "other"
        post = MagicMock(return_value=self.response(bad_metadata))
        self.assertIsNone(external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        ))
        invalid = [
            self.listing(source_listing_id="http", source_url="http://www.iproperty.com.my/x"),
            self.listing(source_listing_id="host", source_url="https://evil.example/x"),
            self.listing(source_listing_id="price", price_rent=15001),
            self.listing(source_listing_id="beds", beds=2),
        ]
        post = MagicMock(return_value=self.response(self.body(invalid)))
        self.assertEqual(external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        ), [])

    def test_formatting_and_run_persist_the_outbound_message(self):
        listing = self.listing()
        post = MagicMock(return_value=self.response(self.body([listing])))
        send = MagicMock(return_value=["wamid.external"])
        save = MagicMock()
        result = external_scraper.run_external_lookup(
            self.lookup, "60123456789", send_text=send,
            save_message=save, request_post=post,
        )
        message = send.call_args.args[1]
        self.assertEqual(result["status"], "sent")
        self.assertIn("*1 additional listing found on iProperty*", message)
        self.assertIn("RM15,000/month · 3 bed · 3,282 sqft", message)
        self.assertIn("Availability has not been verified", message)
        self.assertEqual(save.call_args.kwargs["lead_id"], "lead-1")

    def test_zero_results_message_and_failure_sends_nothing(self):
        empty_post = MagicMock(return_value=self.response(self.body([])))
        send = MagicMock(return_value=["wamid.empty"])
        result = external_scraper.run_external_lookup(
            self.lookup, "60123456789", send_text=send,
            save_message=MagicMock(), request_post=empty_post,
        )
        self.assertEqual(result["status"], "sent")
        self.assertIn("couldn't find any additional listings", send.call_args.args[1])

        failed_send = MagicMock()
        result = external_scraper.run_external_lookup(
            self.lookup, "60123456789", send_text=failed_send,
            save_message=MagicMock(), request_post=MagicMock(side_effect=requests.Timeout()),
        )
        self.assertEqual(result["status"], "failed")
        failed_send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
