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


if __name__ == "__main__":
    unittest.main()
