import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app
import whatsapp_importer as importer


DEVELOPMENTS = [{"_id": "dev-one", "name": "One Menerung"}]
GEOS = [{"_id": "geo-bangsar", "name": "Bangsar"}]


class MatchAlertV2Tests(unittest.TestCase):
    def records(self, transaction="Rent/Let"):
        lead = {
            "_id": "lead-1", "owner": "lead-agent",
            "TransactionType": [transaction], "preferredDevelopments": ["dev-one"],
            "Geo": ["geo-bangsar"], "propertyTypes": ["Condo"],
            "bedroomsMin": 3, "bathroomsMin": 2,
            "budgetRent": 15000, "budgetBuy": 1500000,
        }
        listing = {
            "_id": "listing-1", "owner": "listing-agent",
            "TransactionType": [transaction], "development": "dev-one",
            "Geo": "geo-bangsar", "propertyType": "Condo", "beds": 3,
            "baths": 2, "priceRent": 12000, "priceSale": 1350000,
            "Created Date": "2026-11-24T08:00:00Z",
        }
        return lead, listing

    def test_wtr_wtb_wtl_wts_recipient_first_introductions(self):
        cases = (
            ("lead", "Rent/Let", "We've found a match for your 3-bedroom listing in One Menerung at RM12,000/month"),
            ("lead", "Buy/Sell", "We've found a match for your 3-bedroom listing in One Menerung at RM1,350,000"),
            ("listing", "Rent/Let", "We've found a match for your tenant looking for a 3-bedroom in One Menerung"),
            ("listing", "Buy/Sell", "We've found a match for your buyer looking for a 3-bedroom in One Menerung"),
        )
        for created_type, transaction, expected in cases:
            with self.subTest(created_type=created_type, transaction=transaction):
                lead, listing = self.records(transaction)
                self.assertEqual(importer._recipient_introduction(
                    created_type, lead, listing, DEVELOPMENTS, GEOS), expected)

    def test_nine_parameters_have_correct_orientation_and_fallbacks(self):
        lead, listing = self.records()
        alert = importer.build_match_alert(
            "listing", lead, listing,
            {"name": "Stephanie"},
            {"name": "Gwen Lim", "phone": "60174156107", "AgencyName": "Prestige Realty"},
            DEVELOPMENTS, GEOS, score=93, explanation="Strong location and budget fit",
        )
        self.assertEqual(alert["parameters"], [
            "Stephanie",
            "We've found a match for your tenant looking for a 3-bedroom in One Menerung",
            "WTL · One Menerung, Bangsar · Condo · 3 bedrooms · 2 bathrooms · RM12,000/month · Uploaded 24 Nov 2026",
            "WTR · One Menerung, Bangsar · Condo · 3 bedrooms · 2 bathrooms · Budget RM15,000/month",
            "93", "Strong location and budget fit", "Gwen Lim · Prestige Realty",
            "0174156107", "Gwen",
        ])
        sparse = importer.build_match_alert(
            "listing", {"_id": "lead-1"}, {"_id": "listing-1"},
            {}, {}, score=80, explanation="fit",
        )["parameters"]
        self.assertEqual(len(sparse), 9)
        self.assertEqual(sparse[7], "Not provided")
        self.assertTrue(all("\n" not in value and value for value in sparse))

    def test_template_is_used_inside_and_outside_window_with_both_buttons(self):
        lead, listing = self.records()
        users = {
            "lead-agent": {"name": "Stephanie", "phone": "0122017185", "alerts": True},
            "listing-agent": {"name": "Gwen", "phone": "0174156107"},
        }
        for window in ("open", "closed"):
            with self.subTest(window=window), patch.dict(os.environ, {
                "MATCH_ALERT_TEMPLATES_ENABLED": "true",
            }), patch.object(importer.rentee_app, "bubble",
                side_effect=lambda url: users[url.rsplit("/", 1)[-1]]), \
                patch.object(importer.rentee_app, "owner_check_whatsapp_window", return_value=window), \
                patch.object(importer.rentee_app, "send_whatsapp_text") as text, \
                patch.object(importer.rentee_app, "send_whatsapp_template", return_value=["wamid.v2"]) as template, \
                patch.object(importer, "save_whatsapp_message"), \
                patch.object(importer.rentee_app, "_bubble_patch") as update:
                sent = importer.maybe_send_match_alert(
                    "match-1", "listing", lead, listing, "lead-agent", "listing-agent",
                    match_record={}, development_records=DEVELOPMENTS, geo_records=GEOS,
                    score=91, explanation="fit",
                )
            self.assertTrue(sent)
            text.assert_not_called()
            template.assert_called_once()
            self.assertEqual(template.call_args.args[:2], (
                "60122017185", "rentee_match_alert_v2"))
            self.assertEqual(template.call_args.kwargs["button_parameters"], [
                "listing/listing-1", "lead/lead-1"])
            self.assertEqual(len(template.call_args.kwargs["parameters"]), 9)
            update.assert_called_once()

    def test_missing_meta_message_id_does_not_mark_match_sent(self):
        lead, listing = self.records()
        users = {
            "lead-agent": {"name": "Stephanie", "phone": "0122017185", "alerts": True},
            "listing-agent": {"name": "Gwen", "phone": "0174156107"},
        }
        with patch.dict(os.environ, {"MATCH_ALERT_TEMPLATES_ENABLED": "true"}), \
             patch.object(importer.rentee_app, "bubble", side_effect=lambda url: users[url.rsplit("/", 1)[-1]]), \
             patch.object(importer.rentee_app, "owner_check_whatsapp_window", return_value="open"), \
             patch.object(importer.rentee_app, "send_whatsapp_template", return_value=[None]), \
             patch.object(importer.rentee_app, "_bubble_patch") as update:
            sent = importer.maybe_send_match_alert(
                "match-1", "listing", lead, listing, "lead-agent", "listing-agent",
                match_record={}, score=91, explanation="fit")
        self.assertFalse(sent)
        update.assert_not_called()


class WhatsAppTemplatePayloadV2Tests(unittest.TestCase):
    @patch("app.requests.post")
    def test_dynamic_url_buttons_and_backward_compatibility(self, post):
        post.return_value.status_code = 200
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {"messages": [{"id": "wamid.v2"}]}
        with patch.dict(os.environ, {
            "WHATSAPP_PHONE_NUMBER_ID": "phone-id", "WHATSAPP_ACCESS_TOKEN": "token",
        }):
            app.send_whatsapp_template(
                "60123456789", "rentee_match_alert_v2", language_code="en",
                parameters=[str(index) for index in range(1, 10)],
                button_parameters=["listing/listing-1", "lead/lead-1"],
            )
        components = post.call_args.kwargs["json"]["template"]["components"]
        self.assertEqual(components[1:], [
            {"type": "button", "sub_type": "url", "index": "0",
             "parameters": [{"type": "text", "text": "listing/listing-1"}]},
            {"type": "button", "sub_type": "url", "index": "1",
             "parameters": [{"type": "text", "text": "lead/lead-1"}]},
        ])


if __name__ == "__main__":
    unittest.main()
