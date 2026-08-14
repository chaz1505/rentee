import json
import io
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import requests

from propertyguru_scraper import (
    AccessBlockedError,
    BUBBLE_BASE_URL,
    BUBBLE_LIVE_BASE_URL,
    BubbleClient,
    CondoConfig,
    IPropertyClient,
    IPropertyProject,
    ProjectCandidate,
    NormalizedListing,
    SafetyError,
    ScraperError,
    assert_development_endpoint,
    bubble_payload,
    build_parser,
    cache_project_identity,
    discover_pagination_url,
    iproperty_search_url,
    iproperty_freetext_search_url,
    iproperty_project_search_url,
    load_condo_config,
    load_condo_configs,
    normalize_furnishing,
    normalize_project_name,
    parse_search_results,
    process_condo,
    resolve_iproperty_project,
    run,
    select_batch_condos,
    select_missing_project_id_condos,
    slice_batch_condos,
)


class IPropertyParserTests(unittest.TestCase):
    def temporary_condo_config(self, entries):
        directory = tempfile.TemporaryDirectory()
        path = Path(directory.name) / "scraper_condos.json"
        path.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")
        self.addCleanup(directory.cleanup)
        return path

    def temporary_serai_config(self):
        return self.temporary_condo_config([{
            "requested_name": "Serai", "bubble_name": "Serai Bukit Bandaraya",
            "iproperty_search_name": "Serai", "iproperty_project_name": "Serai",
            "iproperty_project_id": "lj5z9k", "group": "Bangsar/Damansara Heights",
            "enabled": True,
        }])

    def bubble_response(self, method, status, body=b"", content_type=None):
        url = "https://www.rentee.asia/version-test/api/1.1/obj/listing/existing-id"
        request = requests.Request(
            method, url, json={"sourceListingID": "501195195"}
        ).prepare()
        response = requests.Response()
        response.status_code = status
        response.url = url
        response.request = request
        response._content = body
        if content_type:
            response.headers["Content-Type"] = content_type
        return response

    def condo_config(self, name="One Menerung"):
        return CondoConfig(
            requested_name=name,
            bubble_name=name,
            iproperty_search_name=name,
            iproperty_project_name=name,
            iproperty_project_id=None,
            group="Bangsar/Damansara Heights",
            enabled=True,
        )

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

        with IPropertyClient() as client:
            self.assertIs(client.page, page)

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

    def project_candidates_html(self, candidates):
        wrappers = []
        for index, (name, project_id, property_type, transaction_type) in enumerate(candidates):
            wrappers.append({
                "listingData": {
                    "id": 501000000 + index,
                    "typeCode": transaction_type,
                    "localizedTitle": f"{name}, Kuala Lumpur",
                    "property": {"subTypeText": property_type},
                    "url": f"/property/kuala-lumpur/project/rent-{501000000 + index}/",
                },
                "segment": {"parameters": {"metaData": {"listingData": {
                    "projectNanoId": project_id,
                }}}},
            })
        data = {"props": {"pageProps": {"pageData": {"data": {
            "listingsData": wrappers,
        }}}}}
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
            "advertiser": {
                "name": "Amy Yap",
                "agency": {"name": "RVT Realty"},
            },
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

        items = client.discover_listings(
            search_url, max_pages=10, config=self.condo_config(), limit=1
        )

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
            items = client.discover_listings(
                search_url, max_pages=10, config=self.condo_config()
            )

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

    def test_configured_project_id_builds_direct_filtered_url(self):
        cases = (
            ("One Menerung", "fd12m8"),
            ("Sri Penaga", "xzqc7y"),
            ("Ken Bangsar", "pcv9hj"),
            ("DC Residensi", "hxtifi"),
        )
        for name, project_id in cases:
            with self.subTest(name=name):
                self.assertEqual(
                    iproperty_project_search_url(name, project_id),
                    "https://www.iproperty.com.my/property-for-rent?"
                    f"isCommercial=false&_freetextDisplay={name.replace(' ', '+')}"
                    f"&propertyId={project_id}",
                )

    @patch("propertyguru_scraper.resolve_iproperty_project")
    def test_known_project_id_skips_discovery_and_parses_direct_page(self, mocked_resolve):
        config = CondoConfig(
            "One Menerung", "One Menerung", "One Menerung",
            "One Menerung", "fd12m8", "G", True,
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_project_search_url("One Menerung", "fd12m8")
        client.get = MagicMock(return_value=self.search_html([self.listing_record()]))

        items = client.discover_listings(client.page.url, 1, config, limit=1)

        self.assertEqual([item.source_listing_id for item in items], ["501195195"])
        mocked_resolve.assert_not_called()
        client.get.assert_called_once_with(
            client.page.url, "script#__NEXT_DATA__", content_timeout_ms=20000
        )

    @patch("propertyguru_scraper.resolve_iproperty_project")
    def test_known_project_id_missing_structured_data_has_specific_error(self, mocked_resolve):
        config = CondoConfig(
            "One Menerung", "One Menerung", "One Menerung",
            "One Menerung", "fd12m8", "G", True,
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_project_search_url("One Menerung", "fd12m8")
        client.get = MagicMock(return_value="<html><body>No search state</body></html>")

        with self.assertRaisesRegex(
            ScraperError,
            "project-filtered search page did not produce usable listing data.*fd12m8",
        ):
            client.discover_listings(client.page.url, 1, config)
        mocked_resolve.assert_not_called()

    @patch("propertyguru_scraper.resolve_iproperty_project")
    def test_known_project_id_empty_listing_state_has_specific_error(self, mocked_resolve):
        config = CondoConfig(
            "One Menerung", "One Menerung", "One Menerung",
            "One Menerung", "fd12m8", "G", True,
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_project_search_url("One Menerung", "fd12m8")
        client.get = MagicMock(return_value=self.search_html([]))

        with self.assertRaisesRegex(
            ScraperError,
            "project-filtered search page did not produce usable listing data.*fd12m8",
        ):
            client.discover_listings(client.page.url, 1, config)
        mocked_resolve.assert_not_called()

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
        self.assertEqual(item.furnishing, "Fully Furnished")
        self.assertEqual(item.source_agent_name, "Amy Yap")
        self.assertEqual(item.source_agency_name, "RVT Realty")
        payload = bubble_payload(item, "condo-id")
        self.assertEqual(payload["Furnishing"], "Fully Furnished")
        self.assertEqual(payload["sourceAgentName"], "Amy Yap")
        self.assertEqual(payload["sourceAgencyName"], "RVT Realty")
        self.assertIsNone(item.balcony)
        self.assertIsNone(item.maid_room)
        self.assertEqual(len(item.photo_urls), 2)
        self.assertIn("RM 15,000 /mo", item.description)
        self.assertIn("Fully Furnished", item.description)
        self.assertTrue(item.availability.startswith("2026-09-01"))

    def test_search_state_still_rejects_villa_and_bungalow_listings(self):
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

    def test_serai_project_is_resolved_without_source_code_alias(self):
        listing = self.listing_record()
        listing.update({
            "url": "/property/bangsar/serai/rent-501195195/",
            "localizedTitle": "Serai, Bukit Bandaraya, Bangsar",
            "fullAddress": "Serai, Bangsar, Kuala Lumpur",
        })
        html = self.search_html([listing])
        project = resolve_iproperty_project(
            html, iproperty_search_url("Serai"), "Serai"
        )
        items = parse_search_results(
            html, iproperty_search_url("Serai"), "Serai", project
        )
        self.assertEqual(
            iproperty_search_url("Serai"),
            "https://www.iproperty.com.my/property-for-rent/p/serai",
        )
        self.assertEqual(project.name, "Serai")
        self.assertEqual(project.project_id, "fd12m8")
        self.assertEqual(len(items), 1)

    def test_wrong_project_id_is_rejected(self):
        expected = IPropertyProject("Serai", "different-project", "Condominium")
        items = parse_search_results(
            self.search_html([self.listing_record()]),
            "https://www.iproperty.com.my/property-for-rent/p/serai",
            "Serai",
            expected,
        )
        self.assertEqual(items, [])

    def test_configured_project_id_overrides_name(self):
        html = self.search_html([self.listing_record()])
        project = resolve_iproperty_project(
            html,
            "https://www.iproperty.com.my/property-for-rent/p/wrong-name",
            "Wrong Name",
            configured_project_name="Also Wrong",
            configured_project_id="fd12m8",
        )
        self.assertEqual(project.name, "One Menerung")
        self.assertEqual(project.project_id, "fd12m8")

    def test_configured_project_name_matches_without_id(self):
        project = resolve_iproperty_project(
            self.search_html([self.listing_record()]),
            "https://www.iproperty.com.my/property-for-rent/p/search-text",
            "Search Text",
            configured_project_name="one menerung",
        )
        self.assertEqual(project.name, "One Menerung")

    def test_project_name_normalization_is_conservative_and_compact(self):
        self.assertEqual(normalize_project_name("iDamansara"), "idamansara")
        self.assertEqual(normalize_project_name("One KL"), "onekl")
        self.assertEqual(normalize_project_name("One-KL"), "onekl")
        self.assertEqual(normalize_project_name("Mont' Kiara"), "montkiara")
        self.assertEqual(normalize_project_name("Mont’Kiara"), "montkiara")
        self.assertNotEqual(
            normalize_project_name("Pavilion Residence"),
            normalize_project_name("Pavilion Suites"),
        )

    def test_unique_normalized_rent_candidate_accepts_bungalow(self):
        html = self.project_candidates_html([
            ("Idamansara", "df5yzl", "Bungalow House", "RENT"),
        ])
        project = resolve_iproperty_project(
            html, iproperty_search_url("iDamansara"), "iDamansara"
        )
        self.assertEqual(project, IPropertyProject("Idamansara", "df5yzl", "Bungalow House"))

    def test_normalized_match_is_logged_and_caches_canonical_identity(self):
        entries = [{
            "requested_name": "i Damansara", "bubble_name": "iDamansara",
            "iproperty_search_name": "i Damansara", "iproperty_project_name": None,
            "iproperty_project_id": None, "group": "Other", "enabled": True,
        }]
        path = self.temporary_condo_config(entries)
        config = load_condo_config("i Damansara", path)
        with self.assertLogs("iproperty_scraper", level="INFO") as logs:
            project = resolve_iproperty_project(
                self.project_candidates_html([
                    ("Idamansara", "df5yzl", "Bungalow House", "RENT"),
                ]),
                iproperty_search_url("i Damansara"),
                "i Damansara",
            )
        self.assertIn("Resolved project using relaxed name match", " ".join(logs.output))
        self.assertIn("normalized project name", " ".join(logs.output))
        self.assertTrue(cache_project_identity(config, project, path))
        saved = json.loads(path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["iproperty_project_name"], "Idamansara")
        self.assertEqual(saved["iproperty_project_id"], "df5yzl")

    def test_normalized_name_accepts_service_residence_and_condominium(self):
        for configured, candidate, project_type in (
            ("OneKL", "One KL", "Service Residence"),
            ("Mont' Kiara", "Mont’Kiara", "Condominium"),
        ):
            with self.subTest(project_type=project_type):
                project = resolve_iproperty_project(
                    self.project_candidates_html([
                        (candidate, f"id-{project_type}", project_type, "RENT"),
                    ]),
                    iproperty_search_url(configured),
                    configured,
                )
                self.assertEqual(project.name, candidate)

    def test_sale_only_normalized_candidate_is_rejected(self):
        with self.assertRaisesRegex(ScraperError, "Config not modified"):
            resolve_iproperty_project(
                self.project_candidates_html([
                    ("Idamansara", "df5yzl", "Bungalow House", "SALE"),
                ]),
                iproperty_search_url("iDamansara"),
                "iDamansara",
            )

    def test_multiple_normalized_rent_identities_are_ambiguous(self):
        with self.assertRaisesRegex(ScraperError, "Config not modified"):
            resolve_iproperty_project(
                self.project_candidates_html([
                    ("One KL", "first", "Condominium", "RENT"),
                    ("One-KL", "second", "Service Residence", "RENT"),
                ]),
                iproperty_search_url("OneKL"),
                "OneKL",
            )

    def test_broad_substring_project_name_is_not_accepted(self):
        with self.assertRaisesRegex(ScraperError, "Config not modified"):
            resolve_iproperty_project(
                self.project_candidates_html([
                    ("Pavilion Residence", "one", "Condominium", "RENT"),
                    ("Pavilion Suites", "two", "Service Residence", "RENT"),
                ]),
                iproperty_search_url("Pavilion"),
                "Pavilion",
            )

    def test_real_relaxed_project_name_cases_resolve(self):
        cases = (
            ("iDamansara", "Idamansara", "df5yzl"),
            ("Residensi 22 Mont Kiara", "Residensi 22", "pcdy51"),
            ("28 Mont Kiara", "28 Mont Kiara @ MK28", "iymabk"),
            ("10 Mont Kiara", "10 Mont Kiara @ MK10", "06tzry"),
            ("11 Mont Kiara", "11 Mont Kiara @ MK11", "9s4gis"),
            ("KaMi Mont Kiara", "Kami", "qinh82"),
            ("Tiffani Mont Kiara", "Tiffani Kiara", "jjtrko"),
            ("Allevia Mont Kiara", "Allevia", "s49us1"),
            ("Binjai on the Park", "The Binjai on the Park", "v47506"),
            ("The Ritz Carlton Residence", "The Ritz-Carlton Residences", "zltrst"),
        )
        for configured, canonical, project_id in cases:
            with self.subTest(configured=configured):
                project = resolve_iproperty_project(
                    self.project_candidates_html([
                        (canonical, project_id, "Apartment", "RENT"),
                    ]),
                    iproperty_search_url(configured),
                    configured,
                )
                self.assertEqual(project.name, canonical)
                self.assertEqual(project.project_id, project_id)

    def test_duplicate_rent_rows_with_same_project_id_are_one_project(self):
        project = resolve_iproperty_project(
            self.project_candidates_html([
                ("Kami", "qinh82", "Condominium", "RENT"),
                ("Kami", "qinh82", "Service Residence", "RENT"),
            ]),
            iproperty_search_url("KaMi Mont Kiara"),
            "KaMi Mont Kiara",
        )
        self.assertEqual(project.project_id, "qinh82")

    def test_multiple_icon_project_ids_remain_ambiguous(self):
        with self.assertRaisesRegex(ScraperError, "multiple plausible project IDs remain"):
            resolve_iproperty_project(
                self.project_candidates_html([
                    ("The Icon Residence", "icon-one", "Condominium", "RENT"),
                    ("Icon City", "icon-two", "Apartment", "RENT"),
                ]),
                iproperty_search_url("The Icon Residences"),
                "The Icon Residences",
            )

    def test_unique_candidate_without_meaningful_name_relationship_is_rejected(self):
        with self.assertRaisesRegex(
            ScraperError, "no candidate passed relaxed project-name matching"
        ):
            resolve_iproperty_project(
                self.project_candidates_html([
                    ("Completely Different", "different", "Villa", "RENT"),
                ]),
                iproperty_search_url("Tiffani Mont Kiara"),
                "Tiffani Mont Kiara",
            )

    def test_failed_resolution_reports_candidate_projects(self):
        with self.assertRaises(ScraperError) as raised:
            resolve_iproperty_project(
                self.search_html([self.listing_record()]),
                "https://www.iproperty.com.my/property-for-rent/p/serai",
                "Serai",
                configured_project_id="missing-id",
            )
        message = str(raised.exception)
        self.assertIn("Candidate projects found", message)
        self.assertIn("One Menerung | fd12m8 | Condominium | RENT | 1 listings", message)

    @patch("propertyguru_scraper.BubbleClient")
    @patch("propertyguru_scraper.IPropertyClient")
    def test_discovery_prints_candidates_and_never_constructs_bubble(
        self, mocked_client_class, mocked_bubble_class
    ):
        candidate = ProjectCandidate(
            "Serai", "serai-id", "Condominium", "RENT", 12,
            "https://www.iproperty.com.my/property/bangsar/serai/rent-501195195/",
        )
        client = mocked_client_class.return_value.__enter__.return_value
        client.discover_projects.return_value = (
            "https://www.iproperty.com.my/property-for-rent/p/serai", [candidate]
        )
        args = Namespace(
            condo=None, discover="Serai", dry_run=False, search_url=None,
            max_pages=10, limit=None, delay=1.5, timeout=30.0,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(args), 0)
        mocked_bubble_class.assert_not_called()
        self.assertIn("Serai", output.getvalue())
        self.assertIn("serai-id", output.getvalue())

    def test_nonexistent_bubble_condo_aborts(self):
        bubble = BubbleClient("unused")
        bubble.get_all = MagicMock(return_value=[])
        with self.assertRaisesRegex(ScraperError, "No exact Condo match"):
            bubble.find_condo("Clearly Nonexistent Condo")

    def test_condo_config_separates_one_menerung_and_serai_identities(self):
        one_menerung = load_condo_config("One Menerung")
        serai = load_condo_config("Serai", self.temporary_serai_config())
        self.assertEqual(one_menerung.bubble_name, "One Menerung")
        self.assertEqual(one_menerung.iproperty_search_name, "One Menerung")
        self.assertEqual(serai.bubble_name, "Serai Bukit Bandaraya")
        self.assertEqual(serai.iproperty_search_name, "Serai")
        self.assertEqual(serai.iproperty_project_id, "lj5z9k")

    def test_unambiguous_identity_is_cached_atomically_and_only_target_changes(self):
        entries = [
            {
                "requested_name": "One Menerung", "bubble_name": "One Menerung",
                "iproperty_search_name": "One Menerung",
                "iproperty_project_name": None, "iproperty_project_id": None,
                "group": "Bangsar/Damansara Heights", "enabled": True,
            },
            {
                "requested_name": "Serai", "bubble_name": "Serai Bukit Bandaraya",
                "iproperty_search_name": "Serai", "iproperty_project_name": "Serai",
                "iproperty_project_id": "lj5z9k", "group": "Bangsar/Damansara Heights",
                "enabled": True,
            },
        ]
        path = self.temporary_condo_config(entries)
        config = load_condo_config("One Menerung", path)
        self.assertTrue(cache_project_identity(
            config, IPropertyProject("One Menerung", "fd12m8", "Condominium"), path
        ))
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["iproperty_project_name"], "One Menerung")
        self.assertEqual(saved[0]["iproperty_project_id"], "fd12m8")
        for key in ("requested_name", "bubble_name", "iproperty_search_name", "group", "enabled"):
            self.assertEqual(saved[0][key], entries[0][key])
        self.assertEqual(saved[1], entries[1])
        self.assertEqual(config.iproperty_project_id, "fd12m8")
        self.assertEqual(config.iproperty_project_name, "One Menerung")

    def test_five_sequential_identity_saves_preserve_every_prior_save(self):
        entries = [{
            "requested_name": f"Condo {letter}",
            "bubble_name": f"Condo {letter}",
            "iproperty_search_name": f"Condo {letter}",
            "iproperty_project_name": None,
            "iproperty_project_id": None,
            "group": "Test",
            "enabled": True,
        } for letter in "ABCDE"]
        path = self.temporary_condo_config(entries)
        configs = {item.requested_name: item for item in load_condo_configs(path)}

        for letter in "ABCDE":
            name = f"Condo {letter}"
            self.assertTrue(cache_project_identity(
                configs[name], IPropertyProject(name, f"id{letter}", "Condominium"), path
            ))
            saved = {
                row["requested_name"]: row for row in json.loads(path.read_text(encoding="utf-8"))
            }
            for prior in "ABCDE"[: "ABCDE".index(letter) + 1]:
                self.assertEqual(saved[f"Condo {prior}"]["iproperty_project_id"], f"id{prior}")

        final = {
            row["requested_name"]: row for row in json.loads(path.read_text(encoding="utf-8"))
        }
        self.assertEqual(
            [final[f"Condo {letter}"]["iproperty_project_id"] for letter in "ABCDE"],
            [f"id{letter}" for letter in "ABCDE"],
        )

    def test_batch_style_saves_from_one_loaded_snapshot_all_persist(self):
        entries = [{
            "requested_name": f"Condo {letter}",
            "bubble_name": f"Condo {letter}",
            "iproperty_search_name": f"Condo {letter}",
            "iproperty_project_name": None,
            "iproperty_project_id": None,
            "group": "Test",
            "enabled": True,
        } for letter in "ABC"]
        path = self.temporary_condo_config(entries)
        configs = load_condo_configs(path)

        for letter, config in zip("ABC", configs):
            cache_project_identity(
                config, IPropertyProject(f"Canonical {letter}", f"id{letter}", "Apartment"), path
            )

        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(
            [(row["iproperty_project_name"], row["iproperty_project_id"]) for row in saved],
            [(f"Canonical {letter}", f"id{letter}") for letter in "ABC"],
        )

    def test_save_log_is_emitted_only_after_readback_verification(self):
        entries = [{
            "requested_name": "Condo A", "bubble_name": "Condo A",
            "iproperty_search_name": "Condo A", "iproperty_project_name": None,
            "iproperty_project_id": None, "group": "Test", "enabled": True,
        }]
        path = self.temporary_condo_config(entries)
        config = load_condo_config("Condo A", path)
        bubble = MagicMock()
        bubble.find_condo.return_value = ("condo-id", "name")
        client = MagicMock()

        def discover(_url, _pages, _config, _limit, callback, **_options):
            callback(IPropertyProject("Condo A", "idA", "Condominium"))
            return []

        client.discover_listings.side_effect = discover
        args = Namespace(
            search_url=None, max_pages=1, dry_run=True, condo_config_path=path,
        )
        with self.assertLogs("iproperty_scraper", level="INFO") as logs:
            from propertyguru_scraper import process_condo
            process_condo(config, args, bubble, client, 1)
        output = "\n".join(logs.output)
        self.assertIn(f"Saving project identity to:\nINFO:iproperty_scraper:{path.resolve()}", output)
        self.assertIn("Saved and verified:", output)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))[0]["iproperty_project_id"], "idA"
        )

    def test_existing_project_id_is_never_overwritten(self):
        entries = [{
            "requested_name": "Serai", "bubble_name": "Serai Bukit Bandaraya",
            "iproperty_search_name": "Serai", "iproperty_project_name": "Serai",
            "iproperty_project_id": "lj5z9k", "group": "Bangsar/Damansara Heights",
            "enabled": True,
        }]
        path = self.temporary_condo_config(entries)
        config = load_condo_config("Serai", path)
        before = path.read_bytes()
        self.assertFalse(cache_project_identity(
            config, IPropertyProject("Wrong", "wrong-id", "Condominium"), path
        ))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(config.iproperty_project_id, "lj5z9k")

    @patch("propertyguru_scraper.os.replace", side_effect=OSError("disk failure"))
    def test_atomic_write_failure_preserves_original_and_memory(self, _replace):
        entries = [{
            "requested_name": "One Menerung", "bubble_name": "One Menerung",
            "iproperty_search_name": "One Menerung", "iproperty_project_name": None,
            "iproperty_project_id": None, "group": "Bangsar/Damansara Heights",
            "enabled": True,
        }]
        path = self.temporary_condo_config(entries)
        before = path.read_bytes()
        config = load_condo_config("One Menerung", path)
        with self.assertRaisesRegex(ScraperError, "atomically save"):
            cache_project_identity(
                config, IPropertyProject("One Menerung", "fd12m8", "Condominium"), path
            )
        self.assertEqual(path.read_bytes(), before)
        self.assertIsNone(config.iproperty_project_id)
        self.assertIsNone(config.iproperty_project_name)

    def test_batch_selection_filters_enabled_group_and_preserves_order(self):
        configs = [
            self.condo_config("First"),
            CondoConfig("Disabled", "Disabled", "Disabled", None, None,
                        "Bangsar/Damansara Heights", False),
            self.condo_config("Second"),
            CondoConfig("Elsewhere", "Elsewhere", "Elsewhere", None, None,
                        "Mont Kiara", True),
        ]
        self.assertEqual(
            [item.requested_name for item in select_batch_condos(configs)],
            ["First", "Second", "Elsewhere"],
        )
        self.assertEqual(
            [item.requested_name for item in select_batch_condos(
                configs, "bangsar/DAMANSARA heights"
            )],
            ["First", "Second"],
        )

    def test_missing_project_id_filter_excludes_resolved_freetext_and_disabled(self):
        configs = [
            CondoConfig("Missing", "Missing", "Missing", None, None, "G", True),
            CondoConfig("Blank", "Blank", "Blank", None, "", "G", True),
            CondoConfig("Resolved", "Resolved", "Resolved", "Resolved", "id", "G", True),
            CondoConfig("Free", "Free", "Free", None, None, "G", True, "freetext"),
            CondoConfig("Disabled", "Disabled", "Disabled", None, None, "G", False),
        ]
        enabled = select_batch_condos(configs)
        self.assertEqual(
            [item.requested_name for item in select_missing_project_id_condos(enabled)],
            ["Missing", "Blank"],
        )

    def test_group_then_missing_filter_then_slice_order(self):
        configs = [
            CondoConfig("G Resolved", "G Resolved", "G Resolved", None, "id", "G", True),
            CondoConfig("G First", "G First", "G First", None, None, "G", True),
            CondoConfig("Other", "Other", "Other", None, None, "Other", True),
            CondoConfig("G Second", "G Second", "G Second", None, None, "G", True),
            CondoConfig("G Third", "G Third", "G Third", None, None, "G", True),
        ]
        group = select_batch_condos(configs, "G")
        unresolved = select_missing_project_id_condos(group)
        selected = slice_batch_condos(unresolved, start=2, count=2)
        self.assertEqual(
            [(position, item.requested_name) for position, item in selected],
            [(2, "G Second"), (3, "G Third")],
        )

    @patch("propertyguru_scraper.parse_search_results")
    def test_discovery_only_resolves_before_listing_parsing(self, mocked_parse):
        config = CondoConfig(
            "Condo A", "Condo A", "Condo A", None, None, "G", True,
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_search_url("Condo A")
        client.get = MagicMock(return_value=self.project_candidates_html([
            ("Condo A", "idA", "Condominium", "RENT"),
        ]))
        callback = MagicMock()

        items = client.discover_listings(
            client.page.url, 10, config, None, callback, discovery_only=True
        )

        self.assertEqual(items, [])
        callback.assert_called_once_with(IPropertyProject("Condo A", "idA", "Condominium"))
        mocked_parse.assert_not_called()
        client.get.assert_called_once()
        client.page.wait_for_timeout.assert_not_called()

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_missing_id_discovery_batch_filters_before_browser_and_caches(
        self, mocked_bubble_class, mocked_client_class
    ):
        entries = [
            {
                "requested_name": "Resolved", "bubble_name": "Resolved",
                "iproperty_search_name": "Resolved", "iproperty_project_name": "Resolved",
                "iproperty_project_id": "existing", "group": "G", "enabled": True,
            },
            {
                "requested_name": "Free", "bubble_name": "Free",
                "iproperty_search_name": "Free", "iproperty_project_name": None,
                "iproperty_project_id": None, "iproperty_search_mode": "freetext",
                "group": "G", "enabled": True,
            },
            {
                "requested_name": "Disabled", "bubble_name": "Disabled",
                "iproperty_search_name": "Disabled", "iproperty_project_name": None,
                "iproperty_project_id": None, "group": "G", "enabled": False,
            },
            {
                "requested_name": "Missing A", "bubble_name": "Missing A",
                "iproperty_search_name": "Missing A", "iproperty_project_name": None,
                "iproperty_project_id": None, "group": "G", "enabled": True,
            },
            {
                "requested_name": "Missing B", "bubble_name": "Missing B",
                "iproperty_search_name": "Missing B", "iproperty_project_name": None,
                "iproperty_project_id": None, "group": "G", "enabled": True,
            },
        ]
        path = self.temporary_condo_config(entries)
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.return_value = ("condo-id", "name")
        managers = [MagicMock(), MagicMock()]
        clients = [manager.__enter__.return_value for manager in managers]
        for letter, client in zip("AB", clients):
            def discover(_url, _pages, _config, _limit, callback, *,
                         discovery_only=False, letter=letter, **_options):
                self.assertTrue(discovery_only)
                callback(IPropertyProject(
                    f"Canonical {letter}", f"id{letter}", "Condominium"
                ))
                return []
            client.discover_listings.side_effect = discover
        mocked_client_class.side_effect = managers
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            missing_project_id_only=True, discover_only=True,
            start=1, count=2, dry_run=False, search_url=None, max_pages=10,
            limit=None, limit_per_condo=None, delay=1.5, timeout=30.0,
            condo_config_path=path,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )

        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with patch("propertyguru_scraper.time.sleep"):
                with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                    self.assertEqual(run(args), 0)

        self.assertEqual(mocked_client_class.call_count, 2)
        bubble.find_condo.assert_has_calls([call("Missing A"), call("Missing B")])
        bubble.find_listing.assert_not_called()
        bubble.write_listing.assert_not_called()
        saved = {row["requested_name"]: row for row in json.loads(path.read_text())}
        self.assertEqual(saved["Resolved"]["iproperty_project_id"], "existing")
        self.assertIsNone(saved["Free"]["iproperty_project_id"])
        self.assertEqual(saved["Missing A"]["iproperty_project_id"], "idA")
        self.assertEqual(saved["Missing B"]["iproperty_project_id"], "idB")
        output = "\n".join(logs.output)
        self.assertIn("Discovery batch selection:", output)
        self.assertIn("Enabled condos: 4", output)
        self.assertIn("Already resolved project IDs: 1", output)
        self.assertIn("Free-text condos excluded: 1", output)
        self.assertIn("Missing project IDs eligible: 2", output)
        self.assertIn("DISCOVERY BATCH COMPLETE", output)
        self.assertNotIn("Created:", output)
        self.assertNotIn("Updated:", output)

    def test_batch_slicing_uses_one_based_enabled_positions(self):
        configs = [self.condo_config(f"Condo {index}") for index in range(1, 26)]
        cases = (
            (1, 5, list(range(1, 6))),
            (6, 5, list(range(6, 11))),
            (11, 10, list(range(11, 21))),
            (None, 5, list(range(1, 6))),
            (6, None, list(range(6, 26))),
            (None, None, list(range(1, 26))),
        )
        for start, count, expected_positions in cases:
            with self.subTest(start=start, count=count):
                selected = slice_batch_condos(configs, start, count)
                self.assertEqual([position for position, _ in selected], expected_positions)
                self.assertEqual(
                    [config.requested_name for _, config in selected],
                    [f"Condo {position}" for position in expected_positions],
                )

    def test_disabled_and_group_filtering_happen_before_slicing(self):
        configs = [
            CondoConfig("A", "A", "A", None, None, "G1", True),
            CondoConfig("Disabled", "Disabled", "Disabled", None, None, "G1", False),
            CondoConfig("B", "B", "B", None, None, "G2", True),
            CondoConfig("C", "C", "C", None, None, "G1", True),
        ]
        enabled = select_batch_condos(configs)
        self.assertEqual(
            [(position, config.requested_name) for position, config in
             slice_batch_condos(enabled, 2, 2)],
            [(2, "B"), (3, "C")],
        )
        group = select_batch_condos(configs, "g1")
        self.assertEqual(
            [(position, config.requested_name) for position, config in
             slice_batch_condos(group, 2, 1)],
            [(2, "C")],
        )

    def test_batch_slicing_rejects_invalid_values(self):
        configs = [self.condo_config("Only")]
        with self.assertRaisesRegex(ScraperError, "--start must be at least 1"):
            slice_batch_condos(configs, 0, None)
        with self.assertRaisesRegex(ScraperError, "--count must be at least 1"):
            slice_batch_condos(configs, 1, 0)
        with self.assertRaisesRegex(ScraperError, "exceeds the 1 enabled condos"):
            slice_batch_condos(configs, 2, 1)

    @patch("propertyguru_scraper.IPropertyClient")
    def test_start_beyond_available_exits_before_browser_launch(self, mocked_client):
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            start=99, count=1, dry_run=True, search_url=None,
            max_pages=10, limit=None, limit_per_condo=1, delay=1.5, timeout=30.0,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with self.assertRaisesRegex(ScraperError, "--start 99 exceeds"):
            run(args)
        mocked_client.assert_not_called()

    def test_bubble_client_has_no_condo_mutation_capability(self):
        for method_name in ("create_condo", "update_condo", "delete_condo", "write_condo"):
            self.assertFalse(hasattr(BubbleClient, method_name))

    def test_listing_create_and_update_endpoints_are_unchanged(self):
        bubble = BubbleClient("unused")
        bubble.session = MagicMock()
        bubble._json = MagicMock(return_value={"id": "new-listing-id"})
        payload = {"sourceListingID": "501195195"}
        self.assertEqual(bubble.write_listing(payload, None), "new-listing-id")
        bubble.session.post.assert_called_once()
        self.assertIn("/obj/listing", bubble.session.post.call_args.args[0])
        self.assertEqual(bubble.write_listing(payload, "existing-id"), "existing-id")
        bubble.session.patch.assert_called_once()
        self.assertTrue(bubble.session.patch.call_args.args[0].endswith(
            "/obj/listing/existing-id"
        ))

    def test_patch_204_empty_body_succeeds(self):
        bubble = BubbleClient("unused")
        bubble.session.patch = MagicMock(return_value=self.bubble_response("PATCH", 204))
        with self.assertLogs("iproperty_scraper", level="INFO") as logs:
            result = bubble.write_listing({"sourceListingID": "501195195"}, "existing-id")
        self.assertEqual(result, "existing-id")
        self.assertIn("Bubble update succeeded: HTTP 204", "\n".join(logs.output))
        self.assertIn("Updated existing Bubble Listing: existing-id", "\n".join(logs.output))

    def test_patch_200_empty_body_succeeds(self):
        bubble = BubbleClient("unused")
        bubble.session.patch = MagicMock(return_value=self.bubble_response("PATCH", 200))
        self.assertEqual(
            bubble.write_listing({"sourceListingID": "501195195"}, "existing-id"),
            "existing-id",
        )

    def test_patch_200_valid_json_succeeds(self):
        bubble = BubbleClient("unused")
        response = self.bubble_response(
            "PATCH", 200, b'{"response":{"updated":true}}', "application/json"
        )
        bubble.session.patch = MagicMock(return_value=response)
        self.assertEqual(
            bubble.write_listing({"sourceListingID": "501195195"}, "existing-id"),
            "existing-id",
        )

    def test_patch_400_json_retains_diagnostics(self):
        bubble = BubbleClient("unused")
        response = self.bubble_response(
            "PATCH", 400, b'{"message":"Invalid Furnishing"}', "application/json"
        )
        bubble.session.patch = MagicMock(return_value=response)
        with self.assertRaises(ScraperError) as raised:
            bubble.write_listing({"sourceListingID": "501195195"}, "existing-id")
        message = str(raised.exception)
        self.assertIn("Bubble API error 400", message)
        self.assertIn("PATCH", message)
        self.assertIn("sourceListingID", message)
        self.assertIn("Invalid Furnishing", message)

    def test_patch_500_text_retains_diagnostics(self):
        bubble = BubbleClient("unused")
        response = self.bubble_response("PATCH", 500, b"upstream unavailable", "text/plain")
        bubble.session.patch = MagicMock(return_value=response)
        with self.assertRaises(ScraperError) as raised:
            bubble.write_listing({"sourceListingID": "501195195"}, "existing-id")
        self.assertIn("Bubble API error 500", str(raised.exception))
        self.assertIn("upstream unavailable", str(raised.exception))

    def test_post_valid_id_succeeds_and_empty_body_fails(self):
        bubble = BubbleClient("unused")
        valid = self.bubble_response(
            "POST", 201, b'{"id":"new-id"}', "application/json"
        )
        bubble.session.post = MagicMock(return_value=valid)
        self.assertEqual(bubble.write_listing({}, None), "new-id")
        bubble.session.post.return_value = self.bubble_response("POST", 201)
        with self.assertRaisesRegex(ScraperError, "no Listing ID"):
            bubble.write_listing({}, None)

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_successful_existing_update_counts_updated_not_failed(
        self, mocked_bubble_class, mocked_client_class
    ):
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.return_value = ("condo-id", "name")
        bubble.find_listing.return_value = {"_id": "existing-id"}
        bubble.write_listing.return_value = "existing-id"
        client = mocked_client_class.return_value.__enter__.return_value
        client.discover_listings.return_value = [NormalizedListing(
            source_listing_id="501195195",
            source_url="https://www.iproperty.com.my/property/bangsar/serai/rent-501195195/",
            price_rent=15000.0,
        )]
        args = Namespace(
            condo="Serai", discover=None, dry_run=False, search_url=None,
            max_pages=10, limit=1, delay=1.5, timeout=30.0,
            condo_config_path=self.temporary_serai_config(),
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                self.assertEqual(run(args), 0)
        output = "\n".join(logs.output)
        self.assertIn("Updated: 1", output)
        self.assertIn("Failed: 0", output)
        bubble.find_listing.assert_called_once_with("501195195")

    def photo_update_fixture(self, existing, refresh=False, dry_run=False):
        bubble = MagicMock()
        bubble.find_condo.return_value = ("condo-id", "name")
        bubble.find_listing.return_value = existing
        client = MagicMock()
        client.discover_listings.return_value = [NormalizedListing(
            source_listing_id="501195195",
            source_url="https://www.iproperty.com.my/property/x/rent-501195195/",
            price_rent=12345.0,
            beds=3.0,
            cover_photo_url="https://images.test/cover.jpg",
            photo_urls=["https://images.test/one.jpg", "https://images.test/two.jpg"],
        )]
        args = Namespace(
            search_url=None, max_pages=1, dry_run=dry_run,
            refresh_photos=refresh,
        )
        stats = process_condo(self.condo_config(), args, bubble, client, 1)
        return bubble, stats

    def test_existing_populated_photos_are_omitted_from_patch(self):
        bubble, stats = self.photo_update_fixture({
            "_id": "existing-id", "photos": ["old-one", "old-two", "old-three"],
            "coverPhoto": "old-cover",
        })
        payload = bubble.write_listing.call_args.args[0]
        self.assertNotIn("photos", payload)
        self.assertNotIn("coverPhoto", payload)
        self.assertEqual(payload["priceRent"], 12345)
        self.assertEqual(payload["beds"], 3)
        self.assertEqual(stats["photos_skipped"], 1)
        self.assertEqual(stats["photos_processed"], 0)

    @patch("propertyguru_scraper.search_listing_photos")
    def test_existing_photo_decision_skips_structured_photo_extraction(
        self, mocked_photo_extraction
    ):
        items = parse_search_results(
            self.search_html([self.listing_record()]),
            "https://www.iproperty.com.my/property-for-rent/p/one-menerung",
            "One Menerung",
            should_process_photos=lambda _source_id: False,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].photo_urls, [])
        self.assertIsNone(items[0].cover_photo_url)
        mocked_photo_extraction.assert_not_called()

    def test_new_or_existing_without_photo_list_processes_photos(self):
        cases = (
            (None, None),
            ({"_id": "missing"}, "missing"),
            ({"_id": "null", "photos": None}, "null"),
            ({"_id": "empty", "photos": []}, "empty"),
            ({"_id": "cover-only", "photos": [], "coverPhoto": "old"}, "cover-only"),
        )
        for existing, label in cases:
            with self.subTest(label=label):
                bubble, stats = self.photo_update_fixture(existing)
                payload = bubble.write_listing.call_args.args[0]
                self.assertEqual(payload["photos"], [
                    "https://images.test/one.jpg", "https://images.test/two.jpg",
                ])
                self.assertEqual(payload["coverPhoto"], "https://images.test/cover.jpg")
                self.assertEqual(stats["photos_processed"], 1)
                self.assertEqual(stats["photos_skipped"], 0)

    def test_refresh_photos_forces_existing_photo_patch(self):
        bubble, stats = self.photo_update_fixture(
            {"_id": "existing-id", "photos": ["old"]}, refresh=True
        )
        payload = bubble.write_listing.call_args.args[0]
        self.assertIn("photos", payload)
        self.assertIn("coverPhoto", payload)
        self.assertEqual(stats["photos_processed"], 1)
        self.assertEqual(stats["photos_skipped"], 0)

    def test_dry_run_existing_photos_omits_photo_fields_and_never_writes(self):
        output = io.StringIO()
        with redirect_stdout(output):
            bubble, stats = self.photo_update_fixture(
                {"_id": "existing-id", "photos": ["old"]}, dry_run=True
            )
        diagnostic = json.loads(output.getvalue())
        self.assertEqual(diagnostic["action"], "update")
        self.assertNotIn("photos", diagnostic["bubble_payload"])
        self.assertNotIn("coverPhoto", diagnostic["bubble_payload"])
        bubble.write_listing.assert_not_called()
        self.assertEqual(stats["photos_skipped"], 1)

    @patch("propertyguru_scraper.random.uniform", return_value=6.0)
    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_batch_missing_condo_isolated_limit_applied_and_browser_closed(
        self, mocked_bubble_class, mocked_client_class, _mocked_uniform
    ):
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.side_effect = [
            ScraperError('No exact Condo match found for "One Menerung"'),
            ("serai-condo-id", "name"),
        ]
        bubble.find_listing.return_value = None
        events = []
        managers = [MagicMock(), MagicMock()]
        clients = [manager.__enter__.return_value for manager in managers]
        for position, manager in enumerate(managers, 1):
            manager.__exit__.side_effect = (
                lambda *unused, position=position: events.append(f"close-{position}")
            )
        launch_count = 0
        def launch_client(*unused):
            nonlocal launch_count
            launch_count += 1
            events.append(f"launch-{launch_count}")
            return managers[launch_count - 1]
        mocked_client_class.side_effect = launch_client
        clients[1].discover_listings.return_value = [NormalizedListing(
            source_listing_id="501195195",
            source_url="https://www.iproperty.com.my/property/bangsar/serai/rent-501195195/",
        )]
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            start=1, count=2,
            dry_run=True, search_url=None, max_pages=10, limit=None,
            limit_per_condo=1, delay=1.5, timeout=30.0,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with patch("propertyguru_scraper.time.sleep") as mocked_sleep:
                with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                    self.assertEqual(run(args), 0)
        output = "\n".join(logs.output)
        self.assertIn("SKIPPED: Bubble Condo not found: One Menerung", output)
        self.assertIn("Targets processed: 1", output)
        self.assertIn("Targets skipped: 1", output)
        self.assertEqual(clients[1].discover_listings.call_args.args[3], 1)
        bubble.write_listing.assert_not_called()
        self.assertEqual(mocked_client_class.call_count, 2)
        self.assertEqual(events, ["launch-1", "close-1", "launch-2", "close-2"])
        managers[0].__exit__.assert_called_once()
        managers[1].__exit__.assert_called_once()
        _mocked_uniform.assert_called_once_with(4.0, 7.5)
        mocked_sleep.assert_called_once_with(6.0)
        self.assertIn("Launching fresh Chromium for One Menerung", output)
        self.assertIn("Closing Chromium for Sri Penaga", output)

    @patch("propertyguru_scraper.random.uniform", return_value=6.0)
    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_batch_project_failure_does_not_stop_next_condo(
        self, mocked_bubble_class, mocked_client_class, _mocked_uniform
    ):
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.side_effect = [
            ("one-id", "name"), ("serai-id", "name")
        ]
        managers = [MagicMock(), MagicMock()]
        clients = [manager.__enter__.return_value for manager in managers]
        clients[0].discover_listings.side_effect = ScraperError(
            "Could not resolve configured iProperty project"
        )
        clients[1].discover_listings.return_value = []
        mocked_client_class.side_effect = managers
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            start=1, count=2,
            dry_run=True, search_url=None, max_pages=10, limit=None,
            limit_per_condo=1, delay=1.5, timeout=30.0,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with patch("propertyguru_scraper.time.sleep"):
                with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                    self.assertEqual(run(args), 0)
        self.assertEqual(clients[0].discover_listings.call_count, 1)
        self.assertEqual(clients[1].discover_listings.call_count, 1)
        managers[0].__exit__.assert_called_once()
        managers[1].__exit__.assert_called_once()
        self.assertIn("CONDO 2/65: Sri Penaga", "\n".join(logs.output))
        self.assertIn("BATCH ITEM 2/2", "\n".join(logs.output))

    @patch("propertyguru_scraper.time.sleep")
    @patch.object(IPropertyClient, "discover_listings", return_value=[])
    @patch("propertyguru_scraper.sync_playwright")
    @patch("propertyguru_scraper.BubbleClient")
    def test_batch_relaunches_real_browser_context_and_page_per_condo(
        self, mocked_bubble_class, mocked_sync_playwright,
        _mocked_discover, _mocked_sleep,
    ):
        mocked_bubble_class.return_value.find_condo.side_effect = [
            ("one-id", "name"), ("serai-id", "name")
        ]
        playwright = MagicMock()
        mocked_sync_playwright.return_value.start.return_value = playwright
        browsers = [MagicMock(), MagicMock()]
        contexts = [MagicMock(), MagicMock()]
        pages = [MagicMock(), MagicMock()]
        for browser, context, page in zip(browsers, contexts, pages):
            browser.new_context.return_value = context
            context.new_page.return_value = page
        playwright.chromium.launch.side_effect = browsers
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            start=1, count=2,
            dry_run=True, search_url=None, max_pages=10, limit=None,
            limit_per_condo=1, delay=1.5, timeout=30.0,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            self.assertEqual(run(args), 0)
        self.assertEqual(playwright.chromium.launch.call_count, 2)
        self.assertEqual(
            playwright.chromium.launch.call_args_list,
            [call(headless=False), call(headless=False)],
        )
        for browser, context, page in zip(browsers, contexts, pages):
            browser.new_context.assert_called_once()
            context.new_page.assert_called_once_with()
            page.close.assert_called_once_with()
            context.close.assert_called_once_with()
            browser.close.assert_called_once_with()

    def test_agency_is_not_inferred_when_structured_name_is_absent(self):
        listing = self.listing_record()
        listing["advertiser"] = {
            "name": "Amy Yap",
            "email": "amy@rvt-realty.example",
            "logo": "https://example.test/rvt-realty-logo.png",
        }
        item = parse_search_results(
            self.search_html([listing]),
            "https://www.iproperty.com.my/property-for-rent/p/one-menerung",
            "One Menerung",
        )[0]
        self.assertEqual(item.source_agent_name, "Amy Yap")
        self.assertIsNone(item.source_agency_name)
        payload = bubble_payload(item, "condo-id")
        self.assertEqual(payload["sourceAgentName"], "Amy Yap")
        self.assertNotIn("sourceAgencyName", payload)

    def test_furnishing_uses_exact_bubble_text_options(self):
        cases = {
            "Fully Furnished": "Fully Furnished",
            "Partly Furnished": "Partially Furnished",
            "Semi-Furnished": "Partially Furnished",
            "Unfurnished": "Unfurnished",
            "Prefer not to say": None,
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(normalize_furnishing(source), expected)

    def test_freetext_url_and_9_beringin_config(self):
        self.assertEqual(
            iproperty_freetext_search_url("9 Beringin"),
            "https://www.iproperty.com.my/property-for-rent?"
            "page=1&isCommercial=false&listingType=rent&"
            "_freetextDisplay=9+Beringin&freetext=9+Beringin",
        )
        config = load_condo_config("9 Beringin")
        self.assertEqual(config.iproperty_search_mode, "freetext")
        self.assertIsNone(config.iproperty_project_id)

    def test_freetext_exact_project_name_validation(self):
        exact = self.listing_record()
        exact["localizedTitle"] = "9 Beringin, Damansara Heights"
        exact["url"] = "/property/damansara-heights/9-beringin/rent-501195195/"
        unrelated = self.listing_record()
        unrelated["localizedTitle"] = "19 Beringin, Damansara Heights"
        unrelated["url"] = "/property/damansara-heights/19-beringin/rent-501195196/"
        unrelated["id"] = 501195196
        html = self.search_html([exact, unrelated])
        expected = IPropertyProject("9 beringin", None, "Condominium")
        items = parse_search_results(
            html, iproperty_freetext_search_url("9 Beringin"),
            "9 Beringin", expected,
        )
        self.assertEqual([item.source_listing_id for item in items], ["501195195"])

    def test_freetext_pagination_does_not_require_property_id(self):
        url = discover_pagination_url(
            self.search_html([self.listing_record()]),
            iproperty_freetext_search_url("9 Beringin"), 2,
            "9 Beringin", None, "freetext",
        )
        self.assertEqual(
            url,
            "https://www.iproperty.com.my/property-for-rent?"
            "page=2&isCommercial=false&listingType=rent&"
            "_freetextDisplay=9+Beringin&freetext=9+Beringin",
        )

    def test_freetext_mode_never_invokes_project_cache_callback(self):
        listing = self.listing_record()
        listing["localizedTitle"] = "9 Beringin, Damansara Heights"
        config = CondoConfig(
            "9 Beringin", "9 Beringin", "9 Beringin", None, None,
            "Bangsar/Damansara Heights", True, "freetext",
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_freetext_search_url("9 Beringin")
        client.get = MagicMock(return_value=self.search_html([listing]))
        callback = MagicMock()
        items = client.discover_listings(
            client.page.url, 1, config, 1, callback
        )
        self.assertEqual(len(items), 1)
        callback.assert_not_called()

    def test_freetext_with_valid_listing_parses_only_page_one_without_waiting(self):
        listing = self.listing_record()
        listing["localizedTitle"] = "9 Beringin, Damansara Heights"
        config = CondoConfig(
            "9 Beringin", "9 Beringin", "9 Beringin", None, None,
            "Bangsar/Damansara Heights", True, "freetext",
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_freetext_search_url("9 Beringin")
        client.get = MagicMock(return_value=self.search_html([listing]))
        with self.assertLogs("iproperty_scraper", level="INFO") as logs:
            items = client.discover_listings(client.page.url, 2, config)
        self.assertEqual([item.source_listing_id for item in items], ["501195195"])
        client.get.assert_called_once_with(client.page.url, 'a[href*="/rent-"]')
        client.page.wait_for_timeout.assert_not_called()
        self.assertIn(
            "Free-text mode: pagination disabled; using page 1 only.",
            "\n".join(logs.output),
        )

    def test_freetext_with_zero_valid_listings_never_requests_page_two(self):
        unrelated = self.listing_record()
        unrelated["localizedTitle"] = "19 Beringin, Damansara Heights"
        config = CondoConfig(
            "9 Beringin", "9 Beringin", "9 Beringin", None, None,
            "Bangsar/Damansara Heights", True, "freetext",
        )
        client = IPropertyClient()
        client.page = MagicMock()
        client.page.url = iproperty_freetext_search_url("9 Beringin")
        client.get = MagicMock(return_value=self.search_html([unrelated]))

        items = client.discover_listings(client.page.url, 10, config)

        self.assertEqual(items, [])
        client.get.assert_called_once_with(client.page.url, 'a[href*="/rent-"]')
        client.page.wait_for_timeout.assert_not_called()

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_dry_run_never_writes_to_bubble(self, mocked_bubble_class, mocked_client_class):
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.return_value = ("condo-id", "Name")
        bubble.find_listing.return_value = None
        client = mocked_client_class.return_value.__enter__.return_value
        client.discover_listings.return_value = [NormalizedListing(
            source_listing_id="501195195",
            source_url="https://www.iproperty.com.my/property/bangsar/serai/rent-501195195/",
            price_rent=15000.0,
        )]
        args = Namespace(
            condo="Serai", discover=None, dry_run=True, search_url=None,
            max_pages=10, limit=1,
            delay=1.5, timeout=30.0,
            condo_config_path=self.temporary_serai_config(),
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            self.assertEqual(run(args), 0)
        bubble.write_listing.assert_not_called()
        bubble.find_condo.assert_called_once_with("Serai Bukit Bandaraya")
        call = client.discover_listings.call_args
        self.assertEqual(
            call.args[0],
            "https://www.iproperty.com.my/property-for-rent?"
            "isCommercial=false&_freetextDisplay=Serai&propertyId=lj5z9k",
        )
        self.assertEqual(call.args[2].iproperty_search_name, "Serai")

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_dry_run_can_cache_identity_without_bubble_writes(
        self, mocked_bubble_class, mocked_client_class
    ):
        entries = [{
            "requested_name": "One Menerung", "bubble_name": "One Menerung",
            "iproperty_search_name": "One Menerung", "iproperty_project_name": None,
            "iproperty_project_id": None, "group": "Bangsar/Damansara Heights",
            "enabled": True,
        }]
        path = self.temporary_condo_config(entries)
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.return_value = ("condo-id", "name")
        client = mocked_client_class.return_value.__enter__.return_value
        def discover(_url, _pages, _config, _limit, callback, **_options):
            callback(IPropertyProject("One Menerung", "fd12m8", "Condominium"))
            return []
        client.discover_listings.side_effect = discover
        args = Namespace(
            condo="One Menerung", all_condos=False, group=None, discover=None,
            dry_run=True, search_url=None, max_pages=10, limit=1,
            limit_per_condo=None, delay=1.5, timeout=30.0,
            condo_config_path=path,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            self.assertEqual(run(args), 0)
        bubble.write_listing.assert_not_called()
        saved = json.loads(path.read_text(encoding="utf-8"))[0]
        self.assertEqual(saved["iproperty_project_id"], "fd12m8")

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_project_identity_caching_works_in_sliced_batch(
        self, mocked_bubble_class, mocked_client_class
    ):
        entries = [
            {
                "requested_name": "First", "bubble_name": "First",
                "iproperty_search_name": "First", "iproperty_project_name": None,
                "iproperty_project_id": None, "group": "G", "enabled": True,
            },
            {
                "requested_name": "Second", "bubble_name": "Second",
                "iproperty_search_name": "Second", "iproperty_project_name": None,
                "iproperty_project_id": None, "group": "G", "enabled": True,
            },
        ]
        path = self.temporary_condo_config(entries)
        mocked_bubble_class.return_value.find_condo.return_value = ("condo-id", "name")
        client = mocked_client_class.return_value.__enter__.return_value
        def discover(_url, _pages, _config, _limit, callback, **_options):
            callback(IPropertyProject("Second Canonical", "second-id", "Condominium"))
            return []
        client.discover_listings.side_effect = discover
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            start=2, count=1, dry_run=True, search_url=None, max_pages=10,
            limit=None, limit_per_condo=1, delay=1.5, timeout=30.0,
            condo_config_path=path,
            bubble_base_url="https://www.rentee.asia/version-test/api/1.1",
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                self.assertEqual(run(args), 0)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(saved[0]["iproperty_project_id"])
        self.assertEqual(saved[1]["iproperty_project_id"], "second-id")
        self.assertIn("Project identities discovered: 1", "\n".join(logs.output))
        mocked_bubble_class.return_value.write_listing.assert_not_called()

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

    def test_live_cli_flags_parse(self):
        args = build_parser().parse_args([
            "--all-condos", "--live", "--confirm-live-write",
        ])
        self.assertTrue(args.live)
        self.assertTrue(args.confirm_live_write)

    @patch("propertyguru_scraper.IPropertyClient")
    def test_unconfirmed_live_write_refuses_before_chromium(self, mocked_client):
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            missing_project_id_only=False, discover_only=False,
            live=True, confirm_live_write=False, dry_run=False,
        )
        with self.assertRaisesRegex(
            SafetyError, "Live Bubble writes require --confirm-live-write"
        ):
            run(args)
        mocked_client.assert_not_called()

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_live_single_condo_dry_run_uses_live_reads_and_zero_writes(
        self, mocked_bubble_class, mocked_client_class
    ):
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.return_value = ("live-condo", "name")
        client = mocked_client_class.return_value.__enter__.return_value
        client.discover_listings.return_value = []
        args = Namespace(
            condo="Serai", all_condos=False, group=None, discover=None,
            missing_project_id_only=False, discover_only=False,
            live=True, confirm_live_write=False, dry_run=True,
            start=None, count=None, search_url=None, max_pages=1, limit=1,
            limit_per_condo=None, refresh_photos=False, delay=1.5, timeout=30.0,
            condo_config_path=self.temporary_serai_config(),
            bubble_base_url=BUBBLE_BASE_URL,
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                self.assertEqual(run(args), 0)
        mocked_bubble_class.assert_called_once_with(
            "test", BUBBLE_LIVE_BASE_URL, live_writes_enabled=False
        )
        bubble.write_listing.assert_not_called()
        self.assertIn("Environment: LIVE", "\n".join(logs.output))
        self.assertIn("Writes enabled: NO", "\n".join(logs.output))

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_default_batch_uses_development_endpoint(
        self, mocked_bubble_class, mocked_client_class
    ):
        path = self.temporary_condo_config([{
            "requested_name": "One", "bubble_name": "One",
            "iproperty_search_name": "One", "iproperty_project_name": "One",
            "iproperty_project_id": "one-id", "group": "G", "enabled": True,
        }])
        mocked_bubble_class.return_value.find_condo.return_value = ("condo", "name")
        mocked_client_class.return_value.__enter__.return_value.discover_listings.return_value = []
        args = Namespace(
            condo=None, all_condos=True, group=None, discover=None,
            missing_project_id_only=False, discover_only=False,
            live=False, confirm_live_write=False, dry_run=True,
            start=None, count=None, search_url=None, max_pages=1, limit=None,
            limit_per_condo=1, refresh_photos=False, delay=1.5, timeout=30.0,
            condo_config_path=path, bubble_base_url=BUBBLE_BASE_URL,
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            with self.assertLogs("iproperty_scraper", level="INFO") as logs:
                self.assertEqual(run(args), 0)
        mocked_bubble_class.assert_called_once_with(
            "test", BUBBLE_BASE_URL, live_writes_enabled=False
        )
        self.assertIn("Environment: development", "\n".join(logs.output))
        self.assertIn("Bubble endpoint verified: version-test", "\n".join(logs.output))

    @patch("propertyguru_scraper.IPropertyClient")
    @patch("propertyguru_scraper.BubbleClient")
    def test_confirmed_live_single_condo_enables_listing_write(
        self, mocked_bubble_class, mocked_client_class
    ):
        bubble = mocked_bubble_class.return_value
        bubble.find_condo.return_value = ("live-condo", "name")
        bubble.find_listing.return_value = None
        client = mocked_client_class.return_value.__enter__.return_value
        client.discover_listings.return_value = [NormalizedListing(
            source_listing_id="501195195",
            source_url="https://www.iproperty.com.my/property/x/rent-501195195/",
        )]
        args = Namespace(
            condo="Serai", all_condos=False, group=None, discover=None,
            missing_project_id_only=False, discover_only=False,
            live=True, confirm_live_write=True, dry_run=False,
            start=None, count=None, search_url=None, max_pages=1, limit=1,
            limit_per_condo=None, refresh_photos=False, delay=1.5, timeout=30.0,
            condo_config_path=self.temporary_serai_config(),
            bubble_base_url=BUBBLE_BASE_URL,
        )
        with patch.dict("propertyguru_scraper.os.environ", {"BUBBLE_API_TOKEN": "test"}):
            self.assertEqual(run(args), 0)
        mocked_bubble_class.assert_called_once_with(
            "test", BUBBLE_LIVE_BASE_URL, live_writes_enabled=True
        )
        bubble.write_listing.assert_called_once()

    def test_live_enabled_bubble_client_allows_mocked_post_and_patch(self):
        bubble = BubbleClient(
            "unused", BUBBLE_LIVE_BASE_URL, live_writes_enabled=True
        )
        created = self.bubble_response(
            "POST", 201, b'{"id":"live-new"}', "application/json"
        )
        bubble.session.post = MagicMock(return_value=created)
        self.assertEqual(bubble.write_listing({}, None), "live-new")
        updated = self.bubble_response("PATCH", 204)
        bubble.session.patch = MagicMock(return_value=updated)
        self.assertEqual(bubble.write_listing({}, "live-existing"), "live-existing")

    @patch("propertyguru_scraper.BubbleClient")
    @patch("propertyguru_scraper.IPropertyClient")
    def test_discover_ignores_live_write_confirmation_and_stays_bubble_independent(
        self, mocked_client_class, mocked_bubble_class
    ):
        client = mocked_client_class.return_value.__enter__.return_value
        client.discover_projects.return_value = ("https://example.test", [])
        args = Namespace(
            condo=None, all_condos=False, group=None, discover="Serai",
            missing_project_id_only=False, discover_only=False,
            live=True, confirm_live_write=False, dry_run=False,
            start=None, count=None, delay=1.5, timeout=30.0,
        )
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run(args), 0)
        mocked_bubble_class.assert_not_called()

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
