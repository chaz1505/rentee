import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import whatsapp_importer as importer


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
        "preferred_development_names": [], "development_name": None,
        "transaction_types": [], "property_types": [], "property_type": None,
        "budget": None, "asking_price": None, "bedrooms_min": None, "beds": None,
    }
    value.update(updates)
    return value


class WhatsAppImporterTests(unittest.TestCase):
    def parse_as(self, output, raw="forwarded message"):
        response = SimpleNamespace(
            status="completed", output_text=json.dumps(full_model_output(**output))
        )
        with patch.object(importer.rentee_app.client.responses, "create", return_value=response):
            return importer.parse_forwarded_message(raw)

    def process_as(self, parsed):
        create = MagicMock(return_value="bubble-1")
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer.rentee_app, "_bubble_create", create):
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
            "preferred_development_names": [],
            "transaction_types": ["Rent/Let"], "property_types": ["Condo"],
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

    def test_multiple_geo_lead_survives_parse_and_resolution(self):
        parsed = self.parse_as({
            "type": "lead", "geo_names": ["Brickfields", "Petaling Jaya"],
            "transaction_types": ["Rent/Let"], "property_types": ["Apartment"],
            "budget": 2500, "bedrooms_min": 2,
        }, "Looking to rent in Brickfields or PJ. Apartment, 2 bed, max RM2,500.")
        resolved = importer.resolve_geo_names(parsed["geo_names"], GEOS)
        self.assertEqual([item["id"] for item in resolved], ["geo-brickfields", "geo-pj"])

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

    def test_rental_listing_and_development_geo_derivation(self):
        parsed = self.parse_as({
            "type": "listing", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "asking_price": 8500, "beds": 3,
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
            "asking_price": 1800000, "beds": 3,
        }, "WTS\n3 bedroom condo\nRM1.8m\nBangsar")
        payload = importer.build_listing_payload(
            parsed, importer.resolve_geo_name("Bangsar", GEOS), None
        )
        self.assertEqual(payload["priceSale"], 1800000)
        self.assertNotIn("priceRent", payload)

    def test_explicit_geo_wins_and_conflict_is_logged(self):
        parsed = {
            "type": "listing", "geo_name": "KLCC", "development_name": "One Menerung",
            "transaction_types": ["Rent/Let"], "asking_price": 8500, "beds": 3,
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

    def test_unsupported_property_type_is_removed(self):
        parsed = self.parse_as({
            "type": "listing", "transaction_types": ["Buy/Sell"],
            "property_type": "Villa", "asking_price": 1000000,
        })
        self.assertNotIn("property_type", parsed)
        self.assertNotIn("propertyType", importer.build_listing_payload(parsed, None, None))

    def test_ambiguous_fuzzy_development_resolves_neither(self):
        records = [
            {"_id": "a", "name": "Alam Sanctuary One"},
            {"_id": "b", "name": "Alam Sanctuary Two"},
        ]
        result = importer.resolve_development_name("Alam Sanctuary", records)
        self.assertFalse(result["matched"])

    def test_unresolved_development_allows_creation_without_relationship(self):
        parsed = {
            "type": "listing", "geo_name": "Bangsar",
            "development_name": "Alam Sanctuary",
            "transaction_types": ["Rent/Let"], "asking_price": 5000, "beds": 2,
        }
        with patch.object(importer, "verify_development_candidate", return_value={
            "status": "not_found", "raw_name": "Alam Sanctuary",
        }):
            result, create = self.process_as(parsed)
        payload = create.call_args.args[2]
        self.assertNotIn("development", payload)
        self.assertEqual(result["unresolved_development_names"], ["Alam Sanctuary"])
        self.assertEqual(payload["Geo"], "geo-bangsar")

    def test_case_and_punctuation_normalized_exact(self):
        result = importer.resolve_development_name("  ONE  MENERUNG!!! ", DEVELOPMENTS)
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
            "transaction_types": ["Rent/Let"], "asking_price": 8500, "beds": 3,
        }
        with patch.object(importer, "verify_development_candidate") as verify, \
             patch.object(importer, "create_verified_development") as create_development:
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
             patch.object(importer, "verify_development_candidate", return_value=verification), \
             patch.object(importer, "_fresh_development_records", return_value=[]), \
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

    def test_verified_canonical_development_is_reused_without_post(self):
        parsed = {
            "type": "listing", "development_name": "Sefina",
            "transaction_types": ["Rent/Let"], "asking_price": 5000,
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
             patch.object(importer, "verify_development_candidate", return_value=verification), \
             patch.object(importer, "create_verified_development") as create_development, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create:
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
            "transaction_types": ["Buy/Sell"], "asking_price": 900000,
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "verify_development_candidate", return_value={
                 "status": "ambiguous", "raw_name": "Sunshine Residence",
                 "candidates": [{"name": "A"}, {"name": "B"}], "confidence": 0.45,
             }), patch.object(importer, "create_verified_development") as create_dev, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create:
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
            "transaction_types": ["Rent/Let"], "asking_price": 5000,
        }
        with patch.object(importer, "parse_forwarded_message", return_value=parsed), \
             patch.object(importer, "verify_development_candidate", return_value={
                 "status": "verified", "canonical_name": "Sefina Mont Kiara",
                 "geo_name": "Unknown Area", "verification_url": "https://example.com/sefina",
                 "confidence": 0.97,
             }), patch.object(importer, "create_verified_development") as create_dev, \
             patch.object(importer.rentee_app, "_bubble_create", return_value="listing-1") as create:
            result = importer.process_whatsapp_import(
                "Sefina", geo_records=GEOS, development_records=DEVELOPMENTS
            )
        create_dev.assert_not_called()
        self.assertNotIn("development", create.call_args.args[2])
        self.assertEqual(result["unresolved_development_names"], ["Sefina"])

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
             patch.object(importer, "verify_development_candidate", side_effect=verify) as verifier, \
             patch.object(importer, "_fresh_development_records", return_value=[]), \
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

    def test_web_verifier_reuses_responses_web_search_and_context(self):
        output = {
            "status": "verified", "canonical_name": "Sefina Mont Kiara",
            "geo_name": "Mont Kiara",
            "verification_url": "https://developer.example/sefina",
            "evidence": [
                {"url": "https://developer.example/sefina", "source": "Developer",
                 "support": "Official project identity and location"},
                {"url": "https://portal.example/sefina", "source": "Property portal",
                 "support": "Corroborates name and Mont Kiara area"},
            ],
            "candidates": [], "confidence": 0.97,
        }
        response = SimpleNamespace(output_text=json.dumps(output))
        context = {
            "property_types": ["Condo"],
            "raw_text": "Daughters study at Garden International School",
        }
        with patch.object(
            importer.rentee_app.client.responses, "create", return_value=response
        ) as create:
            result = importer.verify_development_candidate("Sefina", context)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(create.call_args.kwargs["tools"], [{"type": "web_search"}])
        prompt = create.call_args.kwargs["input"]
        self.assertIn("Sefina", prompt)
        self.assertIn("Garden International School", prompt)
        self.assertIn("never the returned residential geo_name", prompt)

    def test_weak_single_source_verification_is_downgraded(self):
        output = {
            "status": "verified", "canonical_name": "Sunshine Residence",
            "geo_name": "Kuala Lumpur",
            "verification_url": "https://weak.example/sunshine",
            "evidence": [{
                "url": "https://weak.example/sunshine", "source": "Unknown",
                "support": "Mentions the same words",
            }],
            "candidates": [], "confidence": 0.96,
        }
        with patch.object(
            importer.rentee_app.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ):
            result = importer.verify_development_candidate("Sunshine Residence", {})
        self.assertEqual(result["status"], "ambiguous")

    def test_development_creation_race_requeries_once_and_reuses(self):
        geo = importer.resolve_geo_name("Bangsar", GEOS)
        raced_record = {"_id": "dev-raced", "Name": "Sefina Mont Kiara",
                        "Geo": "geo-bangsar"}
        with patch.object(
            importer, "_fresh_development_records", side_effect=[[], [raced_record]]
        ) as fresh, patch.object(
            importer.rentee_app, "_bubble_create", side_effect=RuntimeError("duplicate")
        ) as create:
            result = importer.create_verified_development(
                "Sefina Mont Kiara", geo, "https://example.com/sefina"
            )
        self.assertEqual(result["id"], "dev-raced")
        self.assertEqual(fresh.call_count, 2)
        create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
