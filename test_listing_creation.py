import unittest
from unittest.mock import Mock
from pathlib import Path

import listing_creation as creation


BASE = "https://bubble.test/api/1.1"


class ListingCreationTests(unittest.TestCase):
    def setUp(self):
        self.create = Mock(return_value="listing-1")
        self.patch = Mock()
        self.listing = {}
        self.get = Mock(side_effect=lambda _url: dict(self.listing))
        self.condos = [{"_id": "condo-1", "name": "One Menerung"}]
        self.geos = [{"_id": "geo-1", "name": "Damansara Heights"}]
        self.records = Mock(side_effect=lambda _base, kind: iter(
            self.condos if kind == "condo" else self.geos
        ))

    def handle(self, text, conversation=None, stored_image_url=None):
        return creation.handle_listing_creation(
            text, conversation or {"_id": "conversation-1"}, "user-gwen", BASE,
            bubble_create=self.create, bubble_patch=self.patch,
            bubble_get=self.get, bubble_records=self.records,
            stored_image_url=stored_image_url,
        )

    def test_start_resolves_condo_and_captures_supplied_shorthand(self):
        result = self.handle(
            "New listing One Menerung 3+1 3200sf 12k FF available now"
        )
        payload = self.create.call_args.args[2]
        self.assertEqual(payload, {
            "owner": "user-gwen", "condo": "condo-1",
            "propertyType": "Condo", "beds": 3, "Sq Ft": 3200,
            "priceRent": 12000, "TransactionType": ["Rent/Let"],
            "Furnishing": "Fully Furnished", "availability": True,
            "availability_date": creation.datetime.date.today().isoformat(),
        })
        self.patch.assert_called_once_with(
            f"{BASE}/obj/conversation/conversation-1",
            {"ActiveSkill": "create_listing", "Listing": "listing-1"},
        )
        self.assertIn("Which unit is it?", result.response_text)

    def test_active_unit_and_price_correction_update_only_new_fields(self):
        self.listing = {
            "_id": "listing-1", "condo": "condo-1", "propertyType": "Condo",
            "beds": 3, "Sq Ft": 3200, "priceRent": 12000,
            "TransactionType": ["Rent/Let"], "Furnishing": "Fully Furnished",
            "availability": True,
        }
        conversation = {
            "_id": "conversation-1", "ActiveSkill": "create_listing",
            "Listing": "listing-1",
        }
        unit = self.handle("23-3", conversation)
        self.patch.assert_called_with(
            f"{BASE}/obj/listing/listing-1", {"unitNumber": "23-3"}
        )
        self.assertEqual(self.listing["priceRent"], 12000)
        self.assertEqual(unit.response_text, "Any photos for this one?")
        self.patch.reset_mock()
        corrected = self.handle("Actually make it 11.5k", conversation)
        self.patch.assert_called_once_with(
            f"{BASE}/obj/listing/listing-1", {"priceRent": 11500}
        )
        self.assertNotIn("Publish?", corrected.response_text)

    def test_geo_only_asks_property_type_and_bungalow_infers_landed(self):
        geo = self.handle("New listing in Damansara Heights")
        payload = self.create.call_args.args[2]
        self.assertEqual(payload["Geo"], "geo-1")
        self.assertNotIn("propertyType", payload)
        self.assertEqual(geo.response_text, "Is it a condo or landed property?")
        self.create.reset_mock()
        self.patch.reset_mock()
        landed = self.handle("New bungalow in Damansara Heights")
        self.assertEqual(self.create.call_args.args[2]["propertyType"], "Landed")
        self.assertIn("Which unit is it?", landed.response_text)

    def test_photo_appends_and_sets_first_cover_without_overwriting_existing(self):
        self.listing = {
            "_id": "listing-1", "condo": "condo-1", "propertyType": "Condo",
            "unitNumber": "23-3", "TransactionType": ["Rent/Let"],
            "priceRent": 12000, "photos": [],
        }
        conversation = {"_id": "conversation-1", "ActiveSkill": "create_listing",
                        "Listing": "listing-1"}
        result = self.handle("", conversation, "https://bubble.test/photo-1")
        self.patch.assert_called_once_with(f"{BASE}/obj/listing/listing-1", {
            "photos": ["https://bubble.test/photo-1"],
            "coverPhoto": "https://bubble.test/photo-1",
        })
        self.assertTrue(result.handled)
        self.assertIsNone(result.response_text)

    def test_exact_live_phrase_persists_beds_and_renders_summary(self):
        result = self.handle(
            "New listing one menerung, 16.3k rent per month, 3 beds"
        )
        payload = self.create.call_args.args[2]
        self.assertEqual(payload["beds"], 3)
        self.assertEqual(payload["priceRent"], 16300)
        self.assertIn("3 bed", result.response_text)

    def test_four_rapid_photos_are_all_saved_without_immediate_prompts(self):
        self.listing = {
            "_id": "listing-1", "condo": "condo-1", "propertyType": "Condo",
            "unitNumber": "A-25-2", "beds": 3,
            "TransactionType": ["Rent/Let"], "priceRent": 16300,
            "photos": [],
        }
        conversation = {"_id": "conversation-1", "ActiveSkill": "create_listing",
                        "Listing": "listing-1"}

        def apply_patch(url, updates):
            if url.endswith("/listing/listing-1"):
                self.listing.update(updates)

        self.patch.side_effect = apply_patch
        results = [
            self.handle("", conversation, f"https://bubble.test/photo-{index}")
            for index in range(1, 5)
        ]
        self.assertEqual(len(self.listing["photos"]), 4)
        self.assertEqual(self.listing["coverPhoto"], "https://bubble.test/photo-1")
        self.assertTrue(all(result.response_text is None for result in results))

    def test_no_photos_publish_and_cancel_clear_active_skill(self):
        self.listing = {
            "_id": "listing-1", "condo": "condo-1", "propertyType": "Condo",
            "unitNumber": "23-3", "TransactionType": ["Rent/Let"],
            "priceRent": 12000,
        }
        conversation = {"_id": "conversation-1", "ActiveSkill": "create_listing",
                        "Listing": "listing-1"}
        self.assertIn("Publish?", self.handle("No photos yet", conversation).response_text)
        self.patch.reset_mock()
        published = self.handle("yes", conversation)
        self.patch.assert_called_once_with(
            f"{BASE}/obj/conversation/conversation-1", {"ActiveSkill": ""}
        )
        self.assertTrue(published.published)
        self.patch.reset_mock()
        cancelled = self.handle("never mind", conversation)
        self.patch.assert_called_once_with(
            f"{BASE}/obj/conversation/conversation-1", {"ActiveSkill": ""}
        )
        self.assertTrue(cancelled.cancelled)

    def test_listing_creation_skill_is_loaded(self):
        instructions = Path("skills/listing_creation/SKILL.md").read_text()
        self.assertIn("# Listing Creation", instructions)
        self.assertIn("agents adding inventory", instructions)


if __name__ == "__main__":
    unittest.main()
