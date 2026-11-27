import os
import datetime
import unittest
from unittest.mock import MagicMock, patch

import requests

import external_scraper


DEVELOPMENTS = [
    {"_id": "dev-one", "name": "One Menerung"},
    {"_id": "dev-troika", "name": "The Troika"},
    {"_id": "dev-other", "name": "Serai"},
    {"_id": "dev-unnamed", "name": "   "},
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
            self.assertEqual(self.lookup(), [])
        with patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": " TRUE "}, clear=True):
            self.assertTrue(external_scraper.external_scraper_enabled())

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_eligible_rental_lead_without_tier_one_builds_lookup(self):
        self.assertEqual(self.lookup(), [{
            "lead_id": "lead-1", "development": "One Menerung",
            "development_id": "dev-one",
            "bedrooms_min": 3, "budget_rent": 15000,
        }])

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_tier_one_blocks_lookup_but_tier_two_and_three_do_not(self):
        listing = {"_id": "listing-1"}
        listing["development"] = "dev-one"
        self.assertEqual(self.lookup(eligible_lead(), [listing],
                                     lambda _lead, _listing: 1), [])
        for tier in (2, 3):
            self.assertTrue(self.lookup(matches=[listing], classifier=(
                lambda _lead, _listing, value=tier: value
            )))

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_cached_scraped_tier_one_listing_does_not_block_lookup(self):
        for value in (True, "true", "Yes"):
            cached = {
                "_id": "listing-cached", "source": "iproperty",
                "scraped?": value,
            }
            self.assertTrue(self.lookup(
                matches=[cached], classifier=lambda _lead, _listing: 1
            ))

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_sale_or_mixed_transaction_blocks_lookup(self):
        for transactions in (["Buy/Sell"], ["Rent/Let", "Buy/Sell"]):
            self.assertEqual(self.lookup(eligible_lead(
                TransactionType=transactions
            )), [])

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_any_single_resolved_named_development_builds_lookup(self):
        for development_id, development_name in (
            ("dev-one", "One Menerung"),
            ("dev-troika", "The Troika"),
            ("dev-other", "Serai"),
        ):
            lookup = self.lookup(eligible_lead(
                preferredDevelopments=[development_id]
            ))
            self.assertEqual(lookup[0]["development_id"], development_id)
            self.assertEqual(lookup[0]["development"], development_name)

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_multiple_developments_are_deduplicated_and_resolved(self):
        lookups = self.lookup(eligible_lead(
            preferredDevelopments=["dev-one", "dev-other", "dev-one"]
        ))
        self.assertEqual(
            [item["development_id"] for item in lookups],
            ["dev-one", "dev-other"],
        )
        for preferred in ([], ["missing"], ["dev-unnamed"]):
            self.assertEqual(self.lookup(eligible_lead(
                preferredDevelopments=preferred
            )), [])

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_tier_one_only_blocks_its_own_development(self):
        lead = eligible_lead(
            preferredDevelopments=["dev-one", "dev-troika"]
        )
        one_match = {"_id": "listing-1", "development": "dev-one"}
        lookups = self.lookup(
            lead, [one_match], lambda _lead, _listing: 1
        )
        self.assertEqual(
            [item["development_id"] for item in lookups], ["dev-troika"]
        )
        both_matches = [
            one_match,
            {"_id": "listing-2", "development": ["dev-troika"]},
        ]
        self.assertEqual(
            self.lookup(lead, both_matches, lambda _lead, _listing: 1), []
        )

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_budget_and_bedrooms_must_be_valid_numbers(self):
        for budget in (None, 0, -1, True, "15000"):
            self.assertEqual(self.lookup(eligible_lead(budgetRent=budget)), [])
        for bedrooms in (None, -1, True, "3"):
            self.assertEqual(self.lookup(eligible_lead(bedroomsMin=bedrooms)), [])
        self.assertTrue(self.lookup(eligible_lead(bedroomsMin=0)))


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

    def test_dynamic_development_name_is_sent_to_scraper(self):
        lookup = dict(self.lookup, development="The Troika",
                      development_id="dev-troika")
        body = self.body([])
        body["development"] = "The Troika"
        post = MagicMock(return_value=self.response(body))

        self.assertEqual(external_scraper.fetch_external_matches(
            lookup, request_post=post
        ), [])
        self.assertEqual(
            post.call_args.kwargs["json"]["development"], "The Troika"
        )

    def test_response_for_different_development_is_rejected(self):
        body = self.body([])
        body["development"] = "The Troika"
        post = MagicMock(return_value=self.response(body))

        self.assertIsNone(external_scraper.fetch_external_matches(
            self.lookup, request_post=post
        ))

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

    def cached_listing(self, listing_id="listing-1", **updates):
        item = {
            "_id": listing_id, "scraped?": True, "source": "iproperty",
            "sourceListingID": f"external-{listing_id}",
            "sourceURL": f"https://www.iproperty.com.my/listing/{listing_id}",
            "scraped_last_date": external_scraper._iso(
                external_scraper._utcnow() - datetime.timedelta(days=1)
            ),
            "development": "dev-one", "beds": 3, "priceRent": 15000,
            "TransactionType": ["Rent/Let"],
        }
        item.update(updates)
        return item

    def test_fresh_listing_selection_filters_and_limits_without_refreshing(self):
        now = external_scraper._utcnow()
        fresh_date = external_scraper._iso(now - datetime.timedelta(days=1))
        fresh = [self.cached_listing(
            f"listing-{index}", scraped_last_date=fresh_date
        ) for index in range(4)]
        rejected = [
            self.cached_listing("stale", scraped_last_date=external_scraper._iso(
                now - datetime.timedelta(days=8))),
            self.cached_listing("missing-date", scraped_last_date=None),
            self.cached_listing("invalid-date", scraped_last_date="not-a-date"),
            self.cached_listing("expensive", priceRent=15001),
            self.cached_listing("small", beds=2),
            self.cached_listing("wrong-development", development="dev-other"),
            self.cached_listing("sale", TransactionType=["Buy/Sell"]),
            self.cached_listing("bad-url", sourceURL="https://example.com/listing"),
        ]
        self.app._bubble_records.return_value = fresh + rejected

        result = external_scraper.get_fresh_external_listings(
            self.lookup, now=now
        )

        self.assertEqual(len(result), 3)
        self.assertTrue(all(item in fresh for item in result))
        self.app._bubble_patch.assert_not_called()
        constraints = self.app._bubble_records.call_args.args[2]
        self.assertIn({"key": "development", "constraint_type": "equals",
                       "value": "dev-one"}, constraints)

    def test_max_age_defaults_for_missing_or_invalid_values(self):
        for value in (None, "invalid", "0", "-2"):
            environment = {} if value is None else {
                "EXTERNAL_SCRAPER_MAX_AGE_DAYS": value
            }
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(
                    external_scraper.external_scraper_max_age_days(), 7
                )
        with patch.dict(os.environ, {"EXTERNAL_SCRAPER_MAX_AGE_DAYS": "3"},
                        clear=True):
            self.assertEqual(external_scraper.external_scraper_max_age_days(), 3)

    def test_worker_reuses_partial_fresh_results_without_scraping_or_upserting(self):
        job = {
            "_id": "job-1", "attempts": 1, "phone": "60123456789",
            "payload": __import__("json").dumps(self.lookup),
        }
        lead = dict(eligible_lead(), _id="lead-1")
        listing = self.cached_listing(priceRent=10000)
        self.app._bubble_records_by_ids.return_value = ([lead], 1)
        send = MagicMock(return_value=["wamid.cached"])
        with patch.object(external_scraper, "get_fresh_external_listings",
                          return_value=[listing]), \
             patch.object(external_scraper, "fetch_external_matches") as fetch, \
             patch.object(external_scraper, "upsert_external_listing") as upsert, \
             patch.object(external_scraper, "create_external_match",
                          return_value=("match-1", True)) as create_match:
            result = external_scraper.process_external_job(
                job, send_text=send, save_message=MagicMock()
            )

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["listings"][0]["bubble_listing_id"], "listing-1")
        fetch.assert_not_called()
        upsert.assert_not_called()
        self.assertTrue(create_match.call_args.kwargs["prequalified"])
        self.assertIn(listing["sourceURL"], send.call_args.args[1])

    def test_no_fresh_listing_uses_original_fetch_and_upsert_path(self):
        job = {
            "_id": "job-1", "attempts": 1, "phone": "60123456789",
            "notificationSent": True,
            "payload": __import__("json").dumps(self.lookup),
        }
        lead = dict(eligible_lead(), _id="lead-1")
        persisted_listing = self.cached_listing()
        self.app._bubble_records_by_ids.return_value = ([lead], 1)
        with patch.object(external_scraper, "get_fresh_external_listings",
                          return_value=[]), \
             patch.object(external_scraper, "fetch_external_matches",
                          return_value=[self.item]) as fetch, \
             patch.object(external_scraper, "upsert_external_listing",
                          return_value=(persisted_listing, False)) as upsert, \
             patch.object(external_scraper, "create_external_match",
                          return_value=("match-1", False)):
            result = external_scraper.process_external_job(
                job, send_text=MagicMock(), save_message=MagicMock()
            )

        self.assertEqual(result["status"], "completed")
        fetch.assert_called_once()
        upsert.assert_called_once()

    def test_external_listing_creation_maps_only_supported_fields(self):
        self.app._bubble_records.return_value = []
        self.app._bubble_create.return_value = "listing-1"
        listing, created = external_scraper.upsert_external_listing(
            self.item, self.lookup
        )
        self.assertTrue(created)
        self.assertEqual(listing["_id"], "listing-1")
        payload = self.app._bubble_create.call_args.args[2]
        self.assertIs(payload["scraped?"], True)
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

    def test_external_listing_deduplicates_when_job_is_retried(self):
        self.app._bubble_records.side_effect = [
            [], [{"_id": "listing-1", "owner": "user-manual"}],
        ]
        self.app._bubble_create.return_value = "listing-1"

        first_listing, first_created = external_scraper.upsert_external_listing(
            self.item, self.lookup
        )
        retry_listing, retry_created = external_scraper.upsert_external_listing(
            self.item, self.lookup
        )

        self.assertEqual(first_listing["_id"], retry_listing["_id"])
        self.assertTrue(first_created)
        self.assertFalse(retry_created)
        self.app._bubble_create.assert_called_once()

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

    def test_existing_external_match_prevents_duplicate_creation(self):
        self.app._bubble_records.return_value = [{"_id": "match-existing"}]
        lead = {
            "_id": "lead-1", "TransactionType": ["Rent/Let"],
            "budgetRent": 15000, "bedroomsMin": 3,
            "preferredDevelopments": ["dev-one"],
        }
        listing = {
            "_id": "listing-1", "development": "dev-one",
            "TransactionType": ["Rent/Let"], "priceRent": 18000, "beds": 3,
        }

        result = external_scraper.create_external_match(lead, listing)

        self.assertEqual(result, ("match-existing", False))
        self.app._bubble_create.assert_not_called()

    def test_prequalified_fresh_match_accepts_any_positive_price_within_budget(self):
        self.app._bubble_records.return_value = []
        self.app._bubble_create.return_value = "match-fresh"
        lead = dict(eligible_lead(), _id="lead-1")
        listing = self.cached_listing(priceRent=10000)

        result = external_scraper.create_external_match(
            lead, listing, prequalified=True
        )

        self.assertEqual(result, ("match-fresh", True))

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_enqueue_deduplicates_same_lead_and_development(self):
        existing = {
            "_id": "job-existing",
            "jobKey": "iproperty:lead-1:dev-one",
        }
        self.app._bubble_records.return_value = [existing]
        result = external_scraper.enqueue_external_lookup(
            self.lookup, "60123456789"
        )
        self.assertEqual(result, ("job-existing", False))
        self.app._bubble_create.assert_not_called()

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_enqueue_allows_different_development_for_same_lead(self):
        self.app._bubble_records.return_value = []
        self.app._bubble_create.return_value = "job-two"
        lookup = dict(
            self.lookup, development_id="dev-troika", development="The Troika"
        )

        result = external_scraper.enqueue_external_lookup(
            lookup, "60123456789"
        )

        self.assertEqual(result, ("job-two", True))
        payload = self.app._bubble_create.call_args.args[2]
        self.assertEqual(payload["jobKey"], "iproperty:lead-1:dev-troika")

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_legacy_job_key_deduplicates_its_payload_development(self):
        for status in ("Queued", "Running", "Completed"):
            with self.subTest(status=status):
                legacy = {
                    "_id": "job-legacy", "jobKey": "iproperty:lead-1",
                    "payload": __import__("json").dumps(self.lookup),
                    "status": status,
                    "notificationSent": status == "Completed",
                }
                self.app._bubble_records.side_effect = [[], [legacy]]

                result = external_scraper.enqueue_external_lookup(
                    self.lookup, "60123456789"
                )

                self.assertEqual(result, ("job-legacy", False))
        self.app._bubble_create.assert_not_called()

    @patch.dict(os.environ, {"EXTERNAL_SCRAPER_ENABLED": "true"})
    def test_legacy_job_for_other_development_does_not_block(self):
        legacy = {
            "_id": "job-legacy", "jobKey": "iproperty:lead-1",
            "payload": __import__("json").dumps(dict(
                self.lookup, development_id="dev-troika",
                development="The Troika",
            )),
        }
        self.app._bubble_records.side_effect = [[], [legacy]]
        self.app._bubble_create.return_value = "job-one"

        result = external_scraper.enqueue_external_lookup(
            self.lookup, "60123456789"
        )

        self.assertEqual(result, ("job-one", True))
        payload = self.app._bubble_create.call_args.args[2]
        self.assertEqual(payload["jobKey"], "iproperty:lead-1:dev-one")

    def test_followup_uses_only_original_listing_links(self):
        second_url = "https://www.iproperty.com.my/listing/external-2"
        message = external_scraper.format_external_matches("One Menerung", [
            dict(self.item, bubble_listing_id="listing-1"),
            dict(self.item, source_listing_id="external-2",
                 source_url=second_url, bubble_listing_id="listing-2"),
        ])
        self.assertIn(self.item["source_url"], message)
        self.assertIn(second_url, message)
        self.assertNotIn("View on Rentee", message)
        self.assertNotIn("https://www.rentee.asia/listing/", message)
        self.assertNotIn("Original iProperty listing:", message)
        self.assertIn("RM18,000/month · 3 bed · 3,282 sqft", message)
        self.assertIn("Agent: Agent External", message)
        self.assertIn("Availability has not been verified by Rentee", message)

    def test_worker_passes_lead_record_dictionary_to_match_creation(self):
        job = {
            "_id": "job-1", "attempts": 1, "phone": "60123456789",
            "notificationSent": True,
            "payload": __import__("json").dumps(self.lookup),
        }
        lead = {
            "_id": "lead-1", "TransactionType": ["Rent/Let"],
            "budgetRent": 30000, "bedroomsMin": 3,
            "preferredDevelopments": ["dev-one"],
        }
        listing = {
            "_id": "listing-1", "development": "dev-one",
            "TransactionType": ["Rent/Let"], "priceRent": 30000, "beds": 3,
        }
        self.app._bubble_records_by_ids.return_value = ([lead], 1)

        with patch.object(external_scraper, "fetch_external_matches",
                          return_value=[self.item]), \
             patch.object(external_scraper, "upsert_external_listing",
                          return_value=(listing, False)), \
             patch.object(external_scraper, "create_external_match",
                          return_value=("match-1", False)) as create_match:
            result = external_scraper.process_external_job(
                job, send_text=MagicMock(), save_message=MagicMock()
            )

        self.assertEqual(result["status"], "completed")
        passed_lead = create_match.call_args.args[0]
        self.assertIs(passed_lead, lead)
        self.assertIsInstance(passed_lead, dict)

    def test_worker_handles_missing_lead_records(self):
        job = {
            "_id": "job-1", "attempts": 1, "phone": "60123456789",
            "payload": __import__("json").dumps(self.lookup),
        }
        self.app._bubble_records_by_ids.return_value = ([], 1)

        with patch.object(external_scraper, "fetch_external_matches") as fetch:
            result = external_scraper.process_external_job(
                job, send_text=MagicMock(), save_message=MagicMock()
            )

        self.assertEqual(result["status"], "retry")
        fetch.assert_not_called()
        failure = self.app._bubble_patch.call_args.args[1]
        self.assertEqual(failure["status"], "Retry")
        self.assertEqual(failure["lastError"],
                         "ValueError: Lead no longer exists")

    def test_partial_persistence_failure_retries_without_sending(self):
        job = {
            "_id": "job-1", "attempts": 1, "phone": "60123456789",
            "payload": __import__("json").dumps(self.lookup),
        }
        self.app._bubble_records_by_ids.return_value = ([{
            "_id": "lead-1", "owner": "lead-owner",
            "TransactionType": ["Rent/Let"], "budgetRent": 15000,
            "bedroomsMin": 3, "preferredDevelopments": ["dev-one"],
        }], 1)
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
        self.app._bubble_records_by_ids.return_value = ([lead], 1)
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

    def test_uncertain_prior_notification_stops_without_resending(self):
        job = {
            "_id": "job-1", "attempts": 2, "phone": "60123456789",
            "notificationSent": False, "notificationState": "Sending",
            "payload": __import__("json").dumps(self.lookup),
        }
        self.app._bubble_records_by_ids.return_value = ([{
            "_id": "lead-1", "TransactionType": ["Rent/Let"],
            "budgetRent": 15000, "bedroomsMin": 3,
            "preferredDevelopments": ["dev-one"],
        }], 1)
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
        self.assertEqual(result["status"], "notification_uncertain")
        send.assert_not_called()
        self.assertEqual(
            self.app._bubble_patch.call_args.args[1]["status"],
            "NotificationUncertain",
        )

    def test_claim_rejects_job_when_readback_token_was_overwritten(self):
        now = external_scraper._iso(external_scraper._utcnow())
        queued = {
            "_id": "job-1", "jobKey": "iproperty:lead-1",
            "status": "Queued", "attempts": 0, "availableAt": now,
        }
        self.app._bubble_records.side_effect = [
            [queued], [dict(queued, status="Running", workerId="other-worker")],
        ]
        self.assertIsNone(external_scraper.claim_external_job())


if __name__ == "__main__":
    unittest.main()
