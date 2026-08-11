import unittest

from propertyguru_scraper import (
    NormalizedListing,
    SafetyError,
    assert_development_endpoint,
    bubble_payload,
    parse_listing,
)


class PropertyGuruParserTests(unittest.TestCase):
    def test_structured_listing_and_photo_order(self):
        html = '''
        <html><head>
          <script type="application/ld+json">{
            "@type":"Residence","name":"One Menerung",
            "description":"A full listing description with a balcony and 1 maid room.",
            "price":"15000","bedrooms":"3+1","bathrooms":4,
            "floorSize":{"value":"3,400 sqft"},
            "images":[
              "https://media.example/property/one-original.jpg",
              "https://media.example/property/two-original.jpg",
              "https://media.example/agent/profile.jpg"
            ]
          }</script>
        </head><body><main><h1>One Menerung</h1>
          <p>RM 15,000 /mo</p><p>Fully furnished</p>
          <p>Listing ID - 501195195</p><p>Available from 1 Sep 2026</p>
        </main></body></html>'''
        item = parse_listing(
            html,
            "https://www.propertyguru.com.my/property-listing/one-menerung-for-rent-501195195",
            "One Menerung",
        )
        self.assertEqual(item.price_rent, 15000)
        self.assertEqual(item.beds, 3)
        self.assertEqual(item.baths, 4)
        self.assertEqual(item.sq_ft, 3400)
        self.assertEqual(item.furnished, "Yes")
        self.assertEqual(item.balcony, "Yes")
        self.assertEqual(item.maid_room, 1)
        self.assertEqual(len(item.photo_urls), 2)
        self.assertTrue(item.availability.startswith("2026-09-01"))

    def test_visible_fallback_and_unknown_features_stay_empty(self):
        html = '''<html><body><main><h1>One Menerung</h1>
        <p>RM 19,888 /mo</p><p>3 Beds 4 Baths 3,272 sqft</p>
        <p>Unfurnished. Listing ID - 501063229</p>
        <meta property="og:image" content="https://media.example/unit/cover.webp">
        </main></body></html>'''
        item = parse_listing(
            html,
            "https://www.propertyguru.com.my/property-listing/one-menerung-for-rent-501063229",
            "One Menerung",
        )
        self.assertEqual((item.price_rent, item.beds, item.baths, item.sq_ft), (19888, 3, 4, 3272))
        self.assertEqual(item.furnished, "No")
        self.assertIsNone(item.balcony)

    def test_rejects_nearby_property(self):
        with self.assertRaisesRegex(Exception, "does not clearly identify"):
            parse_listing(
                "<h1>Nearby Bangsar Condo</h1><p>Listing ID 501111111</p>",
                "https://www.propertyguru.com.my/property-listing/nearby-501111111",
                "One Menerung",
            )

    def test_payload_types_and_safety(self):
        payload = bubble_payload(NormalizedListing("12345678", "https://example.test", balcony="Yes"), "condo-id")
        self.assertIs(payload["scraped?"], True)
        self.assertEqual(payload["balcony"], "Yes")
        self.assertEqual(payload["TransactionType"], ["Rent/Let"])
        with self.assertRaises(SafetyError):
            assert_development_endpoint("https://www.rentee.asia/api/1.1/")


if __name__ == "__main__":
    unittest.main()
