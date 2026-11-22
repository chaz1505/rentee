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
            "development_id": "dev-one",
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
    def test_cached_scraped_tier_one_listing_does_not_block_lookup(self):
        cached = {"_id": "listing-cached", "source": "iproperty", "scraped?": "Yes"}
        self.assertIsNotNone(self.lookup(
            matches=[cached], classifier=lambda _lead, _listing: 1
        ))

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
            "development_id": "dev-one",
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
            self.listing(source_listing_id="price", price_rent=18001),
            self.listing(source_listing_id="beds", beds=2),
        ]
        post = MagicMock(return_value=self.response(self.body(invalid)))
        self.assertEqual(external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        ), [])

    def test_budget_boundaries_match_internal_eighty_to_one_twenty_rule(self):
        matches = [
            self.listing(source_listing_id="low", price_rent=12000,
                         source_url="https://www.iproperty.com.my/low"),
            self.listing(source_listing_id="high", price_rent=18000,
                         source_url="https://www.iproperty.com.my/high"),
            self.listing(source_listing_id="too-low", price_rent=11999,
                         source_url="https://www.iproperty.com.my/too-low"),
            self.listing(source_listing_id="too-high", price_rent=18001,
                         source_url="https://www.iproperty.com.my/too-high"),
        ]
        post = MagicMock(return_value=self.response(self.body(matches)))
        result = external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        )
        self.assertEqual([item["source_listing_id"] for item in result],
                         ["low", "high"])

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


class ExternalBubblePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.app = MagicMock()
        self.app.get_bubble_base_url.return_value = "https://bubble.test/api/1.1"
        self.app.normalize_phone.side_effect = lambda value: value
        self.bubble = patch.object(external_scraper, "_bubble", return_value=self.app)
        self.bubble.start()
        self.addCleanup(self.bubble.stop)
        self.lookup = {
            "lead_id": "lead-1", "development_id": "dev-one",
            "development": "One Menerung", "bedrooms_min": 3,
            "budget_rent": 15000,
        }
        self.item = {
            "source_listing_id": "external-1",
            "source_url": "https://www.iproperty.com.my/listing/external-1",
            "agent": "Agent External", "source_agency_name": "Agency External",
            "price_rent": 18000, "beds": 3, "sq_ft": 3282,
            "description": "Park view",
        }

    def test_external_listing_creation_maps_only_supported_fields(self):
        self.app._bubble_records.return_value = []
        self.app._bubble_create.return_value = "listing-1"
        listing, created = external_scraper.upsert_external_listing(
            self.item, self.lookup
        )
        self.assertTrue(created)
        self.assertEqual(listing["_id"], "listing-1")
        payload = self.app._bubble_create.call_args.args[2]
        self.assertEqual(payload["scraped?"], "Yes")
        self.assertEqual(payload["source"], "iproperty")
        self.assertEqual(payload["sourceListingID"], "external-1")
        self.assertEqual(payload["development"], "dev-one")
        self.assertEqual(payload["TransactionType"], ["Rent/Let"])
        self.assertEqual(payload["propertyType"], "Condo")
        self.assertEqual(payload["Sq Ft"], 3282)
        self.assertNotIn("owner", payload)

    def test_existing_external_listing_is_updated_without_owner_changes(self):
        self.app._bubble_records.return_value = [
            {"_id": "listing-existing", "owner": "user-manual"}
        ]
        listing, created = external_scraper.upsert_external_listing(
            self.item, self.lookup
        )
        self.assertFalse(created)
        self.assertEqual(listing["owner"], "user-manual")
        payload = self.app._bubble_patch.call_args.args[1]
        self.assertNotIn("owner", payload)
        self.app._bubble_create.assert_not_called()

    def test_external_match_has_source_tier_owner_and_no_alert(self):
        self.app._bubble_records.return_value = []
        self.app._bubble_create.return_value = "match-1"
        lead = {
            "_id": "lead-1", "owner": "lead-owner",
            "TransactionType": ["Rent/Let"], "budgetRent": 15000,
            "bedroomsMin": 3, "preferredDevelopments": ["dev-one"],
        }
        listing = {
            "_id": "listing-1", "development": "dev-one",
            "TransactionType": ["Rent/Let"], "priceRent": 18000, "beds": 3,
        }
        match_id, created = external_scraper.create_external_match(lead, listing)
        self.assertEqual((match_id, created), ("match-1", True))
        payload = self.app._bubble_create.call_args.args[2]
        self.assertEqual(payload, {
            "lead": "lead-1", "listing": "listing-1",
            "match_source": "iproperty", "match_tier": 1,
            "lead_owner": "lead-owner",
        })

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_enqueue_deduplicates_by_stable_lead_job_key(self):
        existing = {"_id": "job-existing", "jobKey": "iproperty:lead-1"}
        self.app._bubble_records.return_value = [existing]
        result = external_scraper.enqueue_external_lookup(
            self.lookup, "60123456789"
        )
        self.assertEqual(result, ("job-existing", False))
        self.app._bubble_create.assert_not_called()

    def test_followup_uses_rentee_and_original_listing_links(self):
        message = external_scraper.format_external_matches("One Menerung", [
            dict(self.item, bubble_listing_id="listing-1")
        ])
        self.assertIn("https://www.rentee.asia/listing/listing-1", message)
        self.assertIn(self.item["source_url"], message)

    def test_partial_persistence_failure_retries_without_sending(self):
        job = {
            "_id": "job-1", "attempts": 1, "phone": "60123456789",
            "payload": __import__("json").dumps(self.lookup),
        }
        self.app._bubble_records_by_ids.return_value = [{
            "_id": "lead-1", "owner": "lead-owner",
            "TransactionType": ["Rent/Let"], "budgetRent": 15000,
            "bedroomsMin": 3, "preferredDevelopments": ["dev-one"],
        }]
        send = MagicMock()
        with patch.object(external_scraper, "fetch_external_matches",
                          return_value=[self.item]), \
             patch.object(external_scraper, "upsert_external_listing",
                          side_effect=RuntimeError("Bubble unavailable")):
            result = external_scraper.process_external_job(
                job, send_text=send, save_message=MagicMock()
            )
        self.assertEqual(result["status"], "retry")
        send.assert_not_called()
        retry_payload = self.app._bubble_patch.call_args.args[1]
        self.assertEqual(retry_payload["status"], "Retry")

    def test_retry_with_notification_already_sent_does_not_send_twice(self):
        job = {
            "_id": "job-1", "attempts": 2, "phone": "60123456789",
            "notificationSent": True,
            "payload": __import__("json").dumps(self.lookup),
        }
        lead = {
            "_id": "lead-1", "owner": "lead-owner",
            "TransactionType": ["Rent/Let"], "budgetRent": 15000,
            "bedroomsMin": 3, "preferredDevelopments": ["dev-one"],
        }
        self.app._bubble_records_by_ids.return_value = [lead]
        listing = {
            "_id": "listing-1", "development": "dev-one",
            "TransactionType": ["Rent/Let"], "priceRent": 15000, "beds": 3,
        }
        send = MagicMock()
        with patch.object(external_scraper, "fetch_external_matches",
                          return_value=[self.item]), \
             patch.object(external_scraper, "upsert_external_listing",
                          return_value=(listing, False)), \
             patch.object(external_scraper, "create_external_match",
                          return_value=("match-1", False)):
            result = external_scraper.process_external_job(
                job, send_text=send, save_message=MagicMock()
            )
        self.assertEqual(result["status"], "completed")
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
