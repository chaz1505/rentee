import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import whatsapp_importer as importer
import development_resolver as resolver


FIND_IMPORT_MATCHES = importer.find_import_matches


GEOS = [
    {"_id": "geo-bangsar", "Name": "Bangsar"},
    {"_id": "geo-klcc", "Name": "KLCC"},
    {"_id": "geo-brickfields", "Name": "Brickfields"},
    {"_id": "geo-pj", "Name": "Petaling Jaya"},
    {"_id": "geo-dh", "Name": "Damansara Heights"},
]

DEVELOPMENTS = [
    {"_id": "dev-one", "name": "One Menerung", "Geo": "geo-bangsar"},
    {"_id": "dev-serai", "name": "Serai", "Geo": "geo-klcc"},
]


def full_model_output(**updates):
    value = {
        "type": "unknown", "geo_names": [], "geo_name": None,
        "location_references": [], "location_reference": None,
        "preferred_development_names": [], "development_name": None,
        "transaction_types": [], "property_types": [], "property_type": None,
        "budget": None, "price_rent": None, "price_sale": None,
        "bedrooms_min": None, "beds": None,
        "lead_name": None,
        "adults": None, "children": None, "nationality": None,
        "occupation": None, "move_in_date": None, "pets": None,
        "furnishing_preference": None, "bathrooms_min": None,
        "start_date": None, "helpers": None, "notes": None,
        "baths": None, "sqft": None, "land_sqft": None,
        "furnished": None, "furnishing": None, "available": None,
        "availability_date": None, "balcony": None, "study": None,
        "family_room": None, "maid_room": None, "outdoor_area": None,
        "unit_number": None, "owner_name": None, "owner_contact": None,
        "source_agency_name": None,
        "proposing_agent": {"name": None, "phone": None, "ren": None},
    }
    value.update(updates)
    return value


class WhatsAppImporterTests(unittest.TestCase):
    def setUp(self):
        self.match_patcher = patch.object(
            importer, "find_import_matches", return_value=[]
        )
        self.match_patcher.start()
        self.addCleanup(self.match_patcher.stop)

    def parse_as(self, output, raw="forwarded message"):
        response = SimpleNamespace(
            status="completed", output_text=json.dumps(full_model_output(**output))
        )
        with patch.object(importer.rentee_app.client.responses, "create", return_value=response):
            return importer.parse_forwarded_message(raw)

    def test_incomplete_parser_response_logs_diagnostics_and_raises(self):
        response = SimpleNamespace(
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
            usage={"input_tokens": 100, "output_tokens": 2000},
        )
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create, patch("builtins.print") as log:
            with self.assertRaisesRegex(
                ValueError, "WhatsApp parser did not complete."
            ):
                importer.parse_forwarded_message("WTR Bangsar")
        self.assertEqual(create.call_args.kwargs["max_output_tokens"], 2000)
        rendered = "\n".join(str(call) for call in log.call_args_list)
        self.assertIn("status=incomplete", rendered)
        self.assertIn("incomplete_details={'reason': 'max_output_tokens'}", rendered)
        self.assertIn("usage={'input_tokens': 100, 'output_tokens': 2000}", rendered)

    def process_as(self, parsed):
        create = MagicMock(return_value="bubble-1")
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create", create), \
             patch.object(importer, "find_import_matches", return_value=[]):
            result = importer.process_whatsapp_import(
                "forwarded", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        return result, create

    def test_rental_lead_parsing_and_payload(self):
        parsed = self.parse_as({
            "type": "lead", "geo_names": ["Bangsar"],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
            "budget": 8000, "bedrooms_min": 3,
        }, "WTR\nBangsar\nCondo\n3 bedrooms\nBudget RM8k")
        self.assertEqual(parsed, {
            "type": "lead", "geo_names": ["Bangsar"],
            "location_references": ["Bangsar"],
            "preferred_development_names": [],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
            "proposing_agent": {"name": None, "phone": None, "ren": None},
            "budget": 8000, "bedrooms_min": 3,
        })
        payload = importer.build_lead_payload(
            parsed, importer.resolve_geo_names(parsed["geo_names"], GEOS), []
        )
        self.assertEqual(payload["budgetRent"], 8000)
        self.assertNotIn("budgetBuy", payload)

    def test_purchase_lead_uses_buy_budget(self):
        parsed = self.parse_as({
            "type": "lead", "geo_names": ["Damansara Heights"],
            "transaction_types": ["Buy/Sell"],
            "property_types": ["Landed", "House"], "budget": 3500000,
            "bedrooms_min": 4,
        }, "Buyer looking for landed house in Damansara Heights. Budget RM3.5m. Minimum 4 bedrooms.")
        payload = importer.build_lead_payload(parsed, [], [])
        self.assertEqual(parsed["budget"], 3500000)
        self.assertEqual(parsed["bedrooms_min"], 4)
        self.assertEqual(payload["budgetBuy"], 3500000)
        self.assertNotIn("budgetRent", payload)

    def test_wtb_budget_range_uses_upper_value_as_maximum_budget(self):
        message = """WTB Client looking to buy bungalow...
Budget between 5m to 6m"""
        response = SimpleNamespace(
            status="completed",
            output_text=json.dumps(full_model_output(
                type="lead",
                transaction_types=["Buy/Sell"],
                property_types=["Landed"],
                budget=6000000,
            )),
        )
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create:
            parsed = importer.parse_forwarded_message(message, import_type="lead")

        self.assertEqual(parsed["budget"], 6000000)
        payload = importer.build_lead_payload(parsed, [], [])
        self.assertEqual(payload["budgetBuy"], 6000000)
        self.assertNotIn("budgetRent", payload)
        prompt = create.call_args.kwargs["input"]
        self.assertIn("upper value as the single maximum budget", prompt)
        self.assertIn("'Budget between 5m to 6m' means budget=6000000", prompt)
        self.assertIn("RM3.5 mil/RM3.5 million=3500000", prompt)

    def test_new_imports_canonicalize_apartment_and_house(self):
        apartment = self.parse_as({
            "type": "lead", "property_types": ["Apartment"],
            "transaction_types": ["Rent/Let"],
        })
        house = self.parse_as({
            "type": "listing", "property_type": "House",
            "transaction_types": ["Buy/Sell"],
        })
        self.assertEqual(
            importer.build_lead_payload(apartment, [], [])["propertyTypes"], ["Condo"]
        )
        self.assertEqual(
            importer.build_listing_payload(house, None, None)["propertyType"], "Landed"
        )

    def test_new_listing_types_are_canonical_and_lead_may_request_both(self):
        expected = {
            "Apartment": "Condo", "Condo": "Condo",
            "House": "Landed", "Landed": "Landed",
        }
        for incoming, canonical in expected.items():
            with self.subTest(incoming=incoming):
                payload = importer.build_listing_payload(
                    {"property_type": incoming}, None, None
                )
                self.assertEqual(payload["propertyType"], canonical)
                self.assertIn(payload["propertyType"], {"Condo", "Landed"})
        lead = importer.build_lead_payload(
            {"property_types": ["Condo", "Landed"]}, [], []
        )
        self.assertEqual(lead["propertyTypes"], ["Condo", "Landed"])

    def test_explicit_lead_details_and_notes_map_to_existing_fields(self):
        parsed = self.parse_as({
            "type": "lead", "transaction_types": ["Rent/Let"],
            "adults": 2, "children": 1, "nationality": "British",
            "occupation": "Engineer", "move_in_date": "2026-11-01",
            "pets": "1 small dog", "furnishing_preference": "Fully Furnished",
            "bathrooms_min": 2, "start_date": "2026-11-15", "helpers": 1,
            "notes": "Prefers at least 1,500 sqft; two-year tenancy.",
        })
        payload = importer.build_lead_payload(parsed, [], [])
        self.assertEqual(payload, {
            "source": "whatsapp", "exposure": "public",
            "TransactionType": ["Rent/Let"],
            "adults": 2, "children": 1, "nationality": "British",
            "occupation": "Engineer", "moveInDate": "2026-11-01T00:00:00.000Z",
            "pets": "1 small dog", "furnishingPreference": "Fully Furnished",
            "bathroomsMin": 2, "startDate": "2026-11-15T00:00:00.000Z",
            "helpers": 1, "notes": "Prefers at least 1,500 sqft; two-year tenancy.",
        })

    def test_absent_or_invalid_lead_details_are_not_mapped(self):
        parsed = self.parse_as({
            "type": "lead", "adults": None, "children": None,
            "move_in_date": "mid November", "start_date": "soon",
            "furnishing_preference": "Anything is fine", "notes": "  ",
        })
        payload = importer.build_lead_payload(parsed, [], [])
        for field in (
            "adults", "children", "moveInDate", "startDate",
            "furnishingPreference", "notes",
        ):
            self.assertNotIn(field, payload)

    def test_lead_name_defaults_from_stored_agent_transaction_and_first_development(self):
        parsed = self.parse_as({
            "type": "lead", "transaction_types": ["Rent/Let"],
            "proposing_agent": {
                "name": "Alex Goh", "phone": "016-4697992", "ren": None,
            },
        })
        payload = importer.build_lead_payload(
            parsed,
            [{"matched": True, "id": "geo-bangsar", "name": "Bangsar"}],
            [
                {"matched": True, "id": "dev-1", "name": "Inspirasi"},
                {"matched": True, "id": "dev-2", "name": "Sefina"},
            ],
            {"name": "Alex Goh", "normalized_phone": "60164697992"},
        )
        self.assertEqual(payload["ProposedAgentNameLead"], "Alex Goh")
        self.assertEqual(payload["name"], "Alex Goh (Agent) WTR Inspirasi")

    def test_lead_name_uses_geo_and_buy_label_without_development(self):
        payload = importer.build_lead_payload(
            {"type": "lead", "transaction_types": ["Buy/Sell"]},
            [{"matched": True, "id": "geo-bangsar", "name": "Bangsar"}],
            [],
            {"name": "Alex Goh", "normalized_phone": "60164697992"},
        )
        self.assertEqual(payload["name"], "Alex Goh (Agent) WTB Bangsar")

    def test_explicit_lead_name_is_preserved(self):
        parsed = self.parse_as({
            "type": "lead", "lead_name": "Sarah Lim",
            "transaction_types": ["Rent/Let"],
        })
        payload = importer.build_lead_payload(
            parsed,
            [{"matched": True, "id": "geo-bangsar", "name": "Bangsar"}],
            [{"matched": True, "id": "dev-1", "name": "Inspirasi"}],
            {"name": "Alex Goh", "normalized_phone": "60164697992"},
        )
        self.assertEqual(payload["name"], "Sarah Lim")

    def test_import_payloads_use_proposing_agent_as_owner_and_whatsapp_source(self):
        proposing_agent = {
            "user_id": "user-agent", "name": "Alex Goh",
            "normalized_phone": "60164697992",
        }
        lead_payload = importer.build_lead_payload(
            {"type": "lead", "transaction_types": ["Rent/Let"]},
            [], [], proposing_agent,
        )
        listing_payload = importer.build_listing_payload(
            {"type": "listing", "transaction_types": ["Rent/Let"]},
            None, None, proposing_agent,
        )
        for payload in (lead_payload, listing_payload):
            self.assertEqual(payload["owner"], "user-agent")
            self.assertEqual(payload["source"], "whatsapp")

    def test_import_payloads_leave_owner_unset_without_proposing_agent_user(self):
        unresolved_agent = {
            "user_id": None, "name": "Alex Goh",
            "normalized_phone": "60164697992",
        }
        lead_payload = importer.build_lead_payload(
            {"type": "lead"}, [], [], unresolved_agent,
        )
        listing_payload = importer.build_listing_payload(
            {"type": "listing"}, None, None, unresolved_agent,
        )
        for payload in (lead_payload, listing_payload):
            self.assertNotIn("owner", payload)
            self.assertEqual(payload["source"], "whatsapp")

    def test_multiple_geo_lead_survives_parse_and_resolution(self):
        parsed = self.parse_as({
            "type": "lead", "geo_names": ["Brickfields", "Petaling Jaya"],
            "transaction_types": ["Rent/Let"], "property_types": ["Apartment"],
            "budget": 2500, "bedrooms_min": 2,
        }, "Looking to rent in Brickfields or PJ. Apartment, 2 bed, max RM2,500.")
        resolved = importer.resolve_geo_names(parsed["geo_names"], GEOS)
        self.assertEqual([item["id"] for item in resolved], ["geo-brickfields", "geo-pj"])

    def test_location_references_are_preserved_in_payloads(self):
        lead = importer.build_lead_payload({
            "type": "lead", "location_references": ["Sultan Ismail", "Setia Alam"],
        }, [], [])
        listing = importer.build_listing_payload({
            "type": "listing", "location_reference": "Near Sultan Ismail",
        }, None, None)
        self.assertEqual(lead["locationReferences"], ["Sultan Ismail", "Setia Alam"])
        self.assertEqual(listing["locationReference"], "Near Sultan Ismail")

    def test_location_reference_extraction_keeps_named_place_not_generic_proximity(self):
        parsed = self.parse_as({
            "type": "lead",
            "location_references": [
                "Near to Sultan Ismail",
                "near to working place. Walking distance",
            ],
            "transaction_types": ["Rent/Let"],
        })
        self.assertEqual(parsed["location_references"], ["Sultan Ismail"])

    def test_geo_verifier_retries_invalid_structured_output(self):
        responses = [
            SimpleNamespace(output_text=""),
            SimpleNamespace(output_text=json.dumps({"geo_names": ["KLCC"]})),
        ]
        with patch.object(
            importer.rentee_app.client.responses, "create", side_effect=responses
        ) as create:
            result = importer.verify_geo_reference(
                "Near to Sultan Ismail", GEOS, {"raw_text": "WTR near Sultan Ismail"}
            )
        self.assertEqual([item["id"] for item in result], ["geo-klcc"])
        self.assertEqual(create.call_count, 2)

    def test_geo_verifier_error_log_includes_exception_message(self):
        with patch.object(
            importer.rentee_app.client.responses, "create",
            side_effect=ValueError("bad structured response"),
        ), patch("builtins.print") as log:
            result = importer.verify_geo_reference("Sultan Ismail", GEOS, {})
        self.assertEqual(result, [])
        self.assertIn("ValueError: bad structured response", " ".join(
            str(call) for call in log.call_args_list
        ))

    def test_lead_fallback_combines_and_deduplicates_existing_geos(self):
        parsed = {
            "type": "lead", "location_references": ["Sultan Ismail"],
            "geo_names": [], "preferred_development_names": [],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
        }
        fallback = [
            {"matched": True, "id": "geo-klcc", "name": "KLCC"},
            {"matched": True, "id": "geo-brickfields", "name": "Brickfields"},
        ]
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "verify_geo_reference", return_value=fallback) as verify, \
            patch.object(importer.rentee_app, "_bubble_create", return_value="lead-1") as create:
            importer.process_whatsapp_import(
                "WTR near Sultan Ismail", geo_records=GEOS,
                development_records=DEVELOPMENTS,
            )
        self.assertEqual(create.call_args.args[2]["locationReferences"],
                         ["Sultan Ismail"])
        self.assertEqual(create.call_args.args[2]["Geo"],
                         ["geo-klcc", "geo-brickfields"])
        verify.assert_called_once()

    def test_lead_resolved_development_skips_geo_fallback(self):
        parsed = {
            "type": "lead", "location_references": ["Near One Menerung"],
            "geo_names": [], "preferred_development_names": ["One Menerung"],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "verify_geo_reference") as verify, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="lead-1") as create:
            importer.process_whatsapp_import(
                "WTR near One Menerung", geo_records=GEOS,
                development_records=DEVELOPMENTS,
            )
        verify.assert_not_called()
        self.assertEqual(create.call_args.args[2]["preferredDevelopments"], ["dev-one"])
        self.assertEqual(create.call_args.args[2]["locationReferences"],
                         ["Near One Menerung"])

    def test_unresolved_lead_is_created_with_clear_confirmation(self):
        parsed = {
            "type": "lead", "location_references": ["Unknown Place"],
            "geo_names": [], "preferred_development_names": [],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "verify_geo_reference", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create", return_value="lead-1") as create:
            result = importer.process_whatsapp_import(
                "WTR Unknown Place", geo_records=GEOS,
                development_records=DEVELOPMENTS,
            )
        self.assertNotIn("Geo", create.call_args.args[2])
        self.assertEqual(create.call_args.args[2]["locationReferences"], ["Unknown Place"])
        self.assertTrue(result["confirmation"].endswith(
            "Couldn't resolve Geo: Unknown Place."
        ))

    def test_listing_uses_fallback_only_when_direct_and_development_geo_fail(self):
        parsed = {
            "type": "listing", "location_reference": "Sultan Ismail",
            "transaction_types": ["Rent/Let"], "price_rent": 5000,
        }
        fallback = [{"matched": True, "id": "geo-klcc", "name": "KLCC"}]
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "verify_geo_reference", return_value=fallback), \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create:
            importer.process_whatsapp_import(
                "WTL near Sultan Ismail", geo_records=GEOS,
                development_records=DEVELOPMENTS,
            )
        self.assertEqual(create.call_args.args[2]["locationReference"], "Sultan Ismail")
        self.assertEqual(create.call_args.args[2]["Geo"], "geo-klcc")

    def test_mont_kiara_geo_format_variants_resolve_canonically(self):
        geos = [{"_id": "geo-mk", "Name": "Mont Kiara"}]
        variants = [
            "Mont Kiara", "Mont'Kiara", "Mont' Kiara", "Mont’ Kiara",
            "Mont' Kiara, Kuala Lumpur",
            "Mont' Kiara (Jalan Kiara 3), Kuala Lumpur",
            "Mont Kiara, Kuala Lumpur, Malaysia",
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                resolved = importer.resolve_geo_name(variant, geos)
                self.assertTrue(resolved["matched"])
                self.assertEqual(resolved["id"], "geo-mk")
                self.assertEqual(resolved["name"], "Mont Kiara")

    def test_geo_normalization_does_not_choose_duplicate_canonical_key(self):
        geos = [
            {"_id": "geo-mk-1", "Name": "Mont Kiara"},
            {"_id": "geo-mk-2", "Name": "Mont'Kiara"},
        ]
        result = importer.resolve_geo_name(
            "Mont' Kiara (Jalan Kiara 3), Kuala Lumpur", geos
        )
        self.assertFalse(result["matched"])
        self.assertEqual(result["reason"], "ambiguous")

    def test_preferred_developments_derive_unique_lead_geos(self):
        parsed = {
            "type": "lead", "geo_names": [],
            "preferred_development_names": ["One Menerung", "Serai"],
            "transaction_types": ["Rent/Let"], "property_types": [],
            "budget": 12000, "bedrooms_min": 3,
        }
        result, create = self.process_as(parsed)
        payload = create.call_args.args[2]
        self.assertEqual(payload["preferredDevelopments"], ["dev-one", "dev-serai"])
        self.assertEqual(payload["Geo"], ["geo-bangsar", "geo-klcc"])
        self.assertEqual(len(result["resolved_developments"]), 2)

    def test_lead_confirmation_includes_resolved_preferred_developments(self):
        parsed = {
            "type": "lead", "property_types": ["Condo"],
            "budget": 5000, "bedrooms_min": 3,
            "proposing_agent": {"name": "Alex Goh", "phone": "016-4697992"},
        }
        geos = [{"matched": True, "id": "geo-mk", "name": "Mont Kiara"}]
        developments = [
            {"matched": True, "id": "dev-1", "name": "Inspirasi Mont Kiara"},
            {"matched": True, "id": "dev-2", "name": "Mont Kiara Astana"},
            {"matched": False, "raw_name": "Unknown"},
        ]
        self.assertEqual(
            importer._confirmation(parsed, geos, developments),
            "Added lead to Alex Goh: 60164697992: Mont Kiara, Condo, 3 bed, up to RM5,000. "
            "Developments: Inspirasi Mont Kiara, Mont Kiara Astana.",
        )

    def test_lead_confirmation_is_unchanged_without_resolved_developments(self):
        parsed = {
            "type": "lead", "property_types": ["Condo"],
            "budget": 5000, "bedrooms_min": 3,
        }
        geos = [{"matched": True, "id": "geo-mk", "name": "Mont Kiara"}]
        self.assertEqual(
            importer._confirmation(parsed, geos, []),
            "Added lead: Mont Kiara, Condo, 3 bed, up to RM5,000.",
        )

    def test_listing_confirmation_includes_agent_and_extracted_details(self):
        parsed = {
            "type": "listing", "transaction_types": ["Buy/Sell"],
            "beds": 3, "baths": 2, "sqft": 1000,
            "furnishing": "Partially Furnished", "price_sale": 850000,
            "proposing_agent": {"name": "Jane Tee", "phone": "017-3262281"},
        }
        geos = [{"matched": True, "name": "Ampang"}]
        developments = [{"matched": True, "name": "Arte Plus"}]
        self.assertEqual(
            importer._confirmation(parsed, geos, developments),
            "Added listing to Jane Tee: 60173262281: Arte Plus, Ampang — "
            "3 bed, 2 bath, 1,000 sqft, partially furnished, RM850,000.",
        )

    def test_listing_confirmation_without_agent_keeps_default_prefix(self):
        parsed = {
            "type": "listing", "transaction_types": ["Rent/Let"],
            "beds": 2, "price_rent": 4500,
        }
        self.assertEqual(
            importer._confirmation(
                parsed, [{"matched": True, "name": "Bangsar"}], []
            ),
            "Added listing: Bangsar — 2 bed, RM4,500/month.",
        )

    def test_rental_listing_and_development_geo_derivation(self):
        parsed = self.parse_as({
            "type": "listing", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "price_rent": 8500, "beds": 3,
        }, "One Menerung\n3 bed\nFor rent RM8,500")
        result, create = self.process_as(parsed)
        payload = create.call_args.args[2]
        self.assertEqual(payload["development"], "dev-one")
        self.assertEqual(payload["Geo"], "geo-bangsar")
        self.assertEqual(payload["priceRent"], 8500)
        self.assertNotIn("priceSale", payload)
        self.assertEqual(result["resolved_geo"]["method"], "development_geo")

    def test_sale_listing_uses_sale_price(self):
        parsed = self.parse_as({
            "type": "listing", "geo_name": "Bangsar",
            "transaction_types": ["Buy/Sell"], "property_type": "Condo",
            "price_sale": 1800000, "beds": 3,
        }, "WTS\n3 bedroom condo\nRM1.8m\nBangsar")
        payload = importer.build_listing_payload(
            parsed, importer.resolve_geo_name("Bangsar", GEOS), None
        )
        self.assertEqual(payload["priceSale"], 1800000)
        self.assertNotIn("priceRent", payload)

    def test_combined_rent_and_sale_listing_keeps_distinct_prices(self):
        parsed = self.parse_as({
            "type": "listing", "development_name": "Lakeview Residences",
            "geo_name": "Lakeview Township",
            "transaction_types": ["Rent/Let", "Buy/Sell"],
            "price_rent": 4800, "price_sale": 1100000,
        }, (
            "WTS/WTL\nLakeview Residences, Lakeview Township\n\n"
            "for rent at RM4,800 per month.\nfor sell at RM1.1Mil"
        ))
        payload = importer.build_listing_payload(parsed, None, None)
        self.assertEqual(payload["TransactionType"], ["Rent/Let", "Buy/Sell"])
        self.assertEqual(payload["priceRent"], 4800)
        self.assertEqual(payload["priceSale"], 1100000)
        schema = importer._parser_schema("listing")
        self.assertIn("price_rent", schema["properties"])
        self.assertIn("price_sale", schema["properties"])
        self.assertNotIn("asking_price", schema["properties"])

    def test_combined_listing_does_not_copy_an_unstated_transaction_price(self):
        parsed = self.parse_as({
            "type": "listing", "geo_name": "Bangsar",
            "transaction_types": ["Rent/Let", "Buy/Sell"],
            "price_rent": 4800, "price_sale": None,
        })
        payload = importer.build_listing_payload(parsed, None, None)
        self.assertEqual(payload["priceRent"], 4800)
        self.assertNotIn("priceSale", payload)

    def test_listing_details_parse_and_map_to_existing_bubble_fields(self):
        parsed = self.parse_as({
            "type": "listing", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "price_rent": 12000, "beds": 3,
            "baths": 2.5, "sqft": 1800, "land_sqft": 2400,
            "furnished": "Yes", "furnishing": "Fully Furnished",
            "available": True, "availability_date": "2026-11-01",
            "balcony": "Yes", "study": 1, "family_room": 1,
            "maid_room": 1, "outdoor_area": "No", "unit_number": "A-12-3",
            "owner_name": "Sarah Lim", "owner_contact": "+60 12-345 6789",
            "source_agency_name": "RVT Realty",
            "notes": "Private lift; view of the park.",
        })
        payload = importer.build_listing_payload(
            parsed, None,
            {"matched": True, "id": "dev-one", "name": "One Menerung"},
        )
        self.assertEqual(payload, {
            "exposure": "public", "source": "whatsapp", "development": "dev-one",
            "TransactionType": ["Rent/Let"], "beds": 3, "priceRent": 12000,
            "baths": 2.5, "Sq Ft": 1800, "Landed_sqft": 2400,
            "furnished": "Yes", "Furnishing": "Fully Furnished",
            "availability": True,
            "availability_date": "2026-11-01",
            "balcony": "Yes", "study": 1, "family room": 1,
            "maid room": 1, "outdoor area": "No", "unitNumber": "A-12-3",
            "ownerName": "Sarah Lim", "ownerContact": "+60 12-345 6789",
            "sourceAgencyName": "RVT Realty",
            "Notes": "Private lift; view of the park.",
        })

    def test_listing_omits_unsupported_details_and_always_sets_public_exposure(self):
        parsed = self.parse_as({
            "type": "listing", "geo_name": "Bangsar",
            "transaction_types": ["Buy/Sell"], "availability_date": "soon",
            "furnished": None, "balcony": None, "outdoor_area": None,
        })
        payload = importer.build_listing_payload(parsed, None, None)
        self.assertEqual(payload["exposure"], "public")
        for field in (
            "availability_date", "furnished", "balcony", "outdoor area", "Notes",
        ):
            self.assertNotIn(field, payload)
        self.assertNotIn("exposure", importer.PARSER_SCHEMA["properties"])

    def test_explicit_geo_wins_and_conflict_is_logged(self):
        parsed = {
            "type": "listing", "geo_name": "KLCC", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "price_rent": 8500, "beds": 3,
        }
        with patch("builtins.print") as log:
            _result, create = self.process_as(parsed)
        self.assertEqual(create.call_args.args[2]["Geo"], "geo-klcc")
        self.assertTrue(any("geo conflict" in str(call) for call in log.call_args_list))

    def test_unknown_does_not_write_to_bubble(self):
        parsed = self.parse_as({}, "Thanks, will check and get back to you.")
        self.assertEqual(parsed, {"type": "unknown"})
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            result = importer.process_whatsapp_import("Thanks")
        self.assertEqual(result, {"status": "unknown", "type": "unknown"})
        create.assert_not_called()

    def test_invalid_lead_does_not_write_to_bubble_and_requests_full_message(self):
        parsed = {
            "type": "lead", "geo_names": [], "preferred_development_names": [],
            "transaction_types": [], "property_types": [],
            "proposing_agent": {"name": "Alex Goh", "phone": "016-4697992"},
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create") as create, \
             patch.object(importer, "resolve_or_create_proposing_agent") as resolve_agent:
            result = importer.process_whatsapp_import(
                "WTR", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(result["status"], "invalid")
        self.assertIn("resend the full forwarded message", result["confirmation"])
        create.assert_not_called()
        resolve_agent.assert_not_called()

    def test_invalid_listing_does_not_write_to_bubble_and_requests_full_message(self):
        invalid_listings = (
            {"type": "listing", "transaction_types": ["Rent/Let"]},
            {"type": "listing", "development_name": "One Menerung"},
        )
        for parsed in invalid_listings:
            with self.subTest(parsed=parsed), \
                 patch.object(importer, "parse_forwarded_message", return_value=parsed), \
                 patch.object(importer.rentee_app, "_bubble_create") as create, \
                 patch.object(importer, "resolve_or_create_proposing_agent") as resolve_agent:
                result = importer.process_whatsapp_import(
                    "partial listing", geo_records=GEOS,
                    development_records=DEVELOPMENTS,
                )
            self.assertEqual(result["status"], "invalid")
            self.assertIn("resend the full forwarded message", result["confirmation"])
            create.assert_not_called()
            resolve_agent.assert_not_called()

    def test_matching_requires_exact_shared_transaction(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "budgetRent": 5000,
        }
        listing = {
            "TransactionType": ["Buy/Sell"], "Geo": "geo-bangsar",
            "priceSale": 5000, "owner": "user-agent",
        }
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["TransactionType"] = ["rent/let"]
        self.assertFalse(importer.lead_matches_listing(lead, listing))

    def test_cancelled_and_availability_boolean_eligibility_gates(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "propertyTypes": ["Condo"],
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "propertyType": "Condo", "owner": "user-agent",
        }
        for cancelled, expected in (
            (True, False), (False, True), (None, True), ("true", True),
        ):
            with self.subTest(cancelled=cancelled):
                candidate = dict(lead)
                if cancelled is not None:
                    candidate["cancelled"] = cancelled
                self.assertEqual(
                    importer.lead_matches_listing(candidate, listing), expected
                )
        self.assertTrue(importer.lead_matches_listing(
            dict(lead, cancelled=None), listing
        ))
        for availability, expected in (
            (False, False), (True, True), (None, True), ("false", True),
        ):
            with self.subTest(availability=availability):
                candidate = dict(listing)
                if availability is not None:
                    candidate["availability"] = availability
                self.assertEqual(
                    importer.lead_matches_listing(lead, candidate), expected
                )
        self.assertTrue(importer.lead_matches_listing(
            lead, dict(listing, availability=None)
        ))

    def test_listing_owner_is_required_for_matching(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "propertyTypes": ["Condo"],
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "propertyType": "Condo",
            "owner": "user-agent",
        }
        for owner in (None, "", []):
            with self.subTest(owner=owner):
                self.assertFalse(importer.lead_matches_listing(
                    lead, dict(listing, owner=owner)
                ))
        self.assertTrue(importer.lead_matches_listing(
            lead, dict(listing, owner="user-agent")
        ))

    def test_listing_date_eligibility_uses_calendar_month_window_and_priority(self):
        today = importer.datetime.date(2026, 1, 31)
        self.assertTrue(importer._listing_date_eligible(
            {"availability_date": "2026-04-30T00:00:00.000Z"}, today
        ))
        self.assertFalse(importer._listing_date_eligible(
            {"availability_date": "2026-05-01"}, today
        ))
        self.assertTrue(importer._listing_date_eligible(
            {"availability_date": "2025-01-01"}, today
        ))
        self.assertTrue(importer._listing_date_eligible(
            {"tenantExpiry": "2026-04-30"}, today
        ))
        self.assertFalse(importer._listing_date_eligible(
            {"tenantExpiry": "2026-05-01"}, today
        ))
        self.assertFalse(importer._listing_date_eligible({
            "availability_date": "2026-05-01", "tenantExpiry": "2026-02-01",
        }, today))
        self.assertTrue(importer._listing_date_eligible({
            "availability_date": "2026-02-01", "tenantExpiry": "2027-01-01",
        }, today))
        self.assertTrue(importer._listing_date_eligible({}, today))

    def test_future_listing_date_blocks_otherwise_perfect_match(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "propertyTypes": ["Condo"],
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "propertyType": "Condo", "owner": "user-agent",
            "availability_date": (
                importer.datetime.date.today() + importer.datetime.timedelta(days=200)
            ).isoformat(),
        }
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["availability_date"] = (
            importer.datetime.date.today() - importer.datetime.timedelta(days=1)
        ).isoformat()
        self.assertTrue(importer.lead_matches_listing(lead, listing))

    def test_ineligible_pairs_do_not_create_match_records(self):
        lead = {
            "_id": "lead-1", "TransactionType": ["Rent/Let"],
            "Geo": ["geo-bangsar"], "propertyTypes": ["Condo"],
        }
        listing = {
            "_id": "listing-1", "TransactionType": ["Rent/Let"],
            "Geo": "geo-bangsar", "propertyType": "Condo",
            "owner": "user-agent",
        }
        cases = (
            ("lead", dict(lead, cancelled=True), listing),
            ("listing", dict(listing, availability=False), lead),
        )
        for created_type, created, existing in cases:
            with self.subTest(created_type=created_type), \
                 patch.object(importer.rentee_app, "_bubble_records",
                              return_value=[existing]), \
                 patch.object(importer.rentee_app, "_bubble_create") as create:
                matches = importer.find_import_matches(created_type, created, "live")
                importer.create_missing_match_records(
                    created_type, created["_id"], matches, "live"
                )
            self.assertEqual(matches, [])
            create.assert_not_called()

    def test_matching_requires_development_or_geo_overlap(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "preferredDevelopments": ["dev-one"], "bedroomsMin": 2,
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-klcc",
            "development": "dev-serai", "beds": 2,
            "owner": "user-agent",
        }
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["development"] = "dev-one"
        self.assertTrue(importer.lead_matches_listing(lead, listing))
        listing["development"] = "dev-serai"
        listing["Geo"] = "geo-bangsar"
        self.assertTrue(importer.lead_matches_listing(lead, listing))

    def test_matching_budget_uses_applicable_price_and_inclusive_range(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "budgetRent": 10000,
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "owner": "user-agent",
        }
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["priceSale"] = 10000
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        for price in (8000, 12000):
            with self.subTest(price=price):
                listing["priceRent"] = price
                self.assertTrue(importer.lead_matches_listing(lead, listing))
        listing["priceRent"] = 12001
        self.assertFalse(importer.lead_matches_listing(lead, listing))

    def test_matching_requires_bedroom_minimum_when_present(self):
        lead = {
            "TransactionType": ["Buy/Sell"], "Geo": ["geo-bangsar"],
            "bedroomsMin": 3,
        }
        listing = {
            "TransactionType": ["Buy/Sell"], "Geo": "geo-bangsar",
            "owner": "user-agent",
        }
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["beds"] = 2
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["beds"] = 3
        self.assertTrue(importer.lead_matches_listing(lead, listing))

    def test_matching_requires_budget_and_bedrooms_to_both_pass(self):
        lead = {
            "TransactionType": ["Buy/Sell"], "Geo": ["geo-bangsar"],
            "budgetBuy": 1000000, "bedroomsMin": 3,
        }
        listing = {
            "TransactionType": ["Buy/Sell"], "Geo": "geo-bangsar",
            "priceSale": 1000000, "beds": 2, "owner": "user-agent",
        }
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["beds"] = 3
        listing["priceSale"] = 1300000
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["priceSale"] = 1000000
        self.assertTrue(importer.lead_matches_listing(lead, listing))

    def test_matching_property_type_only_and_legacy_values(self):
        lead = {
            "TransactionType": ["Buy/Sell"], "Geo": ["geo-ttdi"],
            "propertyTypes": ["Landed"],
        }
        listing = {
            "TransactionType": ["Buy/Sell"], "Geo": "geo-ttdi",
            "propertyType": "Landed", "owner": "user-agent",
        }
        self.assertTrue(importer.lead_matches_listing(lead, listing))
        listing["propertyType"] = "Condo"
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["propertyType"] = "House"
        self.assertTrue(importer.lead_matches_listing(lead, listing))

        lead["propertyTypes"] = ["Condo"]
        self.assertFalse(importer.lead_matches_listing(lead, listing))
        listing["propertyType"] = "Apartment"
        self.assertTrue(importer.lead_matches_listing(lead, listing))
        listing["propertyType"] = "Landed"
        self.assertFalse(importer.lead_matches_listing(lead, listing))

        lead["propertyTypes"] = ["Condo", "Landed"]
        self.assertTrue(importer.lead_matches_listing(lead, listing))
        listing["propertyType"] = "Condo"
        self.assertTrue(importer.lead_matches_listing(lead, listing))

    def test_matching_applies_property_bedrooms_and_budget_together(self):
        lead = {
            "TransactionType": ["Buy/Sell"], "Geo": ["geo-ttdi"],
            "propertyTypes": ["Landed"], "bedroomsMin": 4,
            "budgetBuy": 1000000,
        }
        listing = {
            "TransactionType": ["Buy/Sell"], "Geo": "geo-ttdi",
            "propertyType": "House", "beds": 4, "priceSale": 1000000,
            "owner": "user-agent",
        }
        self.assertTrue(importer.lead_matches_listing(lead, listing))
        for field, bad_value in (
            ("propertyType", "Condo"), ("beds", 3), ("priceSale", 1300000),
        ):
            candidate = dict(listing, **{field: bad_value})
            self.assertFalse(importer.lead_matches_listing(lead, candidate))

    def test_legacy_matching_is_read_only(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "propertyTypes": ["Condo"],
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "propertyType": "Apartment", "owner": "user-agent",
        }
        original = dict(listing)
        with patch.object(importer.rentee_app, "_bubble_create") as create, \
             patch.object(importer.rentee_app, "_bubble_patch") as update:
            self.assertTrue(importer.lead_matches_listing(lead, listing))
        self.assertEqual(listing, original)
        create.assert_not_called()
        update.assert_not_called()

    def test_matching_rejects_under_specified_lead(self):
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "priceRent": 5000, "beds": 2, "owner": "user-agent",
        }
        for lead in (
            {"Geo": ["geo-bangsar"], "bedroomsMin": 2},
            {"TransactionType": ["Rent/Let"], "bedroomsMin": 2},
            {"TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"]},
        ):
            with self.subTest(lead=lead):
                self.assertFalse(importer.lead_matches_listing(lead, listing))

    def test_match_lookup_checks_opposite_bubble_record_type_in_both_directions(self):
        lead = {
            "TransactionType": ["Rent/Let"], "Geo": ["geo-bangsar"],
            "budgetRent": 5000,
        }
        listing = {
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "priceRent": 5000, "owner": "user-agent",
        }
        with patch.object(importer.rentee_app, "_bubble_records", return_value=[listing]) as records:
            self.assertEqual(FIND_IMPORT_MATCHES("lead", lead, "live"), [listing])
        self.assertEqual(records.call_args.args[1], "listing")
        with patch.object(importer.rentee_app, "_bubble_records", return_value=[lead]) as records:
            self.assertEqual(FIND_IMPORT_MATCHES("listing", listing, "live"), [lead])
        self.assertEqual(records.call_args.args[1], "lead")

    def test_new_lead_match_creates_match_record(self):
        with patch.object(importer.rentee_app, "_bubble_records", return_value=[]) as records, \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            importer.create_missing_match_records(
                "lead", "lead-new", [{"_id": "listing-existing"}], "live"
            )
        self.assertEqual(records.call_args.args[1], "match")
        create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "match",
            {"lead": "lead-new", "listing": "listing-existing"},
        )

    def test_source_message_hash_normalizes_only_harmless_whitespace(self):
        original = "WTB\r\nTTDI   landed  \r\n\r\nBudget RM1,000 "
        harmless = "  WTB\nTTDI landed\n\n\nBudget RM1,000\n"
        changed = "WTB\nTTDI landed\n\nBudget RM1,100"
        self.assertEqual(
            importer.source_message_hash(original),
            importer.source_message_hash(harmless),
        )
        self.assertNotEqual(
            importer.source_message_hash(original),
            importer.source_message_hash(changed),
        )

    def test_new_import_stores_source_message_hash(self):
        parsed = {
            "type": "lead", "geo_names": ["Bangsar"],
            "preferred_development_names": [], "transaction_types": ["Rent/Let"],
            "property_types": ["Condo"],
        }
        _result, create = self.process_as(parsed)
        self.assertEqual(
            create.call_args.args[2]["sourceMessageHash"],
            importer.source_message_hash("forwarded"),
        )
        self.assertEqual(create.call_args.args[2]["waMessage"], "forwarded")

    def test_new_listing_stores_original_whatsapp_message_verbatim(self):
        raw = "WTL\r\nCondo 🏠\nAgent: Gwen\n+6017-4156107  "
        parsed = {
            "type": "listing", "geo_name": "Bangsar",
            "transaction_types": ["Rent/Let"], "price_rent": 5000,
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="listing-1") as create:
            importer.process_whatsapp_import(
                raw, geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(create.call_args.args[2]["waMessage"], raw)

    def test_duplicate_lookup_is_scoped_by_type_owner_and_hash(self):
        existing = {"_id": "lead-existing", "owner": "user-1",
                    "sourceMessageHash": "hash-1"}

        def records(_base, object_type, constraints):
            values = {item["key"]: item["value"] for item in constraints}
            if (object_type == "lead" and values == {
                    "owner": "user-1", "sourceMessageHash": "hash-1"}):
                return [existing]
            return []

        with patch.object(importer.rentee_app, "_bubble_records", side_effect=records):
            self.assertEqual(
                importer._find_duplicate_import("lead", "user-1", "hash-1", "live"),
                existing,
            )
            self.assertIsNone(importer._find_duplicate_import(
                "lead", "user-2", "hash-1", "live"
            ))
            self.assertIsNone(importer._find_duplicate_import(
                "lead", "user-1", "changed", "live"
            ))
            self.assertIsNone(importer._find_duplicate_import(
                "listing", "user-1", "hash-1", "live"
            ))

    def test_historical_record_without_hash_is_not_duplicate_or_modified(self):
        historical = {"_id": "lead-old", "owner": "user-1", "name": "Keep Me"}
        with patch.object(
            importer.rentee_app, "_bubble_records", return_value=[historical]
        ), patch.object(importer.rentee_app, "_bubble_patch") as update:
            result = importer._find_duplicate_import(
                "lead", "user-1", "incoming-hash", "live"
            )
        self.assertIsNone(result)
        self.assertEqual(historical, {
            "_id": "lead-old", "owner": "user-1", "name": "Keep Me",
        })
        update.assert_not_called()

    def test_duplicate_lead_reuses_existing_record_and_reruns_matching(self):
        raw = "WTB TTDI landed"
        existing_lead = {
            "_id": "lead-existing", "owner": "user-1",
            "sourceMessageHash": importer.source_message_hash(raw),
            "TransactionType": ["Buy/Sell"], "Geo": ["geo-ttdi"],
            "propertyTypes": ["Landed"], "unchanged": "keep",
        }
        listings = [
            {"_id": "listing-old", "TransactionType": ["Buy/Sell"],
             "Geo": "geo-ttdi", "propertyType": "Landed",
             "owner": "user-agent"},
            {"_id": "listing-new", "TransactionType": ["Buy/Sell"],
             "Geo": "geo-ttdi", "propertyType": "Landed",
             "owner": "user-agent"},
        ]

        def records(_base, object_type, constraints=None, **_kwargs):
            if object_type == "lead":
                return [existing_lead]
            if object_type == "listing":
                return listings
            if object_type == "match":
                values = {item["key"]: item["value"] for item in constraints}
                return ([{"_id": "match-old"}]
                        if values["listing"] == "listing-old" else [])
            return []

        parsed = {
            "type": "lead", "geo_names": ["TTDI"],
            "location_references": ["TTDI"], "preferred_development_names": [],
            "transaction_types": ["Buy/Sell"], "property_types": ["Landed"],
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "resolve_or_create_proposing_agent", return_value={
                 "status": "existing", "user_id": "user-1",
             }), patch.object(importer, "find_import_matches",
                              side_effect=FIND_IMPORT_MATCHES), \
             patch.object(importer.rentee_app, "_bubble_records",
                              side_effect=records), \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="match-new") as create, \
             patch.object(importer.rentee_app, "_bubble_patch") as update:
            result = importer.process_whatsapp_import(
                raw, geo_records=[{"_id": "geo-ttdi", "Name": "TTDI"}],
                development_records=[],
            )
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["bubble_id"], "lead-existing")
        self.assertTrue(result["confirmation"].startswith(
            "This lead already exists.\n\nFound 2 matching listings:"
        ), result["confirmation"])
        self.assertEqual(create.call_count, 1)
        self.assertEqual(create.call_args.args[1:], (
            "match", {"lead": "lead-existing", "listing": "listing-new"}
        ))
        self.assertEqual(existing_lead["unchanged"], "keep")
        update.assert_not_called()

    def test_duplicate_listing_reuses_existing_record_and_reruns_matching(self):
        raw = "WTL One Menerung RM5,000"
        existing = {
            "_id": "listing-existing", "owner": "user-1",
            "sourceMessageHash": importer.source_message_hash(raw),
            "TransactionType": ["Rent/Let"], "Geo": "geo-bangsar",
            "beds": 2, "priceRent": 5000,
        }
        lead = {"_id": "lead-1", "TransactionType": ["Rent/Let"],
                "Geo": ["geo-bangsar"], "bedroomsMin": 2}

        def records(_base, object_type, constraints=None, **_kwargs):
            return {"listing": [existing], "lead": [lead], "match": []}.get(
                object_type, []
            )

        parsed = {"type": "listing", "geo_name": "Bangsar",
                  "transaction_types": ["Rent/Let"], "price_rent": 5000,
                  "beds": 2}
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "resolve_or_create_proposing_agent", return_value={
                 "status": "existing", "user_id": "user-1",
             }), patch.object(importer, "find_import_matches",
                              side_effect=FIND_IMPORT_MATCHES), \
             patch.object(importer.rentee_app, "_bubble_records",
                              side_effect=records), \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="match-1") as create:
            result = importer.process_whatsapp_import(
                raw, geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(result["status"], "duplicate")
        self.assertTrue(result["confirmation"].startswith(
            "This listing already exists.\n\nFound 1 matching lead:"
        ), result["confirmation"])
        create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "match",
            {"lead": "lead-1", "listing": "listing-existing"},
        )

    def test_existing_match_is_not_duplicated(self):
        with patch.object(
            importer.rentee_app, "_bubble_records", return_value=[{"_id": "match-1"}]
        ), patch.object(importer.rentee_app, "_bubble_create") as create:
            importer.create_missing_match_records(
                "lead", "lead-1", [{"_id": "listing-1"}], "live"
            )
        create.assert_not_called()

    def test_multiple_matches_create_one_record_per_unique_pair(self):
        matches = [
            {"_id": "listing-1"}, {"_id": "listing-1"}, {"_id": "listing-2"},
        ]
        with patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            importer.create_missing_match_records("lead", "lead-1", matches, "live")
        self.assertEqual([call.args[2] for call in create.call_args_list], [
            {"lead": "lead-1", "listing": "listing-1"},
            {"lead": "lead-1", "listing": "listing-2"},
        ])

    def test_new_listing_match_creates_match_with_correct_direction(self):
        with patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            importer.create_missing_match_records(
                "listing", "listing-new", [{"_id": "lead-existing"}], "live"
            )
        create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "match",
            {"lead": "lead-existing", "listing": "listing-new"},
        )

    def test_new_lead_matches_existing_listings_and_appends_confirmation(self):
        parsed = {
            "type": "lead", "geo_names": ["Bangsar"],
            "preferred_development_names": [], "transaction_types": ["Rent/Let"],
            "property_types": [], "budget": 5000, "bedrooms_min": 2,
        }
        match = {
            "_id": "listing-1", "development": "dev-one", "beds": 2,
            "TransactionType": ["Rent/Let"],
            "priceRent": 4800, "ProposingAgentName": "Jane Tee",
            "ProposingAgentNumber": "60173262281",
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create", return_value="lead-1"), \
             patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer, "find_import_matches", return_value=[match]) as find:
            result = importer.process_whatsapp_import(
                "lead", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(result["matches"], [match])
        self.assertIn(
            "See here: https://www.rentee.asia/lead/lead-1\n\n"
            "Found 1 matching listing:",
            result["confirmation"],
        )
        self.assertIn(
            "Found 1 matching listing:\n\n"
            "One Menerung — 2 bed, RM4,800/month\n"
            "https://www.rentee.asia/listing/listing-1",
            result["confirmation"],
        )
        self.assertNotIn("Jane Tee", result["confirmation"])
        self.assertEqual(find.call_args.args[0], "lead")
        self.assertEqual(find.call_args.args[1]["_id"], "lead-1")

    def test_new_listing_matches_existing_leads_and_appends_confirmation(self):
        parsed = {
            "type": "listing", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "price_rent": 5000, "beds": 2,
        }
        match = {
            "_id": "lead-1", "name": "Alex WTR One Menerung",
            "owner": "user-alex",
            "TransactionType": ["Rent/Let"],
            "budgetRent": 5200, "bedroomsMin": 2,
            "ProposedAgentNameLead": "Alex Goh",
            "ProposedAgentNumberLead": "60164697992",
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1"), \
             patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer.rentee_app, "bubble", return_value={
                 "name": "Alex Goh", "phone": "+60164697992",
             }) as get_owner, \
             patch.object(importer, "find_import_matches", return_value=[match]) as find:
            result = importer.process_whatsapp_import(
                "listing", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(result["matches"], [match])
        self.assertIn(
            "See here: https://www.rentee.asia/listing/listing-1\n\n"
            "Found 1 matching lead:",
            result["confirmation"],
        )
        self.assertIn(
            "Found 1 matching lead:\n\n"
            "Alex WTR One Menerung — 2+ bed, budget RM5,200\n"
            "Agent: Alex Goh\n"
            "Phone: +60164697992\n"
            "https://www.rentee.asia/lead/lead-1",
            result["confirmation"],
        )
        get_owner.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/user/user-alex"
        )
        self.assertEqual(find.call_args.args[0], "listing")
        self.assertEqual(find.call_args.args[1]["_id"], "listing-1")

    def test_match_notification_plural_spacing_transaction_and_zero_omission(self):
        lead = {
            "TransactionType": ["Rent/Let", "Buy/Sell"],
            "budgetRent": 5000, "budgetBuy": 900000,
        }
        listings = [
            {
                "_id": "listing-rent", "development": "dev-one",
                "TransactionType": ["Rent/Let"], "beds": 3,
                "priceRent": 5500, "priceSale": 900000,
            },
            {
                "_id": "listing-empty", "development": "dev-serai",
                "TransactionType": ["Rent/Let"], "beds": 0, "priceRent": 0,
            },
        ]
        rendered = importer.format_import_matches(
            "lead", lead, listings, DEVELOPMENTS
        )
        self.assertEqual(rendered, (
            "Found 2 matching listings:\n\n"
            "One Menerung — 3 bed, RM5,500/month\n"
            "https://www.rentee.asia/listing/listing-rent\n\n"
            "Serai\n"
            "https://www.rentee.asia/listing/listing-empty"
        ))
        self.assertNotIn("900,000", rendered)

    def test_matching_listing_includes_owner_name_and_omits_empty_phone(self):
        lead = {"TransactionType": ["Rent/Let"], "budgetRent": 5000}
        listing = {
            "_id": "listing-1", "development": "dev-one", "owner": "user-james",
            "TransactionType": ["Rent/Let"], "beds": 2, "priceRent": 5000,
        }
        with patch.object(importer.rentee_app, "bubble", return_value={
            "name": "James", "phone": "",
        }):
            rendered = importer.format_import_matches(
                "lead", lead, [listing], DEVELOPMENTS, "live"
            )
        self.assertIn("Agent: James", rendered)
        self.assertNotIn("Phone:", rendered)

    def test_no_matches_append_created_lead_link_to_confirmation(self):
        parsed = {
            "type": "lead", "geo_names": ["Bangsar"],
            "preferred_development_names": [], "transaction_types": ["Rent/Let"],
            "property_types": [], "budget": 5000,
        }
        expected = importer._confirmation(
            parsed,
            [{"matched": True, "id": "geo-bangsar", "name": "Bangsar"}],
            [],
        )
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create", return_value="lead-1"):
            result = importer.process_whatsapp_import(
                "lead", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(result["matches"], [])
        self.assertEqual(
            result["confirmation"],
            expected + "\n\nSee here: https://www.rentee.asia/lead/lead-1",
        )

    def test_unsupported_property_type_is_removed(self):
        parsed = self.parse_as({
            "type": "listing", "transaction_types": ["Buy/Sell"],
            "property_type": "Villa", "price_sale": 1000000,
        })
        self.assertNotIn("property_type", parsed)
        self.assertNotIn("propertyType", importer.build_listing_payload(parsed, None, None))

    def test_ambiguous_fuzzy_development_resolves_neither(self):
        records = [
            {"_id": "a", "name": "Alam Sanctuary One"},
            {"_id": "b", "name": "Alam Sanctuary Two"},
        ]
        result = resolver.resolve_development_name("Alam Sanctuary", records)
        self.assertFalse(result["matched"])

    def test_unresolved_development_allows_creation_without_relationship(self):
        parsed = {
            "type": "listing", "geo_name": "Bangsar",
            "development_name": "Alam Sanctuary",
            "transaction_types": ["Rent/Let"], "price_rent": 5000, "beds": 2,
        }
        with patch.object(resolver, "verify_development_candidate", return_value={
            "status": "not_found", "raw_name": "Alam Sanctuary",
        }):
            result, create = self.process_as(parsed)
        payload = create.call_args.args[2]
        self.assertNotIn("development", payload)
        self.assertEqual(result["unresolved_development_names"], ["Alam Sanctuary"])
        self.assertEqual(payload["Geo"], "geo-bangsar")

    def test_case_and_punctuation_normalized_exact(self):
        result = resolver.resolve_development_name("  ONE  MENERUNG!!! ", DEVELOPMENTS)
        self.assertTrue(result["matched"])
        self.assertEqual(result["method"], "normalized_exact")

    def test_process_reuses_bubble_environment_and_object_type(self):
        parsed = {
            "type": "lead", "geo_names": ["Bangsar"],
            "preferred_development_names": [], "transaction_types": ["Rent/Let"],
            "property_types": ["Condo"], "budget": 8000, "bedrooms_min": 3,
        }
        _result, create = self.process_as(parsed)
        self.assertEqual(create.call_args.args[:2],
                         ("https://www.rentee.asia/api/1.1", "lead"))

    def test_strong_lead_markers(self):
        for text in (
            "WTR Bangsar 3 bed RM8k",
            "Want To Buy landed house RM3m",
        ):
            with self.subTest(text=text):
                result = importer.detect_import_intent(text)
                self.assertEqual(result["intent"], "lead_import")
                self.assertEqual(result["confidence"], 1.0)

    def test_strong_listing_markers(self):
        for text in (
            "WTS One Menerung 3 bed RM1.8m",
            "WTL Mont Kiara condo RM5,500",
        ):
            with self.subTest(text=text):
                result = importer.detect_import_intent(text)
                self.assertEqual(result["intent"], "listing_import")
                self.assertEqual(result["confidence"], 1.0)

    def test_structured_listing_without_abbreviation(self):
        result = importer.detect_import_intent(
            "One Menerung\n3 bedrooms\nFor rent RM8,500\nAvailable immediately"
        )
        self.assertEqual(result["intent"], "listing_import")
        self.assertGreaterEqual(result["confidence"], importer.IMPORT_ROUTING_THRESHOLD)

    def test_normal_property_chat_is_not_imported(self):
        for text in (
            "Show me 3 bed condos in Bangsar under RM8k",
            "What do you have in One Menerung?",
            "Would you recommend Bangsar or Mont Kiara?",
            "Can you find me a 3 bed in KLCC?",
            "Is RM8k enough for Bangsar?",
        ):
            with self.subTest(text=text), patch.object(
                importer.rentee_app.client.responses, "create"
            ) as create:
                self.assertEqual(
                    importer.detect_import_intent(text)["intent"], "normal_chat"
                )
                create.assert_not_called()

    def test_borderline_looking_for_defaults_to_normal_chat(self):
        with patch.object(importer.rentee_app.client.responses, "create") as create:
            result = importer.detect_import_intent("Looking for something in Bangsar")
        self.assertEqual(result["intent"], "normal_chat")
        create.assert_not_called()

    def test_real_structured_requirement_is_lead_import(self):
        message = """Want To Rent
- China Family Tenant (husband work in corporate, wife work in International school & 2 daughter)
- need 3 bedroom
- 1300sf-1500sf
- 2 daughter study in Garden International school.
- Want nearby condo like inspirasi, mk Astana, ceriaan kiara, sefina
- budget Rm4k-5k.
- 1 year tenancy first.

Alex Goh (E2265)
Polygon Properties
016-4697992"""
        result = importer.detect_import_intent(message)
        self.assertEqual(result["intent"], "lead_import")
        self.assertIn("strong_marker:Want To Rent", result["signals"])

    def test_forced_import_type_uses_type_specific_parser_schema(self):
        response = SimpleNamespace(status="completed", output_text=json.dumps(
            full_model_output(type="lead", transaction_types=["Rent/Let"])
        ))
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create:
            parsed = importer.parse_forwarded_message("WTR Bangsar", import_type="lead")
        schema = create.call_args.kwargs["text"]["format"]["schema"]
        self.assertEqual(schema["properties"]["type"]["enum"], ["lead"])
        self.assertIn("do not classify it", create.call_args.kwargs["input"])
        self.assertEqual(parsed["type"], "lead")

    def test_process_passes_forced_import_type_to_parser(self):
        parsed = {
            "type": "lead", "geo_names": [], "preferred_development_names": [],
            "transaction_types": ["Rent/Let"], "property_types": [],
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed) as parse, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="lead-1"):
            importer.process_whatsapp_import(
                "WTR", import_type="lead", geo_records=[], development_records=[]
            )
        parse.assert_called_once_with("WTR", import_type="lead")

    def test_whatsapp_import_gate_returns_before_search_routing(self):
        app = importer.rentee_app
        text = "Want To Rent\n- China Family Tenant\n- need 3 bedroom\n- budget RM4k-5k"
        message = {
            "id": "wamid.import-1", "from": "60123456789", "type": "text",
            "text": {"body": text},
        }
        imported = {
            "status": "processed", "type": "lead", "bubble_id": "lead-1",
            "confirmation": "Added lead.",
        }
        with patch.object(app, "whatsapp_typing_keepalive", return_value=MagicMock()), \
             patch.object(app, "_stop_whatsapp_typing"), \
             patch.object(app, "resolve_whatsapp_user", return_value={"_id": "user-1"}), \
             patch.object(importer, "process_whatsapp_import", return_value=imported) as process, \
             patch.object(app, "send_whatsapp_text") as send, \
             patch.object(app, "find_active_conversation_by_phone") as conversations, \
             patch.object(app, "find_nearby_places") as nearby, \
             patch.object(app, "advance_property_search") as advance:
            app._process_whatsapp_message(message)
        process.assert_called_once_with(text, import_type="lead", bubble_env="live")
        send.assert_called_once_with("60123456789", "Added lead.")
        conversations.assert_not_called()
        nearby.assert_not_called()
        advance.assert_not_called()

    def test_existing_development_skips_web_and_creation(self):
        parsed = {
            "type": "listing", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "price_rent": 8500, "beds": 3,
        }
        with patch.object(resolver, "verify_development_candidate") as verify, \
             patch.object(resolver, "create_verified_development") as create_development:
            result, create_listing = self.process_as(parsed)
        verify.assert_not_called()
        create_development.assert_not_called()
        self.assertEqual(create_listing.call_args.args[2]["development"], "dev-one")
        self.assertEqual(result["resolved_development"]["id"], "dev-one")

    def test_verified_missing_development_is_created_with_exact_fields(self):
        parsed = {
            "type": "lead", "geo_names": [],
            "preferred_development_names": ["Sefina"],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
            "budget": 5000, "bedrooms_min": 3,
        }
        verification = {
            "status": "verified", "raw_name": "Sefina",
            "canonical_name": "Sefina Mont Kiara", "geo_name": "Mont Kiara",
            "verification_url": "https://example.com/sefina", "confidence": 0.97,
        }
        geos = GEOS + [{"_id": "geo-mk", "Name": "Mont Kiara"}]
        create = MagicMock(side_effect=["dev-sefina", "lead-1"])
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate", return_value=verification), \
             patch.object(resolver, "_fresh_development_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create", create):
            result = importer.process_whatsapp_import(
                "Want To Rent near Sefina", geo_records=geos,
                development_records=DEVELOPMENTS,
            )
        development_call, lead_call = create.call_args_list
        self.assertEqual(development_call.args[1], "condo")
        self.assertEqual(development_call.args[2], {
            "Name": "Sefina Mont Kiara", "Geo": "geo-mk",
            "verification_status": "Verified", "source": "WhatsApp Import",
            "verification_url": "https://example.com/sefina",
        })
        self.assertEqual(lead_call.args[2]["preferredDevelopments"], ["dev-sefina"])
        self.assertEqual(lead_call.args[2]["Geo"], ["geo-mk"])
        self.assertEqual(result["created_developments"][0]["id"], "dev-sefina")

    def test_verified_development_verbose_geo_uses_existing_canonical_geo(self):
        parsed = {
            "type": "listing", "development_name": "Ceriaan Kiara",
            "transaction_types": ["Rent/Let"], "price_rent": 5000,
        }
        verification = {
            "status": "verified", "raw_name": "Ceriaan Kiara",
            "canonical_name": "Ceriaan Kiara",
            "geo_name": "Mont' Kiara (Jalan Kiara 3), Kuala Lumpur",
            "verification_url": "https://example.com/ceriaan",
            "confidence": 0.90, "reason": "credible_match",
        }
        geos = GEOS + [{"_id": "geo-mk", "Name": "Mont Kiara"}]
        create = MagicMock(side_effect=["dev-ceriaan", "listing-1"])
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate",
                          return_value=verification), \
             patch.object(resolver, "_fresh_development_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create", create):
            result = importer.process_whatsapp_import(
                "Ceriaan Kiara", geo_records=geos,
                development_records=DEVELOPMENTS,
            )
        development_payload = create.call_args_list[0].args[2]
        listing_payload = create.call_args_list[1].args[2]
        self.assertEqual(development_payload["Geo"], "geo-mk")
        self.assertEqual(development_payload["Name"], "Ceriaan Kiara")
        self.assertEqual(listing_payload["development"], "dev-ceriaan")
        self.assertEqual(listing_payload["Geo"], "geo-mk")
        self.assertEqual(result["created_developments"][0]["id"], "dev-ceriaan")

    def test_verified_development_geo_fallback_creates_and_attaches_to_listing(self):
        parsed = {
            "type": "listing", "development_name": "Lakeview Residences",
            "transaction_types": ["Rent/Let"], "price_rent": 5000,
        }
        verification = {
            "status": "verified", "raw_name": "Lakeview Residences",
            "canonical_name": "Lakeview Residences",
            "geo_name": "Bangsar Township",
            "verification_url": "https://example.com/lakeview",
            "confidence": 0.96, "reason": "credible_match",
        }
        fallback = [{
            "matched": True, "id": "geo-bangsar", "name": "Bangsar",
            "record": GEOS[0], "method": "normalized_exact",
        }]
        create = MagicMock(side_effect=["dev-lakeview", "listing-1"])
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate",
                          return_value=verification), \
             patch.object(importer, "verify_geo_reference",
                          return_value=fallback) as verify_geo, \
             patch.object(resolver, "_fresh_development_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create", create):
            result = importer.process_whatsapp_import(
                "Lakeview listing", geo_records=GEOS,
                development_records=DEVELOPMENTS,
            )
        verify_geo.assert_called_once_with(
            "Bangsar Township", GEOS, unittest.mock.ANY, single=True,
        )
        development_payload = create.call_args_list[0].args[2]
        listing_payload = create.call_args_list[1].args[2]
        self.assertEqual(development_payload["Geo"], "geo-bangsar")
        self.assertEqual(listing_payload["development"], "dev-lakeview")
        self.assertEqual(listing_payload["Geo"], "geo-bangsar")
        self.assertEqual(result["resolved_development"]["id"], "dev-lakeview")

    def test_verified_canonical_development_is_reused_without_post(self):
        parsed = {
            "type": "listing", "development_name": "Sefina",
            "transaction_types": ["Rent/Let"], "price_rent": 5000,
        }
        records = DEVELOPMENTS + [
            {"_id": "dev-sefina", "Name": "Sefina Mont Kiara", "Geo": "geo-mk"}
        ]
        geos = GEOS + [{"_id": "geo-mk", "Name": "Mont Kiara"}]
        verification = {
            "status": "verified", "canonical_name": "Sefina Mont Kiara",
            "geo_name": "Mont Kiara", "verification_url": "https://example.com/sefina",
            "confidence": 0.97,
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate", return_value=verification), \
             patch.object(resolver, "create_verified_development") as create_development, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create, \
             patch("builtins.print") as log:
            result = importer.process_whatsapp_import(
                "Sefina", geo_records=geos, development_records=records
            )
        create_development.assert_not_called()
        self.assertEqual(create.call_args.args[1], "listing")
        self.assertEqual(create.call_args.args[2]["development"], "dev-sefina")
        self.assertEqual(result["created_developments"], [])

    def test_ambiguous_verification_leaves_development_unresolved(self):
        parsed = {
            "type": "listing", "development_name": "Sunshine Residence",
            "transaction_types": ["Buy/Sell"], "price_sale": 900000,
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate", return_value={
                 "status": "ambiguous", "raw_name": "Sunshine Residence",
                 "candidates": [{"name": "A"}, {"name": "B"}], "confidence": 0.45,
             }), patch.object(resolver, "create_verified_development") as create_dev, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create, \
             patch("builtins.print") as log:
            result = importer.process_whatsapp_import(
                "Sunshine Residence", geo_records=GEOS,
                development_records=DEVELOPMENTS,
            )
        create_dev.assert_not_called()
        self.assertNotIn("development", create.call_args.args[2])
        self.assertEqual(result["unresolved_development_names"], ["Sunshine Residence"])

    def test_verified_development_with_unresolved_geo_is_not_created(self):
        parsed = {
            "type": "listing", "development_name": "Sefina",
            "transaction_types": ["Rent/Let"], "price_rent": 5000,
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate", return_value={
                 "status": "verified", "canonical_name": "Sefina Mont Kiara",
                 "geo_name": "Unknown Area", "verification_url": "https://example.com/sefina",
                 "confidence": 0.97,
             }), patch.object(resolver, "create_verified_development") as create_dev, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create, \
             patch("builtins.print") as log:
            result = importer.process_whatsapp_import(
                "Sefina", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        create_dev.assert_not_called()
        self.assertNotIn("development", create.call_args.args[2])
        self.assertEqual(result["unresolved_development_names"], ["Sefina"])
        rendered = " ".join(str(call) for call in log.call_args_list)
        self.assertIn("reason=geo_unresolved", rendered)
        self.assertIn("geo_candidate='Unknown Area'", rendered)

    def test_multiple_developments_mix_created_and_unresolved(self):
        parsed = {
            "type": "lead", "geo_names": [],
            "preferred_development_names": [
                "Inspirasi", "MK Astana", "Ceriaan Kiara", "Sefina", "Sefina",
            ],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
            "budget": 5000, "bedrooms_min": 3,
        }
        outcomes = {
            "Inspirasi": {"status": "verified", "canonical_name": "Inspirasi Mont Kiara",
                          "geo_name": "Mont Kiara", "verification_url": "https://x/inspirasi"},
            "MK Astana": {"status": "ambiguous", "candidates": []},
            "Ceriaan Kiara": {"status": "not_found"},
            "Sefina": {"status": "verified", "canonical_name": "Sefina Mont Kiara",
                       "geo_name": "Mont Kiara", "verification_url": "https://x/sefina"},
        }
        def verify(name, _context, _env):
            return outcomes[name]
        created_ids = iter(["dev-inspirasi", "dev-sefina", "lead-1"])
        geos = GEOS + [{"_id": "geo-mk", "Name": "Mont Kiara"}]
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(resolver, "verify_development_candidate", side_effect=verify) as verifier, \
             patch.object(resolver, "_fresh_development_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create",
                          side_effect=lambda *_args: next(created_ids)) as create:
            result = importer.process_whatsapp_import(
                "structured lead", geo_records=geos, development_records=DEVELOPMENTS
            )
        self.assertEqual(verifier.call_count, 4)
        lead_payload = create.call_args_list[-1].args[2]
        self.assertEqual(lead_payload["preferredDevelopments"],
                         ["dev-inspirasi", "dev-sefina"])
        self.assertEqual(lead_payload["Geo"], ["geo-mk"])
        self.assertEqual(result["unresolved_development_names"],
                         ["MK Astana", "Ceriaan Kiara"])
        self.assertEqual(len(result["created_developments"]), 2)

    def test_school_is_context_not_geo_in_parser(self):
        output = full_model_output(
            type="lead", geo_names=[],
            preferred_development_names=["Inspirasi", "Sefina"],
            transaction_types=["Rent/Let"], property_types=["Condo"],
        )
        response = SimpleNamespace(status="completed", output_text=json.dumps(output))
        text = ("Daughters study at Garden International School. "
                "Looking for Inspirasi or Sefina.")
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create:
            parsed = importer.parse_forwarded_message(text, import_type="lead")
        self.assertNotIn("Garden International School", parsed["geo_names"])
        prompt = create.call_args.kwargs["input"]
        self.assertIn("Never put schools, workplaces", prompt)
        context = importer._verification_context(parsed, text)
        self.assertIn("Garden International School", context["raw_text"])

    def test_parser_prompt_keeps_developments_out_of_geo_names(self):
        output = full_model_output(
            type="lead", geo_names=[],
            preferred_development_names=[
                "Inspirasi", "MK Astana", "Ceriaan Kiara", "Sefina",
            ],
            transaction_types=["Rent/Let"], property_types=["Condo"],
        )
        response = SimpleNamespace(status="completed", output_text=json.dumps(output))
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create:
            parsed = importer.parse_forwarded_message(
                "Looking to rent in Inspirasi, MK Astana, Ceriaan Kiara or Sefina.",
                import_type="lead",
            )
        self.assertEqual(parsed["geo_names"], [])
        self.assertEqual(parsed["preferred_development_names"], output[
            "preferred_development_names"
        ])
        prompt = create.call_args.kwargs["input"]
        self.assertIn("A named Development is not a Geo", prompt)
        self.assertIn("geo_names must be empty", prompt)

    def test_web_verifier_reuses_responses_web_search_and_context(self):
        output = {
            "status": "verified", "canonical_name": "Sefina Mont Kiara",
            "geo_name": "Mont Kiara",
            "verification_url": "https://developer.example/sefina",
            "confidence": 0.97, "reason": "credible_match",
        }
        response = SimpleNamespace(output_text=json.dumps(output))
        context = {
            "property_types": ["Condo"],
            "raw_text": "Daughters study at Garden International School",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create:
            result = resolver.verify_development_candidate("Sefina", context)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(create.call_args.kwargs["tools"], [{"type": "web_search"}])
        prompt = create.call_args.kwargs["input"]
        self.assertIn("Sefina", prompt)
        self.assertIn("Garden International School", prompt)
        self.assertIn("must never be returned as a Development or residential Geo", prompt)
        self.assertIn("clean official property name", prompt)
        self.assertIn("without aliases or explanatory text in brackets or parentheses", prompt)

    def test_verified_result_requires_credible_match_reason(self):
        output = {
            "status": "verified", "canonical_name": "Sunshine Residence",
            "geo_name": "Kuala Lumpur",
            "verification_url": "https://weak.example/sunshine",
            "confidence": 0.96, "reason": "no_credible_property_match",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("Sunshine Residence", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "schema_validation_failed")

    def test_verifier_reports_no_search_results_reason(self):
        output = {
            "status": "not_found", "canonical_name": None, "geo_name": None,
            "verification_url": None, "confidence": 0.0,
            "reason": "no_credible_property_match",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("Ceriaan Kiara", {})
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["raw_name"], "Ceriaan Kiara")
        self.assertEqual(result["reason"], "no_credible_property_match")

    def test_verifier_reports_pages_without_property_candidate(self):
        output = {
            "status": "not_found", "canonical_name": None, "geo_name": None,
            "verification_url": None, "confidence": 0.2,
            "reason": "no_credible_property_match",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("MK Astana", {})
        self.assertEqual(result["reason"], "no_credible_property_match")

    def test_verifier_reports_multiple_plausible_candidate_names(self):
        output = {
            "status": "ambiguous", "canonical_name": None, "geo_name": None,
            "verification_url": None,
            "confidence": 0.45,
            "reason": "multiple_plausible_candidates",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("Sunshine Residence", {})
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(result["reason"], "multiple_plausible_candidates")

    def test_verifier_reports_confidence_below_threshold(self):
        output = {
            "status": "verified", "canonical_name": "Inspirasi Mont Kiara",
            "geo_name": "Mont Kiara", "verification_url": "https://one.test",
            "confidence": 0.84, "reason": "credible_match",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("Inspirasi", {})
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["reason"],
                         "verification_confidence_below_threshold")

    def test_verifier_invalid_json_is_error(self):
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text="not JSON"),
        ), patch("builtins.print") as log:
            result = resolver.verify_development_candidate("Inspirasi", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "invalid_json")
        self.assertIn("reason='invalid_json'", " ".join(
            str(call) for call in log.call_args_list
        ))

    def test_verifier_missing_required_field_is_error(self):
        output = {
            "status": "verified", "canonical_name": "Inspirasi Mont Kiara",
            "geo_name": "Mont Kiara", "confidence": 0.96,
            "reason": "credible_match",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("Inspirasi", {})
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "missing_verification_url")

    def test_valid_not_found_is_not_a_verifier_error(self):
        output = {
            "status": "not_found", "canonical_name": None, "geo_name": None,
            "verification_url": None, "confidence": 0.1,
            "reason": "no_credible_property_match",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = resolver.verify_development_candidate("Not Real Place", {})
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(result["reason"], "no_credible_property_match")

    def test_context_is_passed_to_web_model_without_raw_log_dump(self):
        output = {
            "status": "not_found", "canonical_name": None, "geo_name": None,
            "verification_url": None, "confidence": 0.1,
            "reason": "no_credible_property_match",
        }
        context = {
            "geo_names": ["Mont Kiara"], "property_types": ["Condo"],
            "other_development_names": ["Inspirasi", "MK Astana", "Sefina"],
            "raw_text": "Daughters study at Garden International School. PRIVATE NOTE",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ) as create, patch("builtins.print") as log:
            resolver.verify_development_candidate("Sefina", context)
        prompt = create.call_args.kwargs["input"]
        self.assertIn("Sefina", prompt)
        self.assertIn("Mont Kiara", prompt)
        self.assertIn("Inspirasi", prompt)
        self.assertIn("Garden International School", prompt)
        rendered = " ".join(str(call) for call in log.call_args_list)
        self.assertIn("action=web_model_check", rendered)
        self.assertIn("status=not_found", rendered)
        self.assertNotIn("PRIVATE NOTE", rendered)

    def test_development_creation_race_requeries_once_and_reuses(self):
        geo = importer.resolve_geo_name("Bangsar", GEOS)
        raced_record = {"_id": "dev-raced", "Name": "Sefina Mont Kiara",
                        "Geo": "geo-bangsar"}
        with patch.object(
            resolver, "_fresh_development_records", side_effect=[[], [raced_record]]
        ) as fresh, patch.object(
            importer.rentee_app, "_bubble_create", side_effect=RuntimeError("duplicate")
        ) as create:
            result = resolver.create_verified_development(
                "Sefina Mont Kiara", geo, "https://example.com/sefina"
            )
        self.assertEqual(result["id"], "dev-raced")
        self.assertEqual(fresh.call_count, 2)
        create.assert_called_once()

    def test_malaysian_phone_formats_and_email_are_deterministic(self):
        variants = ("016-4697992", "+60 16-469 7992", "60164697992")
        self.assertEqual([importer.normalize_phone_number(v) for v in variants],
                         ["60164697992"] * 3)
        expected = "whatsapp-60164697992@users.rentee.internal"
        self.assertEqual(importer.build_internal_user_email("60164697992"), expected)
        self.assertEqual(importer.build_internal_user_email("60164697992"), expected)

    def test_ren_normalization_uses_digits_only(self):
        self.assertEqual(
            [importer._normalized_ren(value) for value in (
                "REN74405", "REN 74405", "74405",
            )],
            ["74405", "74405", "74405"],
        )

    def test_existing_proposing_agent_is_reused_without_create(self):
        user = {"_id": "user-agent", "phone": "60164697992", "name": "Alex Goh",
                "REN": "E2265", "email": "alex@example.com"}
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_create") as create, \
             patch.object(importer.rentee_app, "_bubble_patch") as patch_user:
            result = importer.resolve_or_create_proposing_agent(
                "Alex Goh", "016-4697992", "E2265")
        self.assertEqual(result["user_id"], "user-agent")
        create.assert_not_called()
        patch_user.assert_not_called()

    def test_new_proposing_agent_creation_payload(self):
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="user-new") as create:
            result = importer.resolve_or_create_proposing_agent(
                "Alex Goh", "016-4697992", "E2265")
        self.assertEqual(create.call_args.args, (
            "https://www.rentee.asia/api/1.1", "user", {
                "phone": "60164697992", "name": "Alex Goh", "REN": "2265",
                "email": "whatsapp-60164697992@users.rentee.internal",
            },
        ))
        self.assertEqual(result["user_id"], "user-new")

    def test_existing_agency_resolves_with_case_and_punctuation_variation(self):
        agencies = [{"_id": "agency-propnex", "name": "PropNex Realty Sdn Bhd"}]
        with patch.object(importer.rentee_app, "_bubble_records",
                          return_value=agencies), \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            exact = importer._resolve_or_create_agency(
                "PropNex Realty Sdn Bhd", "live"
            )
            variation = importer._resolve_or_create_agency(
                "PROPNEX REALTY SDN. BHD.", "live"
            )
        self.assertEqual(exact, "agency-propnex")
        self.assertEqual(variation, "agency-propnex")
        create.assert_not_called()

    def test_new_agency_is_created_and_assigned_to_new_user(self):
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create",
                          side_effect=["agency-kommons", "user-marcus"]) as create:
            result = importer.resolve_or_create_proposing_agent(
                "Marcus Yeoh", "+6017-4081131", "REN50605",
                source_agency_name="Kommons Realty Sdn Bhd",
            )
        self.assertEqual(create.call_args_list[0].args, (
            "https://www.rentee.asia/api/1.1", "Agency",
            {"name": "Kommons Realty Sdn Bhd"},
        ))
        self.assertEqual(create.call_args_list[1].args[2]["Agency"], "agency-kommons")
        self.assertNotIn("agency", create.call_args_list[1].args[2])
        self.assertEqual(result["user"]["Agency"], "agency-kommons")

    def test_existing_user_empty_agency_is_enriched_without_touching_text_field(self):
        user = {"_id": "user-jasmine", "phone": "60123456789",
                "name": "Jasmine", "REN": "REN12345", "agency": "Legacy Text"}
        agencies = [{"_id": "agency-1", "name": "Kommons Realty Sdn Bhd"}]
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_records",
                          return_value=agencies), \
             patch.object(importer.rentee_app, "_bubble_patch") as update:
            result = importer.resolve_or_create_proposing_agent(
                "Jasmine", "0123456789", "REN12345",
                source_agency_name="Kommons Realty Sdn Bhd",
            )
        update.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/user/user-jasmine",
            {"Agency": "agency-1"},
        )
        self.assertEqual(result["user"]["agency"], "Legacy Text")
        self.assertEqual(result["user"]["Agency"], "agency-1")

    def test_existing_same_agency_is_unchanged_and_different_agency_conflicts(self):
        agencies = [{"_id": "agency-new", "name": "Kommons Realty Sdn Bhd"}]
        for existing_agency, conflict in (
            ("agency-new", False), ("agency-old", True),
        ):
            user = {"_id": "user-1", "phone": "60123456789",
                    "name": "Jasmine", "REN": "REN12345",
                    "Agency": existing_agency, "agency": "Legacy Text"}
            with self.subTest(existing_agency=existing_agency), \
                 patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                              return_value=[user]), \
                 patch.object(importer.rentee_app, "_bubble_records",
                              return_value=agencies), \
                 patch.object(importer.rentee_app, "_bubble_patch") as update, \
                 patch("builtins.print") as log:
                result = importer.resolve_or_create_proposing_agent(
                    "Jasmine", "0123456789", "REN12345",
                    source_agency_name="Kommons Realty Sdn Bhd",
                )
            update.assert_not_called()
            self.assertEqual(result["user"]["Agency"], existing_agency)
            self.assertEqual(result["user"]["agency"], "Legacy Text")
            rendered = " ".join(str(call) for call in log.call_args_list)
            self.assertEqual("conflict=Agency" in rendered, conflict)

    def test_missing_source_agency_name_makes_no_agency_change(self):
        user = {"_id": "user-1", "phone": "60123456789", "name": "Jasmine"}
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_records") as agencies, \
             patch.object(importer.rentee_app, "_bubble_patch") as update:
            importer.resolve_or_create_proposing_agent(
                "Jasmine", "0123456789", None
            )
        agencies.assert_not_called()
        update.assert_not_called()

    def test_existing_user_survives_agency_lookup_and_creation_failures(self):
        user = {"_id": "user-gwen", "phone": "60174156107", "name": "Gwen"}
        cases = (
            (RuntimeError("lookup down"), None, "lookup_failed"),
            ([], RuntimeError("create down"), "create_failed"),
        )
        for agency_records, create_error, action in cases:
            with self.subTest(action=action), \
                 patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                              return_value=[user]), \
                 patch.object(importer.rentee_app, "_bubble_records",
                              side_effect=agency_records if isinstance(
                                  agency_records, Exception) else None,
                              return_value=agency_records if isinstance(
                                  agency_records, list) else None), \
                 patch.object(importer.rentee_app, "_bubble_create",
                              side_effect=create_error) as create, \
                 patch("builtins.print") as log:
                result = importer.resolve_or_create_proposing_agent(
                    "Gwen", "017-4156107", None,
                    source_agency_name="Prestige Realty",
                )
            self.assertEqual(result["status"], "existing")
            self.assertEqual(result["user_id"], "user-gwen")
            self.assertEqual(result["user"], user)
            self.assertIn(action, " ".join(str(call) for call in log.call_args_list))
            if action == "lookup_failed":
                create.assert_not_called()

    def test_existing_user_survives_agency_assignment_failure(self):
        user = {"_id": "user-gwen", "phone": "60174156107", "name": "Gwen"}
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_records", return_value=[{
                 "_id": "agency-1", "name": "Prestige Realty",
             }]), patch.object(importer.rentee_app, "_bubble_patch",
                              side_effect=RuntimeError("assignment down")), \
             patch("builtins.print") as log:
            result = importer.resolve_or_create_proposing_agent(
                "Gwen", "017-4156107", None,
                source_agency_name="Prestige Realty",
            )
        self.assertEqual(result["status"], "existing")
        self.assertEqual(result["user_id"], "user-gwen")
        self.assertEqual(result["user"], user)
        rendered = " ".join(str(call) for call in log.call_args_list)
        self.assertIn("action=assign_failed", rendered)
        self.assertNotIn("WHATSAPP IMPORT AGENT] phone='60174156107' action=failed", rendered)

    def test_new_user_survives_agency_lookup_failure(self):
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_records",
                          side_effect=RuntimeError("agency down")), \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="user-new") as create:
            result = importer.resolve_or_create_proposing_agent(
                "Gwen", "017-4156107", None,
                source_agency_name="Prestige Realty",
            )
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["user_id"], "user-new")
        self.assertNotIn("Agency", create.call_args.args[2])

    def test_agency_failure_keeps_owner_and_duplicate_identity_downstream(self):
        parsed = {
            "type": "lead", "geo_names": ["Bangsar"],
            "preferred_development_names": [], "transaction_types": ["Rent/Let"],
            "property_types": ["Condo"], "source_agency_name": "Prestige Realty",
            "proposing_agent": {"name": "Gwen", "phone": "017-4156107", "ren": None},
        }
        user = {"_id": "user-gwen", "phone": "60174156107", "name": "Gwen"}
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_records",
                          side_effect=RuntimeError("agency down")), \
             patch.object(importer, "_find_duplicate_import", return_value=None) as duplicate, \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="lead-1") as create:
            result = importer.process_whatsapp_import(
                "WTR Bangsar", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        self.assertEqual(result["proposing_agent_user_id"], "user-gwen")
        self.assertEqual(create.call_args.args[2]["owner"], "user-gwen")
        self.assertEqual(duplicate.call_args.args[1], "user-gwen")

    def test_missing_ren_or_name_still_allows_user_creation(self):
        for index, name in enumerate(("Alex Goh", None)):
            with self.subTest(name=name), \
                 patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                              return_value=[]), \
                 patch.object(importer.rentee_app, "_bubble_create",
                              return_value=f"user-{index}") as create:
                importer.resolve_or_create_proposing_agent(
                    name, "0164697992", None)
            payload = create.call_args.args[2]
            self.assertEqual(payload["phone"], "60164697992")
            self.assertEqual(payload["email"],
                             "whatsapp-60164697992@users.rentee.internal")
            self.assertNotIn("REN", payload)
            self.assertEqual(payload.get("name"), name)

    def test_no_phone_skips_user_creation_and_import_fields(self):
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone") as find, \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            result = importer.resolve_or_create_proposing_agent(
                "Alex Goh", None, "E2265")
        self.assertEqual(result["status"], "no_phone")
        find.assert_not_called()
        create.assert_not_called()
        payload = importer.build_lead_payload(
            {"transaction_types": ["Rent/Let"], "property_types": []}, [], [], result)
        self.assertNotIn("ProposedAgentNumberLead", payload)

    def test_existing_user_missing_ren_is_patched_without_email_change(self):
        user = {"_id": "user-agent", "phone": "60164697992", "name": "Alex Goh",
                "REN": "", "email": "alex.real@example.com"}
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_patch") as patch_user:
            result = importer.resolve_or_create_proposing_agent(
                "Alex Goh", "0164697992", "E2265")
        patch_user.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/user/user-agent", {"REN": "2265"})
        self.assertEqual(result["ren"], "2265")
        self.assertEqual(result["user"]["email"], "alex.real@example.com")

    def test_conflicting_identity_values_are_preserved_and_logged(self):
        user = {"_id": "user-agent", "phone": "60164697992", "name": "Alex Goh",
                "REN": "E2265"}
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[user]), \
             patch.object(importer.rentee_app, "_bubble_patch") as patch_user, \
             patch("builtins.print") as log:
            result = importer.resolve_or_create_proposing_agent(
                "Alex", "0164697992", "E2266")
        patch_user.assert_not_called()
        self.assertEqual((result["name"], result["ren"]), ("Alex Goh", "2265"))
        self.assertTrue(any("conflict=REN" in str(call) for call in log.call_args_list))

    def test_real_agent_block_flows_to_user_and_lead_payload(self):
        text = """Want To Rent
- China Family Tenant
- need 3 bedroom
- budget Rm4k-5k.

Alex Goh (E2265)
Polygon Properties
016-4697992"""
        parsed = {
            "type": "lead", "geo_names": [], "preferred_development_names": [],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
            "budget": 5000, "bedrooms_min": 3,
            "proposing_agent": {
                "name": "Alex Goh", "phone": "016-4697992", "ren": "E2265",
            },
        }
        create = MagicMock(side_effect=["user-agent", "lead-1"])
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_records", return_value=[]), \
             patch.object(importer.rentee_app, "_bubble_create", create):
            result = importer.process_whatsapp_import(
                text, import_type="lead", geo_records=[], development_records=[])
        user_payload = create.call_args_list[0].args[2]
        lead_payload = create.call_args_list[1].args[2]
        self.assertEqual(user_payload["REN"], "2265")
        self.assertEqual(lead_payload["ProposedAgentNameLead"], "Alex Goh")
        self.assertEqual(lead_payload["ProposedAgentNumberLead"], "60164697992")
        self.assertEqual(lead_payload["owner"], "user-agent")
        self.assertEqual(lead_payload["source"], "whatsapp")
        self.assertNotIn("proposingAgentNameLead", lead_payload)
        self.assertNotIn("ProposingAgentName", lead_payload)
        self.assertNotIn("ProposingAgentNumber", lead_payload)
        self.assertEqual(result["proposing_agent_user_id"], "user-agent")

    def test_multiple_numbers_parser_selects_signature_agent_only(self):
        output = full_model_output(
            type="listing", development_name="One Menerung",
            transaction_types=["Rent/Let"], price_rent=8500,
            proposing_agent={
                "name": "Alex Goh", "phone": "016-4697992", "ren": "E2265",
            },
        )
        message = ("Owner contact 012-1111111\nOne Menerung for rent RM8,500\n\n"
                   "Agent Alex Goh (E2265)\nPolygon Properties\n016-4697992")
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(status="completed", output_text=json.dumps(output)),
        ):
            parsed = importer.parse_forwarded_message(message, import_type="listing")
        self.assertEqual(parsed["proposing_agent"], {
            "name": "Alex Goh", "phone": "016-4697992", "ren": "E2265",
        })

    def test_malaysian_agent_signature_prefers_mobile_over_office_number(self):
        message = """WTB
landed house (TTDI or others suitable one)

Singaporean buyer
Individual title
Must have garden
Budget follow market value

Kindly propose to me if there’s any suitable listing🙏

Marcus Yeoh | J+Team
Real Estate Negotiator (REN50605)
+6017-4081131
Kommons Realty Sdn Bhd
E(1)2150
+603-64130178"""
        output = full_model_output(
            type="lead", geo_names=["TTDI"], location_references=["TTDI"],
            transaction_types=["Buy/Sell"], property_types=["Landed", "House"],
            source_agency_name="Kommons Realty Sdn Bhd",
            proposing_agent={
                "name": "Marcus Yeoh", "phone": "+6017-4081131", "ren": "REN50605",
            },
        )
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(status="completed", output_text=json.dumps(output)),
        ) as create:
            parsed = importer.parse_forwarded_message(message, import_type="lead")
        self.assertEqual(parsed["proposing_agent"]["name"], "Marcus Yeoh")
        self.assertEqual(parsed["proposing_agent"]["phone"], "+6017-4081131")
        self.assertEqual(parsed["proposing_agent"]["ren"], "REN50605")
        self.assertEqual(parsed["transaction_types"], ["Buy/Sell"])
        self.assertEqual(parsed["geo_names"], ["TTDI"])
        self.assertEqual(parsed["property_types"], ["Landed"])
        self.assertEqual(parsed["source_agency_name"], "Kommons Realty Sdn Bhd")
        self.assertNotIn("budget", parsed)
        self.assertNotIn("bedrooms_min", parsed)
        prompt = create.call_args.kwargs["input"]
        self.assertIn("individual Malaysian mobile (+601/01)", prompt)
        self.assertIn("office or landline (+603/03)", prompt)
        self.assertIn("Use the individual person's name", prompt)
        self.assertIn("both Leads and Listings", prompt)
        self.assertIn("source_agency_name", prompt)

    def test_duplicate_phone_users_do_not_create_another_user(self):
        duplicates = [
            {"_id": "user-1", "phone": "60164697992"},
            {"_id": "user-2", "phone": "60164697992"},
        ]
        with patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          return_value=duplicates), \
             patch.object(importer.rentee_app, "_bubble_create") as create:
            result = importer.resolve_or_create_proposing_agent(
                "Alex Goh", "0164697992", "E2265")
        self.assertEqual(result["status"], "duplicate_existing")
        self.assertIsNone(result["user_id"])
        create.assert_not_called()

    def test_agent_lookup_failure_does_not_stop_listing_import(self):
        parsed = {
            "type": "listing", "geo_name": "Bangsar",
            "transaction_types": ["Rent/Let"], "price_rent": 8500,
            "proposing_agent": {
                "name": "Alex Goh", "phone": "0164697992", "ren": "E2265",
            },
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "find_bubble_users_by_phone",
                          side_effect=RuntimeError("Bubble unavailable")), \
             patch.object(importer.rentee_app, "_bubble_create",
                          return_value="listing-1") as create:
            result = importer.process_whatsapp_import(
                "listing", import_type="listing", geo_records=GEOS,
                development_records=DEVELOPMENTS)
        self.assertEqual(result["status"], "processed")
        self.assertIsNone(result["proposing_agent_user_id"])
        payload = create.call_args.args[2]
        self.assertEqual(payload["ProposingAgentName"], "Alex Goh")
        self.assertEqual(payload["ProposingAgentNumber"], "60164697992")

    def test_bubble_create_error_logs_status_body_and_sanitized_payload(self):
        response = SimpleNamespace(status_code=400, text="Bubble invalid field")
        error = RuntimeError("request failed")
        error.response = response
        payload = {
            "ProposedAgentNameLead": "Alex Goh",
            "token": "must-not-appear",
        }
        with patch.object(importer.rentee_app, "_bubble_create", side_effect=error), \
             patch("builtins.print") as log, self.assertRaises(RuntimeError):
            importer._create_import_record("lead", payload, "live")
        rendered = " ".join(str(call) for call in log.call_args_list)
        self.assertIn("[WHATSAPP IMPORT BUBBLE ERROR]", rendered)
        self.assertIn("type=lead", rendered)
        self.assertIn("status=400", rendered)
        self.assertIn("Bubble invalid field", rendered)
        self.assertIn("ProposedAgentNameLead", rendered)
        self.assertIn("[REDACTED]", rendered)
        self.assertNotIn("must-not-appear", rendered)


if __name__ == "__main__":
    unittest.main()
