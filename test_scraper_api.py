import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import scraper_api
from propertyguru_scraper import (
    AmbiguousProjectError,
    CondoConfig,
    NormalizedListing,
    ProjectNotFoundError,
)


def condo(name, project_id=None, *, bubble_name=None):
    return CondoConfig(
        requested_name=name,
        bubble_name=bubble_name or name,
        iproperty_search_name=name,
        iproperty_project_name=name,
        iproperty_project_id=project_id,
        group=None,
        enabled=True,
    )


class ScraperApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.key_file = Path(self.temp_dir.name) / ".scraper_api_key"
        self.key_file.write_text("test-secret\n", encoding="utf-8")
        self.key_patch = patch.object(scraper_api, "API_KEY_FILE", self.key_file)
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        scraper_api.app.config.update(TESTING=True)
        self.client = scraper_api.app.test_client()

    def post(self, payload, key="test-secret"):
        headers = {"X-API-Key": key} if key is not None else {}
        return self.client.post(
            "/api/external-matches", json=payload, headers=headers
        )

    @staticmethod
    def listing(listing_id, price, beds, sq_ft=2000):
        return {
            "source_listing_id": listing_id,
            "source_url": f"https://www.iproperty.com.my/property/x/rent-{listing_id}/",
            "source_agent_name": "Agent",
            "price_rent": price,
            "beds": beds,
            "sq_ft": sq_ft,
            "description": "Rental listing",
        }

    def test_api_key_authentication_is_preserved(self):
        self.assertEqual(self.post({}, key=None).status_code, 401)
        self.assertEqual(self.post({}, key="wrong").status_code, 401)

    @patch("scraper_api.load_listings")
    def test_one_menerung_contract_filter_sort_and_cap(self, load_listings):
        load_listings.return_value = ("One Menerung", [
            self.listing("500000005", 30000, 5),
            self.listing("500000004", 29000, 5),
            self.listing("500000003", 31000, 6),
            self.listing("500000002", 28000, 5),
            self.listing("500000001", 24000, 5),
            self.listing("500000006", 36001, 5),
            self.listing("500000007", 30000, 4),
        ])

        response = self.post({
            "development": "One Menerung",
            "bedrooms_min": 5,
            "budget_rent": 30000,
        })
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["development"], "One Menerung")
        self.assertEqual(body["source"], "iproperty")
        self.assertEqual(body["total_matches"], 5)
        self.assertEqual(len(body["matches"]), 3)
        self.assertEqual(
            [row["source_listing_id"] for row in body["matches"]],
            ["500000005", "500000003", "500000004"],
        )
        self.assertEqual(set(body["matches"][0]), {
            "source_listing_id", "source_url", "agent", "price_rent",
            "beds", "sq_ft", "description",
        })

    @patch("scraper_api.IPropertyClient")
    @patch("scraper_api.load_condo_configs")
    def test_other_configured_development_reuses_project_id(self, configs, client_type):
        configs.return_value = [condo("The Troika", "troika-id")]
        client = MagicMock()
        client_type.return_value.__enter__.return_value = client
        client.discover_listings.return_value = [NormalizedListing(
            source_listing_id="500000001",
            source_url="https://www.iproperty.com.my/property/x/rent-500000001/",
        )]

        resolved, listings = scraper_api.load_listings("  the   troika ")

        self.assertEqual(resolved, "The Troika")
        self.assertEqual(listings[0]["source_listing_id"], "500000001")
        call = client.discover_listings.call_args.kwargs
        self.assertIn("propertyId=troika-id", call["search_url"])
        self.assertEqual(call["config"].requested_name, "The Troika")

    @patch("scraper_api.load_condo_configs", return_value=[])
    def test_unconfigured_development_is_dynamic_and_normalized(self, _configs):
        config = scraper_api.configured_development("Serai")
        self.assertEqual(config.iproperty_search_name, "Serai")
        self.assertIsNone(config.iproperty_project_id)
        self.assertEqual(
            scraper_api.normalize_project_name("  The-Troika! "),
            scraper_api.normalize_project_name("the troika"),
        )

    def test_unknown_and_ambiguous_developments_are_structured_errors(self):
        payload = {"development": "Unknown", "bedrooms_min": 2, "budget_rent": 5000}
        with patch("scraper_api.load_listings", side_effect=ProjectNotFoundError("not found")):
            response = self.post(payload)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json()["error"]["code"], "development_not_found")
        with patch("scraper_api.load_listings", side_effect=AmbiguousProjectError("ambiguous")):
            response = self.post(payload)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.get_json()["error"]["code"], "ambiguous_development")

    def test_scraper_failure_is_not_a_successful_empty_result(self):
        payload = {"development": "Serai", "bedrooms_min": 2, "budget_rent": 5000}
        with patch("scraper_api.load_listings", side_effect=RuntimeError("boom")):
            response = self.post(payload)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.get_json()["error"]["code"], "scraper_failure")

    def test_invalid_inputs_are_rejected(self):
        invalid_payloads = [
            None,
            {},
            {"development": "", "bedrooms_min": 2, "budget_rent": 5000},
            {"development": "!!!", "bedrooms_min": 2, "budget_rent": 5000},
            {"development": "Serai", "bedrooms_min": 2.5, "budget_rent": 5000},
            {"development": "Serai", "bedrooms_min": -1, "budget_rent": 5000},
            {"development": "Serai", "bedrooms_min": True, "budget_rent": 5000},
            {"development": "Serai", "bedrooms_min": 2, "budget_rent": 0},
            {"development": "Serai", "bedrooms_min": 2, "budget_rent": "NaN"},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.post(payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()["error"]["code"], "invalid_request")


if __name__ == "__main__":
    unittest.main()
