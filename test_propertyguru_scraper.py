import json
import unittest
from unittest.mock import MagicMock, patch

import requests

from propertyguru_scraper import (
    AccessBlockedError,
    BubbleClient,
    IPropertyClient,
    NormalizedListing,
    SafetyError,
    ScraperError,
    assert_development_endpoint,
    bubble_payload,
    discover_pagination_url,
    parse_search_results,
)


class IPropertyParserTests(unittest.TestCase):
    @patch("propertyguru_scraper.sync_playwright")
    def test_one_browser_context_is_reused_and_closed(self, mocked_sync_playwright):
        manager = MagicMock()
        playwright = MagicMock()
        browser = MagicMock()
        context = MagicMock()
        page = MagicMock()
        mocked_sync_playwright.return_value = manager
        manager.start.return_value = playwright
        playwright.chromium.launch.return_value = browser
        browser.new_context.return_value = context
        context.new_page.return_value = page

        with patch.dict("propertyguru_scraper.os.environ", {"DISPLAY": ":99"}):
            with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                with IPropertyClient() as client:
                    self.assertIs(client.page, page)

        self.assertIn("Browser mode: headed", logs.output[0])
        self.assertIn("DISPLAY: :99", logs.output[1])
        self.assertIn(
            "Launching headed Chromium under virtual display...", logs.output[2]
        )

        manager.start.assert_called_once_with()
        playwright.chromium.launch.assert_called_once_with(headless=False)
        browser.new_context.assert_called_once()
        context.new_page.assert_called_once_with()
        page.close.assert_called_once_with()
        context.close.assert_called_once_with()
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()

    def test_playwright_access_control_detection(self):
        with self.assertRaisesRegex(Exception, "access-control page"):
            IPropertyClient._detect_access_control(
                "<html>Bot Protection</html>", "Bot Protection", "https://example.test"
            )

    def search_html(self, listings):
        data = {"props": {"pageProps": {"pageData": {
            "searchParams": {
                "listingType": "rent", "isCommercial": False,
                "_freetextDisplay": "one-menerung", "page": 1,
            },
            "data": {"listingsData": [
                {
                    "listingData": listing,
                    "segment": {"parameters": {"metaData": {"listingData": {
                        "projectNanoId": "fd12m8"
                    }}}},
                } for listing in listings
            ]}
        }}}}
        return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'

    def listing_record(self):
        return {
            "id": 501195195,
            # iProperty sometimes exposes a different internal/cross-listing ID;
            # sourceListingID must come from the public /rent-<id>/ URL.
            "externalId": "37261895",
            "typeCode": "RENT",
            "url": "/property/bangsar/one-menerung/rent-501195195/",
            "localizedTitle": "One Menerung, Bukit Bandaraya, Bangsar",
            "fullAddress": "Jalan Menerung, Bukit Bandaraya, Bangsar, Kuala Lumpur",
            "property": {"subTypeText": "Condominium"},
            "price": {"value": 15000, "pretty": "RM 15,000 /mo"},
            "bedrooms": 3,
            "bathrooms": 4,
            "floorArea": 3400,
            "availabilityInfo": "Available from 1 Sep 2026",
            "recency": {"text": "Listed today"},
            "listingFeatures": [
                [{"dataAutomationId": "listing-card-v2-bedrooms", "text": "3+1"}],
                {"dataAutomationId": "listing-card-v2-area", "text": "3,400 sqft"},
                {"dataAutomationId": "listing-card-v2-unit-type", "text": "Condominium"},
                {"dataAutomationId": "listing-card-v2-furnish", "text": "Fully Furnished"},
            ],
            "badges": [{"text": "Corner Lot"}],
            "mediaCarousel": {"previewMedia": {"images": {"items": [
                {"src": "https://ipp1-cdn.pgimgs.com/listing/501195195/UPHO.111.V800/one.jpg"},
                {"src": "https://ipp1-cdn.pgimgs.com/listing/501195195/UPHO.222.V800/two.jpg"},
                {"src": "https://ipp1-cdn.pgimgs.com/listing/501195195/UPHO.111.V550/one.jpg"},
                {"src": "https://ipp1-cdn.pgimgs.com/projectnet-project/1533/ZPPHO.99.V800/project.jpg"},
            ]}}},
        }

    def test_limit_one_loads_only_the_search_page(self):
        search_url = "https://www.iproperty.com.my/property-for-rent/p/one-menerung"
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = search_url
        client.get = MagicMock(return_value=self.search_html([self.listing_record()]))

        items = client.discover_listings(search_url, max_pages=10, limit=1)

        self.assertEqual(len(items), 1)
        client.get.assert_called_once_with(search_url, 'a[href*="/rent-"]')
        client.page.get_by_role.assert_not_called()

    def test_later_blocked_page_preserves_first_page_results(self):
        search_url = "https://www.iproperty.com.my/property-for-rent/p/one-menerung"
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = search_url
        client.get = MagicMock(side_effect=[
            self.search_html([self.listing_record()]),
            AccessBlockedError("blocked"),
        ])

        with self.assertLogs("iproperty_scraper", level="WARNING") as logs:
            items = client.discover_listings(search_url, max_pages=10)

        self.assertEqual([item.source_listing_id for item in items], ["501195195"])
        page_two_url = (
            "https://www.iproperty.com.my/property-for-rent/2?"
            "isCommercial=false&_freetextDisplay=One+Menerung&propertyId=fd12m8"
        )
        self.assertEqual(client.get.call_args_list[1].args[0], page_two_url)
        wait_ms = client.page.wait_for_timeout.call_args.args[0]
        self.assertGreaterEqual(wait_ms, 8000)
        self.assertLessEqual(wait_ms, 15000)
        self.assertIn("blocked page 2", " ".join(logs.output))
        self.assertIn("continuing with 1 listings", " ".join(logs.output))

    @patch("propertyguru_scraper.random.uniform", return_value=2.7)
    def test_later_page_settles_after_dom_loaded(self, _mocked_uniform):
        client = IPropertyClient(delay=0)
        client.page = MagicMock()
        client.page.url = "https://www.iproperty.com.my/property-for-rent/2"
        client.page.title.return_value = "One Menerung"
        client.page.content.return_value = "<html>results</html>"
        client.page.goto.return_value.status = 200

        html = client.get(
            client.page.url,
            'a[href*="/rent-"]',
            pagination_page_number=2,
        )

        self.assertEqual(html, "<html>results</html>")
        client.page.wait_for_timeout.assert_called_once_with(2700.0)

    def test_distinct_pagination_href_is_preferred(self):
        current = "https://www.iproperty.com.my/property-for-rent/p/one-menerung"
        href = (
            "https://www.iproperty.com.my/property-for-rent/2?"
            "isCommercial=false&_freetextDisplay=One+Menerung&propertyId=dynamic123"
        )
        html = f'<a title="Page 2" href="{href}">2</a>'
        self.assertEqual(discover_pagination_url(html, current, 2, "One Menerung"), href)

    def test_search_state_parses_fields_and_all_listing_photos(self):
        items = parse_search_results(
            self.search_html([self.listing_record()]),
            "https://www.iproperty.com.my/property-for-rent/p/one-menerung",
            "One Menerung",
        )
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.source_listing_id, "501195195")
        self.assertEqual(item.price_rent, 15000)
        self.assertEqual(item.beds, 3)
        self.assertEqual(item.baths, 4)
        self.assertEqual(item.sq_ft, 3400)
        self.assertEqual(item.furnished, "Yes")
        self.assertIsNone(item.balcony)
        self.assertIsNone(item.maid_room)
        self.assertEqual(len(item.photo_urls), 2)
        self.assertIn("RM 15,000 /mo", item.description)
        self.assertIn("Fully Furnished", item.description)
        self.assertTrue(item.availability.startswith("2026-09-01"))

    def test_search_state_rejects_villa_and_bungalow_results(self):
        villa = self.listing_record()
        villa.update({"id": 106811324, "externalId": "106811324",
                      "url": "/property/bangsar/one-menerung/rent-106811324/",
                      "fullAddress": "One Menerung Villa, Bangsar",
                      "property": {"subTypeText": "Twin Villas"}})
        bungalow = self.listing_record()
        bungalow.update({"id": 106860844, "externalId": "106860844",
                         "url": "/property/bangsar/one-menerung/rent-106860844/",
                         "property": {"subTypeText": "Bungalow House"}})
        items = parse_search_results(
            self.search_html([villa, bungalow]),
            "https://www.iproperty.com.my/property-for-rent/p/one-menerung",
            "One Menerung",
        )
        self.assertEqual(items, [])

    def test_payload_types_and_safety(self):
        payload = bubble_payload(
            NormalizedListing(
                "12345678", "https://example.test",
                balcony="Yes", keyfacts="Condominium; Fully Furnished",
            ),
            "condo-id",
        )
        self.assertIs(payload["scraped?"], True)
        self.assertEqual(payload["balcony"], "Yes")
        self.assertEqual(payload["TransactionType"], ["Rent/Let"])
        self.assertEqual(payload["keyFacts"], "Condominium; Fully Furnished")
        self.assertNotIn("keyfacts", payload)
        with self.assertRaises(SafetyError):
            assert_development_endpoint("https://www.rentee.asia/api/1.1/")

    def test_bubble_error_includes_body_and_payload_keys_without_token(self):
        url = "https://www.rentee.asia/version-test/api/1.1/obj/listing"
        request = requests.Request(
            "POST",
            url,
            headers={"Authorization": "Bearer secret-token"},
            json={"priceRent": 25000, "TransactionType": ["Rent/Let"]},
        ).prepare()
        response = requests.Response()
        response.status_code = 400
        response.url = url
        response.request = request
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps({
            "statusCode": 400,
            "body": {"message": "Invalid data for field TransactionType"},
        }).encode()

        with self.assertRaises(ScraperError) as raised:
            BubbleClient("unused")._json(response)

        message = str(raised.exception)
        self.assertIn("Bubble API error 400", message)
        self.assertIn(f"POST {url}", message)
        self.assertIn("priceRent, TransactionType", message)
        self.assertIn("Invalid data for field TransactionType", message)
        self.assertNotIn("secret-token", message)


if __name__ == "__main__":
    unittest.main()
