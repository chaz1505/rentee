import json
import os
import unittest
import requests
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "verify-me")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "wa-token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "phone-number-id")
os.environ.setdefault("WHATSAPP_BUSINESS_PHONE_NUMBER", "60115551234")
os.environ.setdefault("RENTEE_WHATSAPP_NUMBER", "601112032754")

import app as app_module


def webhook_payload(message_id="wamid.1", phone="60123456789", text="Hi"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": phone, "profile": {"name": "Aisha"}}],
            "messages": [{
                "from": phone, "id": message_id, "timestamp": "1787839570",
                "type": "text", "text": {"body": text},
            }],
        }}]}],
    }


def audio_webhook_payload(
    message_id="wamid.audio-1", phone="60123456789", media_id="media-1",
):
    payload = webhook_payload(message_id, phone)
    message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    message["type"] = "audio"
    message.pop("text", None)
    message["audio"] = {"id": media_id, "mime_type": "audio/ogg; codecs=opus"}
    return payload


class ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


class TenantProfileCaptureTests(unittest.TestCase):
    def extraction(self, **values):
        result = {field: None for field in app_module.TENANT_PROFILE_FIELDS}
        result.update(values)
        return result

    def test_structured_extraction_schema_covers_only_supported_fields(self):
        output = self.extraction(nationality="British")
        with patch.object(
            app_module.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ) as create:
            result = app_module.extract_tenant_profile("Nationality: British")
        self.assertEqual(result["nationality"], "British")
        schema = create.call_args.kwargs["text"]["format"]["schema"]
        self.assertEqual(set(schema["properties"]), set(app_module.TENANT_PROFILE_FIELDS))
        self.assertEqual(set(schema["required"]), set(app_module.TENANT_PROFILE_FIELDS))
        self.assertNotIn("budgetBuy", schema["properties"])
        self.assertNotIn("tools", create.call_args.kwargs)

    def test_pax_values_remain_independent_and_missing_is_null(self):
        extracted = self.extraction(adults=2, children=2, helpers=1)
        self.assertEqual(app_module._tenant_profile_patch(extracted), {
            "adults": 2, "children": 2, "helpers": 1,
        })
        no_helper_mentioned = self.extraction(adults=2, children=2)
        self.assertNotIn(
            "helpers", app_module._tenant_profile_patch(no_helper_mentioned)
        )
        explicit_no_helper = self.extraction(helpers=0)
        self.assertEqual(
            app_module._tenant_profile_patch(explicit_no_helper)["helpers"], 0
        )

    def test_natural_language_pax_contract_is_in_extraction_prompt(self):
        output = self.extraction(adults=2, children=2)
        with patch.object(
            app_module.client.responses, "create",
            return_value=SimpleNamespace(output_text=json.dumps(output)),
        ) as create:
            result = app_module.extract_tenant_profile(
                "my husband and I with two kids"
            )
        self.assertEqual((result["adults"], result["children"]), (2, 2))
        prompt = create.call_args.kwargs["input"]
        self.assertIn("Split adults, children, and helpers independently", prompt)

    def test_bedrooms_budget_and_identity_fields_are_whitelisted(self):
        extracted = self.extraction(bedroomsMin=3, budgetRent=12000)
        extracted.update({
            "budgetBuy": 999999, "owner": "other", "Agent?": "No",
            "name": "Changed", "phone": "000",
        })
        self.assertEqual(app_module._tenant_profile_patch(extracted), {
            "bedroomsMin": 3, "budgetRent": 12000,
        })

    def test_furnishing_preference_uses_exact_bubble_text_values(self):
        for value in (
            "Fully Furnished", "Partially Furnished", "Unfurnished",
        ):
            with self.subTest(value=value):
                self.assertEqual(
                    app_module._tenant_profile_patch(
                        self.extraction(furnishingPreference=value)
                    )["furnishingPreference"],
                    value,
                )
        self.assertNotIn(
            "furnishingPreference",
            app_module._tenant_profile_patch(
                self.extraction(furnishingPreference=None)
            ),
        )

    def test_profile_payload_serializes_text_and_numbers_by_bubble_schema(self):
        payload = app_module._tenant_profile_patch(self.extraction(
            nationality="British", adults=2, children=2, helpers=1,
            bedroomsMin=3, furnishingPreference="Fully Furnished",
            occupation="Finance Director at Shell", pets="No",
            budgetRent=12000, viewingPreference="Saturday afternoon",
        ))
        self.assertEqual(set(payload), {
            "nationality", "adults", "children", "helpers", "bedroomsMin",
            "furnishingPreference", "occupation", "pets", "budgetRent",
            "viewingPreference",
        })
        for field in ("adults", "children", "helpers", "bedroomsMin", "budgetRent"):
            self.assertIsInstance(payload[field], (int, float))
            self.assertNotIsInstance(payload[field], bool)
        for field in (
            "nationality", "furnishingPreference", "occupation", "pets",
            "viewingPreference",
        ):
            self.assertIsInstance(payload[field], str)

    def test_invalid_and_empty_profile_values_are_omitted(self):
        payload = app_module._tenant_profile_patch(self.extraction(
            nationality="  ", adults="2 adults", children=-1, helpers=True,
            bedroomsMin="3", furnishingPreference="Anything is fine",
            occupation=None, pets="", startDate="mid October",
            budgetRent="RM12,000", viewingPreference=[],
        ))
        self.assertEqual(payload, {})

    def test_budget_routes_to_only_the_resolved_transaction_field(self):
        extracted = self.extraction(budgetRent=12000)
        self.assertEqual(
            app_module._tenant_profile_patch(extracted, "Rent/Let"),
            {"budgetRent": 12000},
        )
        self.assertEqual(
            app_module._tenant_profile_patch(extracted, "Buy/Sell"),
            {"budgetBuy": 12000},
        )
        self.assertEqual(
            app_module._tenant_profile_patch(extracted, None), {},
        )

    def test_unresolved_transaction_keeps_non_budget_profile_fields(self):
        payload = app_module._tenant_profile_patch(
            self.extraction(nationality="British", adults=2, budgetRent=12000),
            None,
        )
        self.assertEqual(payload, {"nationality": "British", "adults": 2})

    def test_text_profile_values_preserve_useful_detail(self):
        payload = app_module._tenant_profile_patch(self.extraction(
            pets="1 small dog", occupation="Finance Director at Shell",
            viewingPreference="Saturday afternoon",
        ))
        self.assertEqual(payload, {
            "occupation": "Finance Director at Shell",
            "pets": "1 small dog",
            "viewingPreference": "Saturday afternoon",
        })

    def test_exact_date_serializes_as_bubble_date_and_approximate_is_omitted(self):
        exact = app_module._tenant_profile_patch(
            self.extraction(startDate="2026-10-15")
        )
        approximate = app_module._tenant_profile_patch(
            self.extraction(startDate=None)
        )
        self.assertEqual(exact["startDate"], "2026-10-15T00:00:00.000Z")
        self.assertNotIn("startDate", approximate)

    @patch("app._bubble_patch")
    @patch("app.extract_tenant_profile")
    @patch("app.find_handoff_lead_by_phone")
    def test_capture_updates_only_non_null_values_and_allows_corrections(
        self, find_linked, extract, patch_bubble,
    ):
        find_linked.return_value = {
            "_id": "lead-1", "nationality": "British", "budgetRent": 12000,
            "owner": "user-gwen", "Agent?": "No", "name": "Sarah",
            "phone": "60123456789",
            "_handoff_transaction_type": "Rent/Let",
        }
        extract.return_value = self.extraction(
            budgetRent=14000, viewingPreference="Saturday afternoon"
        )
        lead = app_module.capture_linked_tenant_profile(
            "60123456789", "Actually 14k; Saturday afternoon"
        )
        patch_bubble.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/lead/lead-1",
            {"budgetRent": 14000, "viewingPreference": "Saturday afternoon"},
        )
        self.assertEqual(lead["nationality"], "British")
        self.assertEqual(lead["budgetRent"], 14000)
        for field, expected in {
            "owner": "user-gwen", "Agent?": "No", "name": "Sarah",
            "phone": "60123456789",
        }.items():
            self.assertEqual(lead[field], expected)

    @patch("app.bubble", return_value={"_id": "lead-1"})
    @patch("app._bubble_records")
    def test_durable_enquiry_relationship_resolves_existing_lead(
        self, records, bubble_get,
    ):
        records.return_value = iter([{
            "_id": "enquiry-1", "Enquirer Phone": "60123456789",
            "Lead": "lead-1",
        }])
        lead = app_module.find_handoff_lead_by_phone("+60 12-345-6789")
        self.assertEqual(lead["_id"], "lead-1")
        bubble_get.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/lead/lead-1"
        )

    @patch("app.extract_tenant_profile", side_effect=RuntimeError("AI unavailable"))
    @patch("app.find_handoff_lead_by_phone", return_value={"_id": "lead-1"})
    def test_extraction_failure_returns_existing_lead_without_raising(
        self, _find, _extract,
    ):
        self.assertEqual(
            app_module.capture_linked_tenant_profile("60123456789", "profile")["_id"],
            "lead-1",
        )

    def test_incomplete_response_rejects_partial_structured_output(self):
        partial = self.extraction(nationality="British")
        response = SimpleNamespace(
            status="incomplete", output_text=json.dumps(partial)
        )
        with patch.object(
            app_module.client.responses, "create", return_value=response
        ):
            with self.assertRaises(app_module.TenantProfileExtractionError):
                app_module.extract_tenant_profile("Nationality: British")

    @patch("app._bubble_patch", side_effect=RuntimeError("Bubble unavailable"))
    @patch("app.extract_tenant_profile")
    @patch("app.find_handoff_lead_by_phone")
    def test_bubble_update_failure_returns_existing_lead_without_mutating_it(
        self, find_linked, extract, _patch_bubble,
    ):
        lead = {"_id": "lead-1", "budgetRent": 12000}
        find_linked.return_value = lead
        extract.return_value = self.extraction(budgetRent=14000)

        result = app_module.capture_linked_tenant_profile(
            "60123456789", "Actually 14k"
        )

        self.assertIs(result, lead)
        self.assertEqual(result["budgetRent"], 12000)

    @patch("app.extract_tenant_profile")
    @patch("app.find_handoff_lead_by_phone")
    def test_bubble_http_failure_logs_status_fields_and_safe_response(
        self, find_linked, extract,
    ):
        find_linked.return_value = {"_id": "lead-1"}
        extract.return_value = self.extraction(
            nationality="British", adults=2,
            furnishingPreference="Fully Furnished",
        )
        response = requests.Response()
        response.status_code = 400
        response._content = (
            b'Invalid data for furnishingPreference: Fully Furnished'
        )
        error = requests.HTTPError("400 Client Error", response=response)

        with patch("app._bubble_patch", side_effect=error), \
             patch("builtins.print") as mocked_print:
            app_module.capture_linked_tenant_profile(
                "60123456789", "profile"
            )

        log = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("tenant_profile_update_failed status=400", log)
        self.assertIn(
            "fields=['nationality', 'adults', 'furnishingPreference']", log
        )
        self.assertIn("Invalid data for furnishingPreference", log)
        self.assertNotIn("Fully Furnished", log)
        self.assertNotIn("British", log)


class OwnerCheckTests(unittest.TestCase):
    def setUp(self):
        self.conversation_patcher = patch(
            "app.conversation_store.find_or_create_conversation",
            return_value=({"_id": "conversation-owner"}, False),
        )
        self.conversation_patcher.start()
        self.addCleanup(self.conversation_patcher.stop)

    def enquiry(self, **values):
        result = {
            "_id": "enquiry-1", "Listing": "listing-1",
            "Enquirer Phone": "60123456789", "Lead": "lead-1",
            "Principal": "principal-1",
        }
        result.update(values)
        return result

    def prepare(self, enquiry, owner_contact="+60 11-555 1234"):
        lead = {
            "_id": "lead-1", "ActiveForwardedEnquiry": enquiry["_id"],
        }
        with patch("app.bubble", side_effect=[
                 enquiry, {"ownerContact": owner_contact},
             ]) as get, \
             patch("app._bubble_patch") as update:
            result = app_module.prepare_owner_check(lead)
        return result, get, update

    def test_valid_formatted_owner_contact_creates_pending_state(self):
        with patch("builtins.print") as logged:
            result, get, update = self.prepare(self.enquiry())
        self.assertEqual(get.call_args_list[0].args, (
            "https://www.rentee.asia/api/1.1/obj/enquiry/enquiry-1",
        ))
        self.assertEqual(get.call_args_list[1].args, (
            "https://www.rentee.asia/api/1.1/obj/listing/listing-1",
        ))
        update.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/enquiry/enquiry-1",
            {"OwnerCheckStatus": "Pending", "OwnerCheckPhone": "60115551234"},
        )
        self.assertEqual(result["OwnerCheckStatus"], "Pending")
        self.assertNotIn("OwnerCheckSentAt", update.call_args.args[1])
        self.assertNotIn("OwnerCheckResponse", update.call_args.args[1])
        logs = " ".join(str(call) for call in logged.call_args_list)
        self.assertIn(
            "listing_id=listing-1 bubble_auth=admin_token_present", logs
        )
        self.assertIn(
            "listing_id=listing-1 raw_owner_contact_present=true", logs
        )
        self.assertNotIn("+60 11-555 1234", logs)

    def test_owner_contact_is_read_from_enquiry_listing_not_enquiry_id(self):
        enquiry = self.enquiry(_id="enquiry-A", Listing="listing-B")
        lead = {"_id": "lead-1", "ActiveForwardedEnquiry": "enquiry-A"}
        with patch("app.bubble", side_effect=[
                 enquiry, {"_id": "listing-B", "condo": "condo-C",
                           "ownerContact": "+60 11-555 1234"},
             ]) as get, patch("app._bubble_patch") as update:
            app_module.prepare_owner_check(lead)
        self.assertEqual(get.call_args_list[0].args[0].split("/")[-1], "enquiry-A")
        self.assertEqual(get.call_args_list[1].args[0].split("/")[-1], "listing-B")
        self.assertNotEqual(get.call_args_list[1].args[0].split("/")[-1], "enquiry-A")
        update.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/enquiry/enquiry-A",
            {"OwnerCheckStatus": "Pending", "OwnerCheckPhone": "60115551234"},
        )

    def test_blank_and_invalid_owner_contact_do_not_create_pending_state(self):
        for contact in ("", "not-a-phone", "123"):
            with self.subTest(contact=contact):
                _result, _get, update = self.prepare(self.enquiry(), contact)
                update.assert_not_called()

    def test_existing_matching_pending_state_is_reused(self):
        enquiry = self.enquiry(
            OwnerCheckStatus="Pending", OwnerCheckPhone="60115551234",
        )
        _result, _get, update = self.prepare(enquiry)
        update.assert_not_called()

    def test_sent_and_replied_states_never_regress(self):
        for status in ("Sent", "Replied"):
            with self.subTest(status=status):
                _result, get, update = self.prepare(
                    self.enquiry(OwnerCheckStatus=status)
                )
                self.assertEqual(get.call_count, 1)
                update.assert_not_called()

    def test_missing_active_enquiry_does_not_guess(self):
        with patch("app._bubble_records") as records, \
             patch("app.bubble") as get, patch("app._bubble_patch") as update:
            self.assertIsNone(app_module.prepare_owner_check({"_id": "lead-1"}))
        records.assert_not_called()
        get.assert_not_called()
        update.assert_not_called()

    def test_active_enquiry_lead_mismatch_is_rejected(self):
        enquiry = self.enquiry(Lead="lead-other")
        lead = {"_id": "lead-1", "ActiveForwardedEnquiry": "enquiry-1"}
        with patch("app.bubble", return_value=enquiry), \
             patch("app._bubble_patch") as update:
            self.assertIsNone(app_module.prepare_owner_check(lead))
        update.assert_not_called()

    def test_multiple_history_does_not_trigger_enquiry_search(self):
        enquiry = self.enquiry(_id="enquiry-active")
        lead = {"_id": "lead-1", "ActiveForwardedEnquiry": "enquiry-active"}
        with patch("app._bubble_records") as records, \
             patch("app.bubble", side_effect=[
                 enquiry, {"ownerContact": "+60 11-555 1234"},
             ]), patch("app._bubble_patch") as update:
            app_module.prepare_owner_check(lead)
        records.assert_not_called()
        self.assertTrue(update.call_args.args[0].endswith("/enquiry/enquiry-active"))

    def test_pending_owner_check_sends_once_and_marks_sent(self):
        enquiry = self.enquiry(
            TransactionType=["Rent/Let"], OwnerCheckStatus="Pending",
            OwnerCheckPhone="60115551234", OwnerCheckResponse="unchanged",
        )
        lead = {
            "_id": "lead-1", "nationality": "British", "adults": 2,
            "children": 2, "pets": "No", "budgetRent": 15000,
            "phone": "60123456789", "email": "private@example.com",
        }
        listing = {
            "name": "One Menerung", "beds": 3, "priceRent": 15000,
        }
        now = datetime(2026, 8, 30, 4, 5, tzinfo=timezone.utc)
        with patch("app.bubble", return_value=listing), \
             patch("app.owner_check_whatsapp_window", return_value="open"), \
             patch("app.send_whatsapp_text", return_value=["wamid.freeform"]) as send, \
             patch("app._bubble_create", return_value="message-owner") as create, \
             patch("app._bubble_patch") as update, \
             patch("builtins.print") as logged:
            sent = app_module.send_pending_owner_check(
                lead, enquiry, now=now
            )
            repeated = app_module.send_pending_owner_check(
                lead, enquiry, now=now
            )
        self.assertTrue(sent)
        self.assertFalse(repeated)
        send.assert_called_once()
        destination, message = send.call_args.args
        self.assertEqual(destination, "60115551234")
        self.assertIn("still available", message)
        self.assertIn("would the owner consider this tenant profile", message)
        self.assertIn("British", message)
        self.assertIn("2 adults + 2 children", message)
        self.assertNotIn("60123456789", message)
        self.assertNotIn("private@example.com", message)
        update.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/enquiry/enquiry-1",
            {
                "OwnerCheckStatus": "Sent",
                "OwnerCheckSentAt": "2026-08-30T04:05:00Z",
            },
        )
        self.assertEqual(enquiry["OwnerCheckStatus"], "Sent")
        self.assertEqual(enquiry["OwnerCheckResponse"], "unchanged")
        create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "message", {
                "phone": "60115551234",
                "direction": "Outbound",
                "own_Sent?": "No",
                "messageContent": message,
                "whatsappMessageId": "wamid.freeform",
                "lead": "lead-1",
                "Conversation": "conversation-owner",
            },
        )
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn(
            "destination=ownerContact phone=...551234 "
            "window=open method=freeform",
            logs,
        )
        self.assertNotIn("60115551234", logs)

    def test_failed_owner_send_leaves_pending_and_sent_at_untouched(self):
        enquiry = self.enquiry(
            OwnerCheckStatus="Pending", OwnerCheckPhone="60115551234",
        )
        with patch("app.bubble", return_value={"name": "One Menerung"}), \
             patch("app.owner_check_whatsapp_window", return_value="open"), \
             patch("app.send_whatsapp_text", side_effect=RuntimeError("Meta down")), \
             patch("app._bubble_patch") as update:
            sent = app_module.send_pending_owner_check({"_id": "lead-1"}, enquiry)
        self.assertFalse(sent)
        self.assertEqual(enquiry["OwnerCheckStatus"], "Pending")
        self.assertNotIn("OwnerCheckSentAt", enquiry)
        update.assert_not_called()

    def test_owner_window_uses_latest_inbound_created_date(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        with patch("app._bubble_records", return_value=iter([
            {"Created Date": "2026-08-29T10:00:00Z"},
            {"Created Date": "2026-08-30T11:00:00Z"},
        ])) as records:
            window = app_module.owner_check_whatsapp_window(
                "+60 11-555 1234", now=now
            )
        self.assertEqual(window, "open")
        self.assertEqual(records.call_args.args[2], [
            {"key": "phone", "constraint_type": "equals",
             "value": "60115551234"},
            {"key": "direction", "constraint_type": "equals",
             "value": "Inbound"},
        ])

    def test_owner_window_expired_inbound_is_closed(self):
        now = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
        with patch("app._bubble_records", return_value=iter([
            {"Created Date": "2026-08-29T11:59:59Z"},
        ])):
            self.assertEqual(
                app_module.owner_check_whatsapp_window("60115551234", now=now),
                "closed",
            )

    def test_owner_window_without_inbound_history_is_closed(self):
        with patch("app._bubble_records", return_value=iter([])) as records:
            self.assertEqual(
                app_module.owner_check_whatsapp_window("60115551234"), "closed"
            )
        self.assertEqual(
            records.call_args.args[2][1]["value"], "Inbound"
        )

    def test_recent_outbound_only_history_does_not_open_window(self):
        with patch("app._bubble_records", return_value=iter([])) as records:
            window = app_module.owner_check_whatsapp_window("60115551234")
        self.assertEqual(window, "closed")
        self.assertEqual(records.call_args.args[2][1], {
            "key": "direction", "constraint_type": "equals", "value": "Inbound",
        })

    def test_malformed_inbound_created_date_is_unknown(self):
        with patch("app._bubble_records", return_value=iter([
            {"Created Date": "not-a-date"},
        ])):
            self.assertEqual(
                app_module.owner_check_whatsapp_window("60115551234"), "unknown"
            )

    def test_closed_window_uses_template_and_marks_sent_only_on_success(self):
        enquiry = self.enquiry(
            OwnerCheckStatus="Pending", OwnerCheckPhone="60115551234",
        )
        now = datetime(2026, 8, 30, 4, 5, tzinfo=timezone.utc)
        with patch.dict(os.environ, {
                 "WHATSAPP_OWNER_CHECK_TEMPLATE_NAME": "owner_check",
                 "WHATSAPP_OWNER_CHECK_TEMPLATE_LANGUAGE": "en_US",
             }), patch("app.bubble", return_value={"name": "One Menerung"}), \
             patch("app.owner_check_whatsapp_window", return_value="closed"), \
             patch("app.send_whatsapp_text") as freeform, \
             patch("app.send_whatsapp_template", return_value=["wamid.template"]) as template, \
             patch("app._bubble_create", return_value="message-owner") as create, \
             patch("app._bubble_patch") as update, \
             patch("builtins.print") as logged:
            result = app_module.send_pending_owner_check(
                {"_id": "lead-1"}, enquiry, now=now
            )
        self.assertTrue(result)
        freeform.assert_not_called()
        template.assert_called_once_with(
            "60115551234", "owner_check", "en_US"
        )
        persisted = create.call_args.args[2]
        self.assertEqual(persisted["phone"], "60115551234")
        self.assertEqual(persisted["direction"], "Outbound")
        self.assertEqual(persisted["own_Sent?"], "No")
        self.assertEqual(persisted["whatsappMessageId"], "wamid.template")
        self.assertIn("still available", persisted["messageContent"])
        self.assertEqual(persisted["lead"], "lead-1")
        update.assert_called_once()
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn("window=closed method=template", logs)

    def test_template_failure_or_missing_config_leaves_pending(self):
        for configured, failure in ((True, RuntimeError("Meta down")), (False, None)):
            with self.subTest(configured=configured):
                enquiry = self.enquiry(
                    OwnerCheckStatus="Pending", OwnerCheckPhone="60115551234",
                )
                environment = (
                    {"WHATSAPP_OWNER_CHECK_TEMPLATE_NAME": "owner_check"}
                    if configured else {}
                )
                with patch.dict(
                         os.environ, environment, clear=False
                     ), patch("app.bubble", return_value={"name": "One Menerung"}), \
                     patch("app.owner_check_whatsapp_window", return_value="closed"), \
                     patch("app.send_whatsapp_template", side_effect=failure) as template, \
                     patch("app._bubble_patch") as update:
                    if not configured:
                        os.environ.pop("WHATSAPP_OWNER_CHECK_TEMPLATE_NAME", None)
                    result = app_module.send_pending_owner_check(
                        {"_id": "lead-1"}, enquiry
                    )
                self.assertFalse(result)
                self.assertEqual(enquiry["OwnerCheckStatus"], "Pending")
                self.assertNotIn("OwnerCheckSentAt", enquiry)
                update.assert_not_called()
                if configured:
                    template.assert_called_once()
                else:
                    template.assert_not_called()

    def test_message_persistence_failure_after_meta_does_not_resend(self):
        enquiry = self.enquiry(
            OwnerCheckStatus="Pending", OwnerCheckPhone="60115551234",
        )
        now = datetime(2026, 8, 30, 4, 5, tzinfo=timezone.utc)
        with patch("app.bubble", return_value={"name": "One Menerung"}), \
             patch("app.owner_check_whatsapp_window", return_value="open"), \
             patch("app.send_whatsapp_text", return_value=["wamid.accepted"]) as send, \
             patch("app._bubble_create", side_effect=RuntimeError("Bubble down")), \
             patch("app._bubble_patch") as update, \
             patch("builtins.print") as logged:
            first = app_module.send_pending_owner_check(
                {"_id": "lead-1"}, enquiry, now=now
            )
            second = app_module.send_pending_owner_check(
                {"_id": "lead-1"}, enquiry, now=now
            )
        self.assertTrue(first)
        self.assertFalse(second)
        send.assert_called_once()
        update.assert_called_once()
        self.assertEqual(enquiry["OwnerCheckStatus"], "Sent")
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn("action=message_persistence_failed", logs)
        self.assertIn("preserving_sent_state=true", logs)

    def test_owner_check_uses_enquiry_principal_conversation(self):
        enquiry = self.enquiry(
            Principal="principal-1", OwnerCheckStatus="Pending",
            OwnerCheckPhone="60115551234",
        )
        conversation_record = {"_id": "conversation-1"}
        with patch("app.bubble", return_value={"name": "One Menerung"}), \
             patch("app.conversation_store.find_or_create_conversation",
                   return_value=(conversation_record, True)) as find_conversation, \
             patch("app.owner_check_whatsapp_window", return_value="open"), \
             patch("app.send_whatsapp_text", return_value=["wamid.owner"]), \
             patch("app._bubble_create", return_value="message-owner") as create, \
             patch("app.conversation_store.update_conversation_last_outbound_at") as activity, \
             patch("app._bubble_patch"):
            result = app_module.send_pending_owner_check(
                {"_id": "lead-1"}, enquiry
            )
        self.assertTrue(result)
        find_conversation.assert_called_once_with(
            "principal-1", "60115551234", enquiry_id="enquiry-1",
            counterparty_role="Owner Representative",
            rentee_role="Tenant Introducing Agent", subject="One Menerung",
            bubble_env="live", side="owner",
        )
        payload = create.call_args.args[2]
        self.assertEqual(payload["Conversation"], "conversation-1")
        activity.assert_called_once_with("conversation-1", "live")

    @patch("app.requests.post")
    def test_owner_template_uses_meta_template_payload(self, post):
        post.return_value.status_code = 200
        post.return_value.raise_for_status.return_value = None
        post.return_value.json.return_value = {
            "messages": [{"id": "wamid.template"}],
        }
        ids = app_module.send_whatsapp_template(
            "+60 11-555 1234", "owner_check", "en_US"
        )
        self.assertEqual(ids, ["wamid.template"])
        self.assertEqual(post.call_args.kwargs["json"], {
            "messaging_product": "whatsapp",
            "to": "60115551234",
            "type": "template",
            "template": {
                "name": "owner_check",
                "language": {"code": "en_US"},
            },
        })

    def test_sent_and_replied_owner_checks_do_not_resend(self):
        for status in ("Sent", "Replied"):
            with self.subTest(status=status), \
                 patch("app.bubble") as get, \
                 patch("app.send_whatsapp_text") as send, \
                 patch("app._bubble_patch") as update:
                result = app_module.send_pending_owner_check(
                    {"_id": "lead-1"},
                    self.enquiry(
                        OwnerCheckStatus=status,
                        OwnerCheckPhone="60115551234",
                    ),
                )
            self.assertFalse(result)
            get.assert_not_called()
            send.assert_not_called()
            update.assert_not_called()

    def test_owner_message_omits_unknown_profile_facts(self):
        message = app_module.build_owner_check_message(
            {"TransactionType": ["Rent/Let"]},
            {"nationality": "Malaysian"},
            {"name": "The Estate", "beds": 2},
        )
        self.assertIn("Malaysian", message)
        for invented in ("children", "pets", "occupation", "budget", "moving"):
            self.assertNotIn(invented, message.lower())

    def test_direct_and_representative_lead_conversation_roles(self):
        cases = (
            ("No", "Lead", "Lead Advisor"),
            ("Yes", "Lead Representative", "Lead Representative Coordinator"),
        )
        for agent_value, counterparty_role, rentee_role in cases:
            with self.subTest(agent_value=agent_value), \
                 patch("app.bubble", return_value={
                     "_id": "enquiry-1", "Lead": "lead-1",
                     "Principal": "principal-1", "Agent?": agent_value,
                 }), patch(
                     "app.conversation_store.find_or_create_conversation",
                     return_value=({"_id": "conversation-1"}, False),
                 ) as find:
                result = app_module.find_forwarded_lead_conversation(
                    {"_id": "lead-1", "ActiveForwardedEnquiry": "enquiry-1"},
                    "+60 12-345 6789",
                )
            self.assertEqual(result["_id"], "conversation-1")
            find.assert_called_once_with(
                "principal-1", "+60 12-345 6789", enquiry_id="enquiry-1",
                counterparty_role=counterparty_role, rentee_role=rentee_role,
                bubble_env="live", side="lead",
            )

    def test_repeated_lead_conversation_resolution_reuses_identity_helper(self):
        lead = {"_id": "lead-1", "ActiveForwardedEnquiry": "enquiry-1"}
        enquiry = {
            "_id": "enquiry-1", "Lead": "lead-1", "Principal": "principal-1",
            "Agent?": "No",
        }
        with patch("app.bubble", return_value=enquiry), patch(
            "app.conversation_store.find_or_create_conversation",
            return_value=({"_id": "conversation-1"}, False),
        ) as find:
            first = app_module.find_forwarded_lead_conversation(lead, "60123456789")
            second = app_module.find_forwarded_lead_conversation(lead, "60123456789")
        self.assertEqual(first["_id"], second["_id"])
        self.assertEqual(find.call_count, 2)

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-profile")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.send_pending_owner_check")
    @patch("app.prepare_owner_check")
    @patch("app.run_rentee_turn", return_value=(
        app_module.OWNER_CHECK_RESPONSE, "response-1", False,
    ))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.capture_linked_tenant_profile", return_value={"_id": "lead-linked"})
    @patch("app.find_existing_general_conversation_by_phone",
           return_value={"_id": "conversation-general"})
    @patch("app.find_reply_to_conversation", return_value=None)
    @patch("app.send_whatsapp_typing_indicator")
    @patch("app.find_internal_user", return_value=None)
    def test_sufficient_profile_prepares_state_and_sends_only_customer_reply(
        self, _internal, _typing, _reply, _general, _capture, _folio, _turn,
        prepare, owner_send, send, _latest, _create, _save,
    ):
        prepared = {"_id": "enquiry-1", "OwnerCheckStatus": "Pending"}
        prepare.return_value = prepared
        item = webhook_payload(text="Complete profile")["entry"][0]["changes"][0]["value"]["messages"][0]
        app_module._process_whatsapp_message(item)
        prepare.assert_called_once_with({"_id": "lead-linked"}, "live")
        owner_send.assert_called_once_with(
            {"_id": "lead-linked"}, prepared, "live"
        )
        send.assert_called_once_with("60123456789", app_module.OWNER_CHECK_RESPONSE)


class WhatsAppTests(unittest.TestCase):
    def setUp(self):
        app_module._whatsapp_processing_ids.clear()
        app_module._whatsapp_processed_ids.clear()
        app_module._whatsapp_processed_order.clear()
        app_module._whatsapp_phone_locks.clear()
        self.internal_user_patcher = patch("app.find_internal_user", return_value=None)
        self.mocked_internal_user = self.internal_user_patcher.start()
        self.addCleanup(self.internal_user_patcher.stop)
        direct_persistence_tests = {
            "test_inbound_message_persists_whatsapp_fields_once",
            "test_duplicate_inbound_meta_id_reuses_existing_message",
            "test_inbound_message_stores_conversation_and_updates_activity",
            "test_new_message_creation_rejects_missing_conversation",
        }
        if self._testMethodName not in direct_persistence_tests:
            self.inbound_patcher = patch(
                "app.persist_inbound_whatsapp_message",
                return_value=("inbound-message", True),
            )
            self.mocked_inbound = self.inbound_patcher.start()
            self.addCleanup(self.inbound_patcher.stop)
        self.attach_inbound_patcher = patch("app.attach_whatsapp_message_lead")
        self.mocked_attach_inbound = self.attach_inbound_patcher.start()
        self.addCleanup(self.attach_inbound_patcher.stop)
        self.forwarded_conversation_patcher = patch(
            "app.find_forwarded_lead_conversation", return_value=None
        )
        self.mocked_forwarded_conversation = (
            self.forwarded_conversation_patcher.start()
        )
        self.addCleanup(self.forwarded_conversation_patcher.stop)
        self.reply_conversation_patcher = patch(
            "app.find_reply_to_conversation", return_value=None
        )
        self.reply_conversation_patcher.start()
        self.addCleanup(self.reply_conversation_patcher.stop)
        self.active_phone_conversation_patcher = patch(
            "app.find_active_conversation_by_phone",
            return_value=(None, "none", 0),
        )
        self.active_phone_conversation_patcher.start()
        self.addCleanup(self.active_phone_conversation_patcher.stop)
        self.general_conversation_patcher = patch(
            "app.find_general_conversation",
            return_value={"_id": "conversation-general"},
        )
        self.general_conversation_patcher.start()
        self.addCleanup(self.general_conversation_patcher.stop)
        self.existing_general_patcher = patch(
            "app.find_existing_general_conversation_by_phone",
            return_value={"_id": "conversation-general"},
        )
        self.existing_general_patcher.start()
        self.addCleanup(self.existing_general_patcher.stop)
        self.persist_sent_patcher = patch(
            "app.persist_sent_whatsapp_text", return_value="message-outbound"
        )
        self.persist_sent_patcher.start()
        self.addCleanup(self.persist_sent_patcher.stop)

    def test_webhook_verification_accepts_valid_token_and_rejects_invalid(self):
        client = app_module.app.test_client()
        valid = client.get("/whatsapp/webhook", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-123",
        })
        invalid = client.get("/whatsapp/webhook", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "wrong",
            "hub.challenge": "challenge-123",
        })
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.get_data(as_text=True), "challenge-123")
        self.assertEqual(invalid.status_code, 403)

    def test_phone_normalization_equates_common_formats(self):
        values = ["+60123456789", "60123456789", "60 12 345 6789", "+60-12-345-6789"]
        self.assertEqual({app_module.normalize_phone(value) for value in values}, {"60123456789"})

    def test_reply_to_resolves_exact_message_conversation(self):
        self.reply_conversation_patcher.stop()
        with patch("app._bubble_records", return_value=iter([{
            "_id": "message-old", "Conversation": "conversation-exact",
        }])) as records, patch("app.bubble", return_value={
            "_id": "conversation-exact", "Enquiry": "enquiry-2",
        }):
            result = app_module.find_reply_to_conversation({
                "context": {"id": "wamid.replied-to"},
            })
        self.assertEqual(result["_id"], "conversation-exact")
        self.assertEqual(records.call_args.args[2], [{
            "key": "whatsappMessageId", "constraint_type": "equals",
            "value": "wamid.replied-to",
        }])

    def test_single_active_phone_conversation_resolves_without_principal_input(self):
        self.active_phone_conversation_patcher.stop()
        conversation = {
            "_id": "conversation-owner", "Principal": "principal-1",
            "CounterParty Phone": "60123456789", "Status": "Active",
            "Enquiry": "enquiry-1", "Lead": "lead-1", "Listing": "listing-1",
        }
        with patch("app._bubble_records", return_value=iter([conversation])) as records:
            result, resolution, count = app_module.find_active_conversation_by_phone(
                "+60 12-345 6789", "It is available"
            )
        self.assertEqual(result, conversation)
        self.assertEqual(result["Principal"], "principal-1")
        self.assertEqual((resolution, count), ("single_active", 1))
        self.assertEqual(records.call_args.args[2], [
            {"key": "CounterParty Phone", "constraint_type": "equals",
             "value": "60123456789"},
            {"key": "Status", "constraint_type": "equals", "value": "Active"},
        ])

    def test_multiple_active_phone_conversations_are_not_arbitrarily_selected(self):
        self.active_phone_conversation_patcher.stop()
        candidates = [
            {"_id": "conversation-1", "Principal": "principal-1",
             "CounterParty Phone": "60123456789", "Status": "Active",
             "Enquiry": "enquiry-1", "Subject": "One Menerung"},
            {"_id": "conversation-2", "Principal": "principal-1",
             "CounterParty Phone": "60123456789", "Status": "Active",
             "Enquiry": "enquiry-2", "Subject": "The Loft"},
        ]
        with patch("app._bubble_records", return_value=iter(candidates)):
            result, resolution, count = app_module.find_active_conversation_by_phone(
                "60123456789", "Yes, it is available"
            )
        self.assertIsNone(result)
        self.assertEqual((resolution, count), ("ambiguous", 2))

    def test_multiple_active_conversations_can_use_explicit_property_reference(self):
        self.active_phone_conversation_patcher.stop()
        candidates = [
            {"_id": "conversation-1", "CounterParty Phone": "60123456789",
             "Status": "Active", "Enquiry": "enquiry-1",
             "Subject": "One Menerung"},
            {"_id": "conversation-2", "CounterParty Phone": "60123456789",
             "Status": "Active", "Enquiry": "enquiry-2",
             "Subject": "The Loft"},
        ]
        with patch("app._bubble_records", return_value=iter(candidates)):
            result, resolution, count = app_module.find_active_conversation_by_phone(
                "60123456789", "The Loft is still available"
            )
        self.assertEqual(result["_id"], "conversation-2")
        self.assertEqual((resolution, count), ("explicit_reference", 2))

    def test_new_message_creation_rejects_missing_conversation(self):
        with patch("app._bubble_records", return_value=iter([])), \
             patch("app._bubble_create") as create:
            with self.assertRaises(ValueError):
                app_module.persist_inbound_whatsapp_message(
                    "60123456789", "wamid.new", "Hello"
                )
            with self.assertRaises(ValueError):
                app_module.create_whatsapp_ai_message("lead-1", "60123456789")
        create.assert_not_called()

    def test_successful_ad_hoc_outbound_persists_conversation_and_activity(self):
        self.persist_sent_patcher.stop()
        with patch("app._bubble_create", return_value="message-1") as create, \
             patch("app.conversation_store.update_conversation_last_outbound_at") as activity:
            result = app_module.persist_sent_whatsapp_text(
                "60123456789", "Thanks", ["wamid.out"], "conversation-1",
                "lead-1",
            )
        self.assertEqual(result, "message-1")
        payload = create.call_args.args[2]
        self.assertEqual(payload["Conversation"], "conversation-1")
        self.assertEqual(payload["direction"], "Outbound")
        activity.assert_called_once_with("conversation-1", "live")

    @patch("app.requests.get")
    def test_bubble_get_authenticates_and_preserves_caller_headers(self, mocked_get):
        mocked_get.return_value.json.return_value = {"response": {"ok": True}}
        mocked_get.return_value.raise_for_status.return_value = None
        result = app_module.bubble(
            "https://bubble.test/api/1.1/obj/lead",
            headers={
                "X-Request-Source": "whatsapp",
                "Authorization": "Bearer caller-must-not-replace-server-token",
            },
            params={"cursor": 0},
        )
        self.assertEqual(result, {"ok": True})
        headers = mocked_get.call_args.kwargs["headers"]
        self.assertEqual(
            headers["Authorization"], f"Bearer {app_module.BUBBLE_API_TOKEN}"
        )
        self.assertEqual(headers["X-Request-Source"], "whatsapp")
        self.assertEqual(mocked_get.call_args.kwargs["params"], {"cursor": 0})

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble")
    def test_new_whatsapp_phone_creates_one_lead(
        self, mocked_bubble, _mocked_find, mocked_create
    ):
        mocked_bubble.return_value = {
            "_id": "lead-new", "phone": "60123456789", "searchBriefJSON": ""
        }
        lead, created = app_module.find_or_create_whatsapp_lead("+60 12-345-6789", "Aisha")
        self.assertTrue(created)
        self.assertEqual(lead["_id"], "lead-new")
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "lead", {"phone": "60123456789"}
        )

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble", return_value={"_id": "lead-new"})
    def test_new_handoff_lead_stores_text_agent_classification(
        self, _bubble, _find, mocked_create
    ):
        lead, created = app_module.find_or_create_whatsapp_lead(
            "+60 12-345-6789", agent_classification="Yes"
        )
        self.assertTrue(created)
        self.assertEqual(lead["Agent?"], "Yes")
        self.assertEqual(mocked_create.call_args.args[2], {
            "phone": "60123456789", "Agent?": "Yes",
        })

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble", return_value={"_id": "lead-new"})
    def test_new_handoff_lead_uses_confirmed_name_field(
        self, _bubble, _find, mocked_create
    ):
        lead, _created = app_module.find_or_create_whatsapp_lead(
            "60123456789", customer_name="Sarah Lim (Agent)",
            agent_classification="Yes",
        )
        self.assertEqual(lead["name"], "Sarah Lim (Agent)")
        self.assertEqual(mocked_create.call_args.args[2]["name"], "Sarah Lim (Agent)")

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble", return_value={"_id": "lead-new"})
    def test_new_handoff_lead_uses_owner_relationship_field(
        self, _bubble, _find, mocked_create
    ):
        lead, created = app_module.find_or_create_whatsapp_lead(
            "60123456789", agent_classification="No",
            owner_user_id="user-gwen",
        )
        self.assertTrue(created)
        self.assertEqual(lead["owner"], "user-gwen")
        self.assertEqual(mocked_create.call_args.args[2]["owner"], "user-gwen")
        self.assertNotIn("Agent", mocked_create.call_args.args[2])

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble", return_value={"_id": "lead-new"})
    def test_owner_is_independent_from_agent_classification(
        self, _bubble, _find, mocked_create
    ):
        lead, created = app_module.find_or_create_whatsapp_lead(
            "60123456789", agent_classification="Yes",
            owner_user_id="user-gwen",
        )
        self.assertTrue(created)
        self.assertEqual(lead["owner"], "user-gwen")
        self.assertEqual(lead["Agent?"], "Yes")
        self.assertEqual(mocked_create.call_args.args[2], {
            "phone": "60123456789", "Agent?": "Yes", "owner": "user-gwen",
        })

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble", return_value={"_id": "lead-new"})
    def test_new_lead_continues_when_bubble_hides_phone_on_read(
        self, _mocked_bubble, _mocked_find, _mocked_create
    ):
        with patch("builtins.print") as mocked_print:
            lead, created = app_module.find_or_create_whatsapp_lead("60123456789")
        self.assertTrue(created)
        self.assertEqual(lead["_id"], "lead-new")
        self.assertEqual(lead["phone"], "60123456789")
        self.assertEqual(lead["searchBriefJSON"], "")
        self.assertIn(
            "phone is not readable",
            "\n".join(str(call) for call in mocked_print.call_args_list),
        )

    @patch("app.bubble")
    def test_exact_phone_constraint_reuses_lead_when_phone_is_hidden(self, mocked_bubble):
        mocked_bubble.side_effect = [
            {"results": [{"_id": "lead-existing"}], "remaining": 0},
            {"_id": "lead-existing", "searchBriefJSON": ""},
        ]
        lead = app_module.find_lead_by_phone("60123456789")
        self.assertEqual(lead["_id"], "lead-existing")
        self.assertEqual(lead["phone"], "60123456789")

    @patch("app._bubble_create")
    @patch("app.find_lead_by_phone")
    def test_existing_whatsapp_phone_reuses_lead(self, mocked_find, mocked_create):
        mocked_find.return_value = {"_id": "lead-existing", "phone": "+60 12 345 6789"}
        lead, created = app_module.find_or_create_whatsapp_lead("60123456789")
        self.assertFalse(created)
        self.assertEqual(lead["_id"], "lead-existing")
        mocked_create.assert_not_called()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_status_callback_is_ignored(self, mocked_process):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}],
        }
        response = app_module.app.test_client().post("/whatsapp/webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        mocked_process.assert_not_called()

    @patch("app.requests.post")
    def test_typing_indicator_uses_inbound_wamid_and_native_meta_payload(
        self, mocked_post
    ):
        mocked_post.return_value.status_code = 200
        mocked_post.return_value.raise_for_status.return_value = None

        app_module.send_whatsapp_typing_indicator("wamid.inbound-1")

        mocked_post.assert_called_once_with(
            "https://graph.facebook.com/v23.0/phone-number-id/messages",
            headers={
                "Authorization": "Bearer wa-token",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": "wamid.inbound-1",
                "typing_indicator": {"type": "text"},
            },
            timeout=30,
        )

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_duplicate_webhook_dispatches_message_only_once(self, mocked_process):
        client = app_module.app.test_client()
        payload = webhook_payload(message_id="wamid.duplicate")

        first = client.post("/whatsapp/webhook", json=payload)
        second = client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        mocked_process.assert_called_once()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_text_webhook_acknowledges_and_dispatches_customer_text(self, mocked_process):
        response = app_module.app.test_client().post(
            "/whatsapp/webhook", json=webhook_payload()
        )
        self.assertEqual(response.status_code, 200)
        message = mocked_process.call_args.args[0]
        self.assertEqual(message["from"], "60123456789")
        self.assertEqual(message["text"]["body"], "Hi")
        self.assertEqual(message["customer_name"], "Aisha")

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_audio_webhook_extracts_media_id_and_dispatches(self, mocked_process):
        response = app_module.app.test_client().post(
            "/whatsapp/webhook", json=audio_webhook_payload()
        )
        self.assertEqual(response.status_code, 200)
        message = mocked_process.call_args.args[0]
        self.assertEqual(message["type"], "audio")
        self.assertEqual(message["audio"]["id"], "media-1")
        self.assertEqual(message["customer_name"], "Aisha")

    @patch("app.requests.get")
    def test_download_whatsapp_audio_fetches_meta_url_and_bytes(self, get):
        metadata = MagicMock()
        metadata.json.return_value = {
            "url": "https://lookaside.facebook.test/audio",
            "mime_type": "audio/ogg",
        }
        metadata.raise_for_status.return_value = None
        audio = MagicMock()
        audio.content = b"voice-bytes"
        audio.headers = {"Content-Type": "audio/ogg"}
        audio.raise_for_status.return_value = None
        get.side_effect = [metadata, audio]

        content, mime_type = app_module.download_whatsapp_audio("media-1")

        self.assertEqual(content, b"voice-bytes")
        self.assertEqual(mime_type, "audio/ogg")
        self.assertEqual(get.call_args_list[0].args[0],
                         "https://graph.facebook.com/v23.0/media-1")
        self.assertEqual(get.call_args_list[1].args[0],
                         "https://lookaside.facebook.test/audio")
        for call in get.call_args_list:
            self.assertEqual(
                call.kwargs["headers"], {"Authorization": "Bearer wa-token"}
            )

    @patch("app.download_whatsapp_audio",
           return_value=(b"voice-bytes", "audio/ogg"))
    def test_transcription_uses_dedicated_audio_api(self, _download):
        with patch.object(
            app_module.client.audio.transcriptions, "create",
            return_value=SimpleNamespace(text="  Three bedrooms in KLCC  "),
        ) as create:
            transcript = app_module.transcribe_whatsapp_audio(
                "media-1", "wamid.audio-1"
            )
        self.assertEqual(transcript, "Three bedrooms in KLCC")
        self.assertEqual(
            create.call_args.kwargs["model"], "gpt-4o-mini-transcribe"
        )
        self.assertEqual(create.call_args.kwargs["file"][1], b"voice-bytes")

    @patch("app.download_whatsapp_audio",
           return_value=(b"voice-bytes", "audio/ogg"))
    def test_empty_openai_transcription_is_rejected(self, _download):
        with patch.object(
            app_module.client.audio.transcriptions, "create",
            return_value=SimpleNamespace(text="   "),
        ):
            with self.assertRaises(ValueError):
                app_module.transcribe_whatsapp_audio("media-1", "wamid.audio-1")

    @patch("app.send_whatsapp_typing_indicator")
    @patch("app.send_whatsapp_text", return_value=["wamid.reply"])
    @patch("app.handle_internal_user_message")
    @patch("app.transcribe_whatsapp_audio",
           return_value="Show me three bedrooms in KLCC")
    def test_audio_transcript_enters_existing_text_workflow(
        self, transcribe, workflow, _send, _typing,
    ):
        self.mocked_internal_user.return_value = {"_id": "user-1"}
        result = SimpleNamespace(
            handled=True, response_text="Okay", complete=MagicMock(),
        )
        workflow.return_value = result
        item = audio_webhook_payload()["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        transcribe.assert_called_once_with("media-1", "wamid.audio-1")
        self.assertEqual(workflow.call_args.args[1],
                         "Show me three bedrooms in KLCC")

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.send_whatsapp_typing_indicator")
    @patch("app.send_whatsapp_text", return_value=["wamid.reply"])
    @patch("app.handle_internal_user_message")
    @patch("app.transcribe_whatsapp_audio", return_value="Agent enquiry coming")
    def test_duplicate_audio_webhook_transcribes_and_responds_once(
        self, transcribe, workflow, send, _typing,
    ):
        self.mocked_internal_user.return_value = {"_id": "user-1"}
        workflow.return_value = SimpleNamespace(
            handled=True, response_text="Send it through", complete=MagicMock(),
        )
        client = app_module.app.test_client()
        payload = audio_webhook_payload(message_id="wamid.audio-duplicate")

        client.post("/whatsapp/webhook", json=payload)
        client.post("/whatsapp/webhook", json=payload)

        transcribe.assert_called_once()
        send.assert_called_once_with("60123456789", "Send it through")

    def test_failed_or_empty_audio_transcription_sends_safe_response(self):
        item = audio_webhook_payload()["entry"][0]["changes"][0]["value"]["messages"][0]
        for failure in (RuntimeError("OpenAI down"), ValueError("empty")):
            with self.subTest(error=type(failure).__name__), \
                 patch("app.send_whatsapp_typing_indicator"), \
                 patch("app.transcribe_whatsapp_audio", side_effect=failure), \
                 patch("app.send_whatsapp_text") as send:
                app_module._process_whatsapp_message(item)
            send.assert_called_once_with(
                "60123456789", app_module.WHATSAPP_AUDIO_ERROR_RESPONSE
            )
            app_module._whatsapp_processed_ids.clear()
            app_module._whatsapp_processed_order.clear()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_unsupported_whatsapp_message_type_is_ignored(self, process):
        payload = audio_webhook_payload()
        message = payload["entry"][0]["changes"][0]["value"]["messages"][0]
        message["type"] = "image"
        response = app_module.app.test_client().post("/whatsapp/webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        process.assert_not_called()

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_internal_user_handled_message_skips_external_lead_flow(
        self, _typing, mocked_workflow, mocked_send, mocked_lead
    ):
        self.mocked_internal_user.return_value = {
            "_id": "user-1", "phone": "+60 12-345 6789"
        }
        result = MagicMock(
            handled=True, response_text="Sure — send me the agent enquiry."
        )
        mocked_workflow.return_value = result
        item = webhook_payload(text="new agent enquiry coming")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        mocked_workflow.assert_called_once()
        self.assertEqual(
            mocked_workflow.call_args.kwargs["rentee_whatsapp_number"],
            "601112032754",
        )
        mocked_send.assert_called_once_with(
            "60123456789", "Sure — send me the agent enquiry."
        )
        result.complete.assert_called_once_with()
        mocked_lead.assert_not_called()

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_handoff_number_does_not_use_meta_phone_number_id(
        self, _typing, mocked_workflow, _mocked_send, _mocked_lead
    ):
        self.mocked_internal_user.return_value = {"_id": "user-1"}
        mocked_workflow.return_value = MagicMock(handled=True, response_text="Handled")
        item = webhook_payload()["entry"][0]["changes"][0]["value"]["messages"][0]
        with patch.dict(os.environ, {
            "RENTEE_WHATSAPP_NUMBER": "601112032754",
            "WHATSAPP_PHONE_NUMBER_ID": "meta-sender-id",
        }):
            app_module._process_whatsapp_message(item)
        self.assertEqual(
            mocked_workflow.call_args.kwargs["rentee_whatsapp_number"],
            "601112032754",
        )

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_missing_handoff_number_is_passed_as_unconfigured(
        self, _typing, mocked_workflow, _mocked_send, _mocked_lead
    ):
        self.mocked_internal_user.return_value = {"_id": "user-1"}
        mocked_workflow.return_value = MagicMock(handled=True, response_text="Handled")
        item = webhook_payload(message_id="wamid.missing-config")["entry"][0]["changes"][0]["value"]["messages"][0]
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENTEE_WHATSAPP_NUMBER", None)
            app_module._process_whatsapp_message(item)
        self.assertIsNone(
            mocked_workflow.call_args.kwargs["rentee_whatsapp_number"]
        )

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.handle_external_handoff_message")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_internal_user_with_handoff_code_uses_handoff_before_internal_route(
        self, _typing, mocked_internal, mocked_send, mocked_external, mocked_lead
    ):
        self.mocked_internal_user.return_value = {"_id": "user-1"}
        mocked_external.return_value = MagicMock(
            handled=True, response_text="Handoff response"
        )
        item = webhook_payload(text="Please check RNT-7K4M9Q2P")["entry"][0]["changes"][0]["value"]["messages"][0]
        app_module._process_whatsapp_message(item)
        mocked_external.assert_called_once()
        self.assertEqual(
            mocked_external.call_args.kwargs["sender_user_id"], "user-1"
        )
        mocked_internal.assert_not_called()
        mocked_lead.assert_not_called()
        mocked_send.assert_called_once_with("60123456789", "Handoff response")

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_external_handoff_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_valid_external_handoff_skips_normal_lead_flow(
        self, _typing, mocked_handoff, mocked_send, mocked_lead
    ):
        mocked_handoff.return_value = MagicMock(
            handled=True,
            response_text="Hi — I've got your enquiry for this property. I'll help you from here.",
        )
        item = webhook_payload(text="RNT-7K4M9Q2P")["entry"][0]["changes"][0]["value"]["messages"][0]
        app_module._process_whatsapp_message(item)
        mocked_handoff.assert_called_once()
        mocked_send.assert_called_once()
        mocked_lead.assert_not_called()

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.run_rentee_turn")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_external_handoff_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_successful_handoff_sends_acknowledgement_then_profile_request(
        self, _typing, mocked_handoff, mocked_send, mocked_turn, mocked_lead
    ):
        acknowledgement = (
            "Hi — I've got your enquiry for this property. I'll help you from here."
        )
        mocked_handoff.return_value = MagicMock(
            handled=True, response_text=acknowledgement,
            followup_text=app_module.TENANT_PROFILE_REQUEST,
            enquiry_id="enquiry-1",
        )
        item = webhook_payload(
            message_id="wamid.handoff-profile", text="RNT-7K4M9Q2P"
        )["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        self.assertEqual(mocked_send.call_args_list, [
            unittest.mock.call("60123456789", acknowledgement),
            unittest.mock.call(
                "60123456789", app_module.TENANT_PROFILE_REQUEST
            ),
        ])
        mocked_turn.assert_not_called()
        mocked_lead.assert_not_called()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.handle_external_handoff_message")
    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_typing_indicator")
    def test_duplicate_handoff_webhook_sends_profile_request_only_once(
        self, _typing, mocked_send, mocked_handoff
    ):
        mocked_handoff.return_value = MagicMock(
            handled=True, response_text="Acknowledged",
            followup_text=app_module.TENANT_PROFILE_REQUEST,
            enquiry_id="enquiry-1",
        )
        client = app_module.app.test_client()
        payload = webhook_payload(
            message_id="wamid.duplicate-handoff", text="RNT-7K4M9Q2P"
        )

        client.post("/whatsapp/webhook", json=payload)
        client.post("/whatsapp/webhook", json=payload)

        mocked_handoff.assert_called_once()
        self.assertEqual(mocked_send.call_count, 2)
        mocked_send.assert_any_call(
            "60123456789", app_module.TENANT_PROFILE_REQUEST
        )

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.run_rentee_turn")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_external_handoff_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_profile_send_failure_keeps_handoff_and_does_not_run_llm(
        self, _typing, mocked_handoff, mocked_send, mocked_turn, mocked_lead
    ):
        mocked_handoff.return_value = MagicMock(
            handled=True, response_text="Acknowledged",
            followup_text=app_module.TENANT_PROFILE_REQUEST,
            enquiry_id="enquiry-1",
        )
        mocked_send.side_effect = [None, RuntimeError("Meta unavailable")]
        item = webhook_payload(
            message_id="wamid.profile-failure", text="RNT-7K4M9Q2P"
        )["entry"][0]["changes"][0]["value"]["messages"][0]

        with patch("builtins.print") as mocked_print:
            app_module._process_whatsapp_message(item)

        self.assertEqual(mocked_send.call_count, 2)
        self.assertIn(
            "enquiry_id=enquiry-1 tenant_profile_request_failed",
            " ".join(str(call) for call in mocked_print.call_args_list),
        )
        mocked_turn.assert_not_called()
        mocked_lead.assert_not_called()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_completed_duplicate_internal_webhook_is_not_consumed_twice(
        self, _typing, mocked_workflow, mocked_send
    ):
        self.mocked_internal_user.return_value = {
            "_id": "user-1", "phone": "60123456789"
        }
        result = MagicMock(
            handled=True,
            response_text="Got it — I've received the agent enquiry.",
        )
        mocked_workflow.return_value = result
        client = app_module.app.test_client()
        payload = webhook_payload(message_id="wamid.forwarded-enquiry")

        client.post("/whatsapp/webhook", json=payload)
        client.post("/whatsapp/webhook", json=payload)

        mocked_workflow.assert_called_once()
        mocked_send.assert_called_once()
        result.complete.assert_called_once_with()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.get_relationship_names", return_value={})
    @patch("app._bubble_records", return_value=iter([]))
    @patch("app._bubble_patch")
    @patch("app._bubble_create", return_value="enquiry-1")
    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_typing_indicator")
    def test_duplicate_forwarded_webhook_creates_only_one_enquiry(
        self, _typing, mocked_send, mocked_create, _patch, _records, _names
    ):
        self.mocked_internal_user.return_value = {
            "_id": "user-1", "phone": "60123456789",
            "Awaiting Enquiry": True,
            "Pending Enquirer Agent?": "Yes",
            "Awaiting Enquiry Since": datetime.now(timezone.utc).isoformat(),
        }
        client = app_module.app.test_client()
        payload = webhook_payload(
            message_id="wamid.real-forwarded", text="Forwarded enquiry details"
        )

        first = client.post("/whatsapp/webhook", json=payload)
        second = client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        enquiry_creates = [
            call for call in mocked_create.call_args_list if call.args[1] == "enquiry"
        ]
        self.assertEqual(len(enquiry_creates), 1)
        mocked_send.assert_called_once()

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-1")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn", return_value=("Normal reply", "response-1", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead", return_value=({"_id": "lead-1"}, False))
    @patch("app.send_whatsapp_typing_indicator")
    def test_unknown_internal_user_falls_through_to_existing_whatsapp_flow(
        self, _typing, mocked_lead, _folio, mocked_turn, mocked_send,
        _latest, _create, _save
    ):
        item = webhook_payload(text="Find me a home")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        self.mocked_internal_user.assert_called_once()
        mocked_lead.assert_called_once()
        mocked_turn.assert_called_once()
        mocked_send.assert_called_once_with("60123456789", "Normal reply")

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-profile")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn", return_value=("Normal reply", "response-1", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.capture_linked_tenant_profile", return_value={"_id": "lead-linked"})
    @patch("app.send_whatsapp_typing_indicator")
    def test_linked_profile_reply_reuses_lead_and_continues_normal_conversation(
        self, _typing, capture, create_lead, _folio, turn, send,
        _latest, _create_message, _save,
    ):
        text = "British, 2 adults, no pets, budget 12k"
        item = webhook_payload(text=text)["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        capture.assert_called_once_with("60123456789", text, "live")
        create_lead.assert_not_called()
        turn.assert_called_once_with(
            text, "folio-1", previous_response_id=None,
            message_id="message-profile", bubble_env="live",
        )
        send.assert_called_once_with("60123456789", "Normal reply")

    def _assert_profile_capture_failure_still_converses(
        self, *, extraction_error=None, update_error=None,
    ):
        text = "British, budget 12k"
        item = webhook_payload(text=text)["entry"][0]["changes"][0]["value"]["messages"][0]
        extracted = {
            field: (12000 if field == "budgetRent" else None)
            for field in app_module.TENANT_PROFILE_FIELDS
        }
        with patch("app.send_whatsapp_typing_indicator"), \
             patch("app.find_handoff_lead_by_phone", return_value={"_id": "lead-linked"}), \
             patch("app.extract_tenant_profile", return_value=extracted,
                   side_effect=extraction_error), \
             patch("app._bubble_patch", side_effect=update_error), \
             patch("app.find_or_create_whatsapp_lead") as create_lead, \
             patch("app.find_or_create_lead_folio", return_value=("folio-1", False)), \
             patch("app.find_latest_ai_message", return_value=None), \
             patch("app.create_whatsapp_ai_message", return_value="message-profile"), \
             patch("app.run_rentee_turn", return_value=("Normal reply", "resp-1", False)) as turn, \
             patch("app.save_whatsapp_ai_message"), \
             patch("app.send_whatsapp_text") as send:
            app_module._process_whatsapp_message(item)
        create_lead.assert_not_called()
        turn.assert_called_once()
        self.assertEqual(turn.call_args.args[0], text)
        send.assert_called_once_with("60123456789", "Normal reply")

    def test_extractor_exception_still_runs_normal_conversation(self):
        self._assert_profile_capture_failure_still_converses(
            extraction_error=RuntimeError("OpenAI unavailable")
        )

    def test_incomplete_extractor_response_still_runs_normal_conversation(self):
        self._assert_profile_capture_failure_still_converses(
            extraction_error=app_module.TenantProfileExtractionError(
                "response_status=incomplete"
            )
        )

    def test_bubble_profile_update_failure_still_runs_normal_conversation(self):
        self._assert_profile_capture_failure_still_converses(
            update_error=RuntimeError("Bubble unavailable")
        )

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="bubble-message-2")
    @patch("app.find_latest_ai_message")
    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_recommendation_batch")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_typing_indicator")
    def test_message_reuses_lead_context_and_sends_only_clean_final_answer(
        self, mocked_typing, mocked_lead, _mocked_folio, mocked_turn, mocked_batch,
        mocked_send, mocked_latest, _mocked_create, mocked_save
    ):
        events = []
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_typing.side_effect = lambda _message_id: events.append("typing")
        mocked_lead.side_effect = lambda *_args: (events.append("lead") or (lead, False))
        mocked_latest.return_value = {
            "_id": "bubble-message-1", "lead": "lead-1",
            "own_Sent?": "No", "response_ID": "response-1",
        }
        mocked_turn.return_value = (
            "Here are two suitable Bangsar homes.", "response-2", False
        )

        app_module._process_whatsapp_message(
            webhook_payload(text="Find me a 3-bed in Bangsar")["entry"][0]["changes"][0]["value"]["messages"][0]
        )

        mocked_typing.assert_called_once_with("wamid.1")
        self.assertEqual(events[:2], ["typing", "lead"])
        mocked_turn.assert_called_once_with(
            "Find me a 3-bed in Bangsar", "folio-1",
            previous_response_id="response-1",
            message_id="bubble-message-2", bubble_env="live",
        )
        mocked_send.assert_called_once_with(
            "60123456789", "Here are two suitable Bangsar homes."
        )
        mocked_batch.assert_not_called()
        mocked_save.assert_called_once_with(
            "bubble-message-2", "Here are two suitable Bangsar homes.",
            "response-2", "live",
        )
        outgoing = mocked_send.call_args.args[1]
        for leaked in ("function_call", "# to=functions", "match_lead tool",
                       "advance_property_search", "awaiting results"):
            self.assertNotIn(leaked, outgoing)

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", side_effect=[
        "message-1", "message-2", "message-3"
    ])
    @patch("app.find_latest_ai_message", side_effect=[
        None,
        {"_id": "message-1", "lead": "lead-1", "own_Sent?": "No",
         "response_ID": "response-1", "Created Date": "2026-08-28T01:00:00Z"},
        {"_id": "message-2", "lead": "lead-1", "own_Sent?": "No",
         "response_ID": "response-2", "Created Date": "2026-08-28T02:00:00Z"},
    ])
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_typing_indicator")
    def test_three_messages_reuse_latest_persisted_openai_continuation(
        self, mocked_typing, mocked_lead, _folio, mocked_turn, _send, _latest,
        _create, mocked_save
    ):
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_lead.return_value = (lead, False)
        mocked_turn.side_effect = [
            ("First reply", "response-1", False),
            ("Second reply", "response-2", False),
            ("Third reply", "response-3", False),
        ]

        first = webhook_payload("wamid.1", text="Looking in Bangsar")
        second = webhook_payload("wamid.2", text="Budget is 12k")
        third = webhook_payload("wamid.3", text="Rent please")
        app_module._process_whatsapp_message(
            first["entry"][0]["changes"][0]["value"]["messages"][0])
        app_module._process_whatsapp_message(
            second["entry"][0]["changes"][0]["value"]["messages"][0])
        app_module._process_whatsapp_message(
            third["entry"][0]["changes"][0]["value"]["messages"][0])

        self.assertIsNone(mocked_turn.call_args_list[0].kwargs["previous_response_id"])
        self.assertEqual(
            mocked_turn.call_args_list[1].kwargs["previous_response_id"], "response-1"
        )
        self.assertEqual(
            mocked_turn.call_args_list[2].kwargs["previous_response_id"], "response-2"
        )
        self.assertEqual(
            _folio.call_args_list[1].args, ("lead-1", "live")
        )
        self.assertEqual(
            mocked_turn.call_args_list[0].kwargs["message_id"], "message-1"
        )
        self.assertEqual(
            mocked_turn.call_args_list[1].kwargs["message_id"], "message-2"
        )
        self.assertEqual(
            mocked_turn.call_args_list[2].kwargs["message_id"], "message-3"
        )
        self.assertEqual(mocked_save.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in mocked_typing.call_args_list],
            ["wamid.1", "wamid.2", "wamid.3"],
        )

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-current")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn", return_value=("Reply", "resp-1", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead", return_value=({"_id": "lead-1"}, False))
    @patch(
        "app.send_whatsapp_typing_indicator",
        side_effect=requests.RequestException("Meta unavailable"),
    )
    def test_typing_indicator_failure_does_not_block_customer_reply(
        self, mocked_typing, _lead, _folio, mocked_turn, mocked_send, _latest,
        _create, mocked_save
    ):
        item = webhook_payload(message_id="wamid.failure")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        mocked_typing.assert_called_once_with("wamid.failure")
        mocked_turn.assert_called_once()
        mocked_save.assert_called_once()
        mocked_send.assert_called_once_with("60123456789", "Reply")

    @patch("app._bubble_records")
    def test_response_continuity_survives_python_memory_restart(self, mocked_records):
        mocked_records.return_value = iter([{
            "_id": "message-previous", "lead": "lead-a", "own_Sent?": "No",
            "response_ID": "resp-durable", "Created Date": "2026-08-28T03:00:00Z",
        }])
        app_module._whatsapp_processing_ids.clear()
        app_module._whatsapp_phone_locks.clear()
        previous = app_module.find_latest_ai_message("lead-a")
        self.assertEqual(previous["response_ID"], "resp-durable")

    @patch("app.requests.post")
    def test_send_whatsapp_uses_meta_text_endpoint_and_splits_only_long_text(self, mocked_post):
        mocked_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"messages": [{"id": "wamid.out"}]},
        )
        mocked_post.return_value.raise_for_status.return_value = None
        ids = app_module.send_whatsapp_text("+60123456789", "Hello from Rentee")
        self.assertEqual(ids, ["wamid.out"])
        self.assertEqual(mocked_post.call_count, 1)
        self.assertEqual(mocked_post.call_args.kwargs["json"]["to"], "60123456789")
        self.assertEqual(mocked_post.call_args.kwargs["json"]["text"]["body"], "Hello from Rentee")

    def test_listing_image_prefers_cover_photo(self):
        self.assertEqual(
            app_module.get_listing_whatsapp_image({
                "coverPhoto": "https://cdn.test/cover.jpg",
                "photos": ["https://cdn.test/first.jpg"],
            }),
            "https://cdn.test/cover.jpg",
        )

    def test_listing_image_falls_back_to_first_photo(self):
        self.assertEqual(
            app_module.get_listing_whatsapp_image({
                "photos": ["https://cdn.test/first.jpg", "https://cdn.test/second.jpg"]
            }),
            "https://cdn.test/first.jpg",
        )

    def test_listing_image_missing_or_empty_photos_returns_none(self):
        for listing in ({}, {"photos": None}, {"photos": []}):
            with self.subTest(listing=listing):
                self.assertEqual(
                    app_module.get_listing_whatsapp_image(listing), None
                )

    def test_listing_image_malformed_cover_uses_first_valid_photo(self):
        self.assertEqual(
            app_module.get_listing_whatsapp_image({
                "coverPhoto": "not a URL",
                "photos": ["//cdn.test/first.jpg"],
            }),
            "https://cdn.test/first.jpg",
        )

    @patch("app.requests.post")
    def test_send_whatsapp_image_uses_meta_image_payload(self, mocked_post):
        mocked_post.return_value.status_code = 200
        mocked_post.return_value.raise_for_status.return_value = None
        app_module.send_whatsapp_image("+60 12 345 6789", "https://cdn.test/a.jpg")
        payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(payload, {
            "messaging_product": "whatsapp",
            "to": "60123456789",
            "type": "image",
            "image": {"link": "https://cdn.test/a.jpg"},
        })

    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_image")
    def test_recommendation_batch_interleaves_images_and_text_with_cap(
        self, mocked_image, mocked_text
    ):
        events = []
        mocked_image.side_effect = lambda *_args: (
            events.append("image") or MagicMock(status_code=200)
        )
        mocked_text.side_effect = lambda *_args: events.append("text")
        listings = [{
            "listing_id": f"listing-{index}",
            "property_name": f"Home {index}",
            "coverPhoto": f"https://cdn.test/{index}.jpg",
        } for index in range(1, 7)]

        app_module.send_whatsapp_recommendation_batch(
            "60123456789", "folio-1", listings, top_count=6
        )

        self.assertEqual(mocked_image.call_count, 4)
        self.assertEqual(mocked_text.call_count, 6)  # intro + four cards + footer
        self.assertEqual(events, [
            "text", "image", "text", "image", "text",
            "image", "text", "image", "text", "text",
        ])

    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_image")
    def test_missing_and_failed_images_still_send_all_recommendation_text(
        self, mocked_image, mocked_text
    ):
        mocked_image.side_effect = requests.RequestException("Meta rejected image")
        listings = [
            {"listing_id": "a", "property_name": "A",
             "coverPhoto": "https://cdn.test/a.jpg"},
            {"listing_id": "b", "property_name": "B", "photos": []},
            {"listing_id": "c", "property_name": "C",
             "photos": ["https://cdn.test/c.jpg"]},
        ]
        with patch("builtins.print") as mocked_print:
            app_module.send_whatsapp_recommendation_batch(
                "60123456789", "folio-1", listings
            )
        self.assertEqual(mocked_image.call_count, 2)
        self.assertEqual(mocked_text.call_count, 5)  # intro + three cards + footer
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("failed", logs)
        self.assertIn("no image available", logs)

    @patch("app.bubble")
    @patch("app._bubble_records", return_value=iter([]))
    def test_exact_phone_lookup_no_match_returns_none_without_enumeration(
        self, mocked_records, mocked_bubble
    ):
        found = app_module.find_lead_by_phone("60123456789")
        self.assertIsNone(found)
        mocked_records.assert_called_once()
        constraints = mocked_records.call_args.args[2]
        self.assertEqual(constraints, [{
            "key": "phone", "constraint_type": "equals", "value": "60123456789"
        }])
        mocked_bubble.assert_not_called()

    @patch("app._bubble_records")
    def test_exact_phone_lookup_propagates_api_error_without_fallback(
        self, mocked_records
    ):
        mocked_records.side_effect = requests.RequestException("Bubble unavailable")
        with patch("builtins.print") as mocked_print, self.assertRaises(
            requests.RequestException
        ):
            app_module.find_lead_by_phone("60123456789")
        mocked_records.assert_called_once()
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("...6789", logs)
        self.assertNotIn("60123456789", logs)

    @patch("app._bubble_records")
    def test_latest_ai_message_is_lead_scoped_filtered_and_newest(self, mocked_records):
        mocked_records.return_value = iter([
            {"_id": "own", "lead": "lead-a", "own_Sent?": "Yes",
             "direction": "Inbound", "response_ID": "ignore-own",
             "Created Date": "2026-08-28T05:00:00Z"},
            {"_id": "inbound-misflagged", "lead": "lead-a",
             "own_Sent?": "No", "direction": "Inbound",
             "response_ID": "ignore-inbound",
             "Created Date": "2026-08-28T07:00:00Z"},
            {"_id": "empty", "lead": "lead-a", "own_Sent?": "No",
             "response_ID": "", "Created Date": "2026-08-28T04:00:00Z"},
            {"_id": "other", "lead": "lead-b", "own_Sent?": "No",
             "response_ID": "ignore-other", "Created Date": "2026-08-28T06:00:00Z"},
            {"_id": "older", "lead": "lead-a", "own_Sent?": "No",
             "response_ID": "resp-1", "Created Date": "2026-08-28T01:00:00Z"},
            {"_id": "newer", "lead": "lead-a", "own_Sent?": "No",
             "response_ID": "resp-2", "Created Date": "2026-08-28T02:00:00Z"},
        ])
        with patch("builtins.print") as mocked_print:
            latest = app_module.find_latest_ai_message("lead-a")
        self.assertEqual(latest["_id"], "newer")
        self.assertEqual(latest["response_ID"], "resp-2")
        args = mocked_records.call_args.args
        self.assertEqual(args[1], "message")
        constraints = args[2]
        self.assertEqual(constraints, [
            {"key": "lead", "constraint_type": "equals", "value": "lead-a"}
        ])
        self.assertEqual(len(args), 3)
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("messages_fetched=6", logs)
        self.assertIn("eligible_messages=2", logs)
        self.assertIn("own_sent_values=['No', 'Yes']", logs)

    @patch("app._bubble_create", return_value="message-current")
    def test_current_ai_message_uses_existing_bubble_semantics(self, mocked_create):
        message_id = app_module.create_whatsapp_ai_message(
            "lead-a", "+60 12-345 6789", conversation_id="conversation-1"
        )
        self.assertEqual(message_id, "message-current")
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "message",
            {
                "lead": "lead-a", "phone": "60123456789",
                "direction": "Outbound", "own_Sent?": "No",
                "messageContent": "", "Conversation": "conversation-1",
            },
        )

    @patch("app._bubble_create", return_value="inbound-current")
    @patch("app._bubble_records", return_value=iter([]))
    @patch("app.conversation_store.update_conversation_last_inbound_at")
    def test_inbound_message_persists_whatsapp_fields_once(
        self, activity, mocked_records, mocked_create
    ):
        message_id, created = app_module.persist_inbound_whatsapp_message(
            "+60 12-345 6789", "wamid.inbound", "Hello Rentee", "lead-a",
            "conversation-1",
        )
        self.assertEqual((message_id, created), ("inbound-current", True))
        mocked_records.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "message", [{
                "key": "whatsappMessageId", "constraint_type": "equals",
                "value": "wamid.inbound",
            }]
        )
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "message", {
                "phone": "60123456789", "direction": "Inbound",
                "own_Sent?": "Yes", "whatsappMessageId": "wamid.inbound",
                "messageContent": "Hello Rentee", "lead": "lead-a",
                "Conversation": "conversation-1",
            }
        )
        self.assertNotIn("response_ID", mocked_create.call_args.args[2])
        activity.assert_called_once_with("conversation-1", "live")

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_duplicate_inbound_meta_id_reuses_existing_message(
        self, mocked_records, mocked_create
    ):
        mocked_records.return_value = iter([{
            "_id": "inbound-existing", "lead": "lead-a",
            "whatsappMessageId": "wamid.inbound",
        }])
        result = app_module.persist_inbound_whatsapp_message(
            "60123456789", "wamid.inbound", "Retried", "lead-a"
        )
        self.assertEqual(result, ("inbound-existing", False))
        mocked_create.assert_not_called()

    @patch("app.conversation_store.update_conversation_last_inbound_at")
    @patch("app._bubble_create", return_value="inbound-current")
    @patch("app._bubble_records", return_value=iter([]))
    def test_inbound_message_stores_conversation_and_updates_activity(
        self, _records, create, activity
    ):
        result = app_module.persist_inbound_whatsapp_message(
            "60123456789", "wamid.inbound", "Hello", "lead-a",
            "conversation-1", "live",
        )
        self.assertEqual(result, ("inbound-current", True))
        self.assertEqual(
            create.call_args.args[2]["Conversation"], "conversation-1"
        )
        activity.assert_called_once_with("conversation-1", "live")

    @patch("app._bubble_create", return_value="message-current")
    def test_ai_outbound_message_optionally_stores_conversation(self, create):
        app_module.create_whatsapp_ai_message(
            "lead-a", "60123456789", "live", "conversation-1"
        )
        self.assertEqual(
            create.call_args.args[2]["Conversation"], "conversation-1"
        )

    @patch("app._bubble_patch")
    def test_outbound_meta_message_id_is_saved(self, mocked_patch):
        app_module.save_whatsapp_message_id(
            "message-current", "wamid.outbound", "live"
        )
        mocked_patch.assert_called_once_with(
            "https://www.rentee.asia/api/1.1/obj/message/message-current",
            {"whatsappMessageId": "wamid.outbound"},
        )

    @patch("app.save_whatsapp_message_id")
    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-current")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text", return_value=["wamid.outbound"])
    @patch("app.run_rentee_turn", return_value=("Reply", "resp-1", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead",
           return_value=({"_id": "lead-a"}, False))
    @patch("app.send_whatsapp_typing_indicator")
    def test_normal_ai_response_persists_outbound_phone_and_meta_id(
        self, _typing, _lead, _folio, _turn, _send, _latest, create_ai,
        _save_ai, save_meta_id,
    ):
        item = webhook_payload(message_id="wamid.inbound")[
            "entry"
        ][0]["changes"][0]["value"]["messages"][0]
        with patch("builtins.print") as logged:
            app_module._process_whatsapp_message(item)
        create_ai.assert_called_once_with(
            "lead-a", "60123456789", "live", "conversation-general"
        )
        save_meta_id.assert_called_once_with(
            "message-current", "wamid.outbound", "live", "conversation-general"
        )
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn(
            "[WHATSAPP AI SEND] lead_id=lead-a destination=enquirer "
            "phone=...456789",
            logs,
        )
        self.assertNotIn("60123456789", logs)

    @patch("app.conversation_store.set_conversation_previous_response_id")
    @patch("app.save_whatsapp_message_id")
    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-current")
    @patch("app.find_latest_ai_message", return_value={
        "_id": "legacy-message", "response_ID": "legacy-response",
    })
    @patch("app.send_whatsapp_text", return_value=["wamid.outbound"])
    @patch("app.run_rentee_turn", return_value=("Reply", "response-new", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.find_forwarded_lead_conversation")
    @patch("app.send_whatsapp_typing_indicator")
    def test_forwarded_lead_conversation_scopes_messages_and_continuity(
        self, _typing, find_conversation, find_lead, _folio, turn, _send,
        _legacy, create_ai, _save_ai, save_meta_id, save_previous,
    ):
        find_lead.return_value = ({
            "_id": "lead-1", "ActiveForwardedEnquiry": "enquiry-1",
        }, False)
        find_conversation.return_value = {
            "_id": "conversation-1",
            "Previous Response ID": "conversation-response",
        }
        item = webhook_payload(
            message_id="wamid.inbound", text="Is it still available?"
        )["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        self.assertEqual(
            turn.call_args.kwargs["previous_response_id"],
            "conversation-response",
        )
        create_ai.assert_called_once_with(
            "lead-1", "60123456789", "live", "conversation-1"
        )
        associated_inbound_calls = [
            call for call in self.mocked_inbound.call_args_list
            if call.kwargs.get("conversation_id") == "conversation-1"
        ]
        self.assertEqual(len(associated_inbound_calls), 1)
        save_meta_id.assert_called_once_with(
            "message-current", "wamid.outbound", "live", "conversation-1"
        )
        save_previous.assert_called_once_with(
            "conversation-1", "response-new", "live"
        )

    @patch("app.conversation_store.set_conversation_previous_response_id")
    @patch("app.save_whatsapp_message_id")
    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-current")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text", return_value=["wamid.outbound"])
    @patch("app.run_rentee_turn", return_value=("Thanks", "response-new", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.capture_linked_tenant_profile")
    @patch("app.find_active_conversation_by_phone")
    @patch("app.send_whatsapp_typing_indicator")
    def test_owner_inbound_reuses_single_active_conversation_without_lead_owner(
        self, _typing, active, capture, find_lead, _folio, _turn, _send,
        _latest, create_ai, _save_ai, save_meta, _save_previous,
    ):
        owner_conversation = {
            "_id": "conversation-owner", "Principal": "principal-1",
            "CounterParty Phone": "60123456789", "Status": "Active",
            "Enquiry": "enquiry-1", "Lead": "lead-1", "Listing": "listing-1",
            "CounterParty Role": "Owner Representative",
        }
        active.return_value = (owner_conversation, "single_active", 1)
        with patch("app.bubble", return_value={"_id": "lead-1"}), \
             patch("builtins.print") as logged:
            item = webhook_payload(
                message_id="wamid.owner-reply", text="Yes, it is available"
            )["entry"][0]["changes"][0]["value"]["messages"][0]
            app_module._process_whatsapp_message(item)

        capture.assert_not_called()
        find_lead.assert_not_called()
        self.mocked_forwarded_conversation.assert_not_called()
        associated = [
            call for call in self.mocked_inbound.call_args_list
            if call.kwargs.get("conversation_id") == "conversation-owner"
        ]
        self.assertEqual(len(associated), 1)
        self.assertEqual(associated[0].kwargs["lead_id"], "lead-1")
        create_ai.assert_called_once_with(
            "lead-1", "60123456789", "live", "conversation-owner"
        )
        save_meta.assert_called_once_with(
            "message-current", "wamid.outbound", "live", "conversation-owner"
        )
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn("resolution=single_active conversation_id=conversation-owner", logs)

    @patch("app.requests.patch")
    def test_completed_ai_message_saves_answer_and_response_id(self, mocked_patch):
        mocked_patch.return_value.raise_for_status.return_value = None
        app_module.save_whatsapp_ai_message(
            "message-current", "Final answer", "resp-2"
        )
        self.assertTrue(mocked_patch.call_args.args[0].endswith(
            "/obj/message/message-current"
        ))
        self.assertEqual(mocked_patch.call_args.kwargs["json"], {
            "messageContent": "Final answer", "response_ID": "resp-2",
        })

    def test_search_brief_has_no_whatsapp_conversation_state(self):
        state = app_module.load_search_state("")
        self.assertNotIn("channel_state", state)

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_missing_lead_folio_is_created_with_lead_relationship(
        self, mocked_records, mocked_create
    ):
        mocked_records.side_effect = [iter([]), iter([])]
        mocked_create.return_value = "folio-new"
        result = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual(result, ("folio-new", True))
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "folio",
            {"lead": "lead-a", "folioItems": []},
        )

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_existing_folio_is_reused_when_exact_constraint_returns_nothing(
        self, mocked_records, mocked_create
    ):
        mocked_records.side_effect = [iter([]), iter([
            {"_id": "folio-x", "lead": "lead-a", "Created Date": "2026-01-01"}
        ])]
        folio_id, created = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual((folio_id, created), ("folio-x", False))
        mocked_create.assert_not_called()

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_duplicate_folios_choose_existing_deterministically_without_creation(
        self, mocked_records, mocked_create
    ):
        mocked_records.return_value = iter([
            {"_id": "folio-b", "lead": "lead-a", "Created Date": "2026-02-01"},
            {"_id": "folio-a", "lead": "lead-a", "Created Date": "2026-01-01"},
        ])
        with patch("builtins.print") as mocked_print:
            folio_id, created = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual((folio_id, created), ("folio-a", False))
        mocked_create.assert_not_called()
        self.assertIn(
            "duplicate_folios=2",
            "\n".join(str(call) for call in mocked_print.call_args_list),
        )

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_folio_relationship_survives_process_memory_restart(
        self, mocked_records, mocked_create
    ):
        mocked_records.return_value = iter([{"_id": "folio-x", "lead": "lead-a"}])
        app_module._whatsapp_phone_locks.clear()
        app_module._whatsapp_processing_ids.clear()
        folio_id, created = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual((folio_id, created), ("folio-x", False))
        mocked_create.assert_not_called()

    @patch("app._bubble_create")
    @patch("app.bubble", return_value={"_id": "lead-existing", "phone": "60123456789"})
    @patch("app._bubble_records", return_value=iter([{"_id": "lead-existing"}]))
    def test_exact_lookup_makes_whatsapp_reuse_lead_without_duplicate_creation(
        self, _mocked_records, _mocked_bubble, mocked_create
    ):
        lead, created = app_module.find_or_create_whatsapp_lead("+60 12 345 6789")
        self.assertEqual(lead["_id"], "lead-existing")
        self.assertFalse(created)
        mocked_create.assert_not_called()

    def test_build_folio_url_uses_customer_folio_route(self):
        folio_id = "1787842581873x206575709934321660"
        self.assertEqual(
            app_module.build_folio_url(folio_id),
            "https://www.rentee.asia/folio3/1787842581873x206575709934321660",
        )

    @patch("app.get_current_recommendations")
    def test_recommendation_summary_shows_top_three_and_links_to_full_folio(
        self, mocked_current
    ):
        listings = []
        for index, name in enumerate(
            ("One Menerung", "The Loft", "Ken Bangsar", "Serai", "Nadi Bangsar"), 1
        ):
            listings.append({
                "listing_id": f"listing-internal-{index}",
                "condo": f"condo-internal-{index}",
                "property_name": name,
                "priceRent": 10000 + index * 500,
                "beds": 4,
                "recommendation_reason": f"Fits the customer's budget and four-bedroom requirement {index}.",
            })
        mocked_current.return_value = json.dumps({"current_recommendations": listings})

        result = app_module.build_whatsapp_recommendation_summary("folio-active")

        for name in ("One Menerung", "The Loft", "Ken Bangsar"):
            self.assertIn(name, result)
        self.assertNotIn("Serai", result)
        self.assertIn("RM10,500/month", result)
        self.assertIn("4 bed", result)
        self.assertIn("budget and four-bedroom requirement", result)
        self.assertIn("See all 5", result)
        self.assertIn("https://www.rentee.asia/folio3/folio-active", result)
        for index in range(1, 6):
            self.assertNotIn(f"listing-internal-{index}", result)
            self.assertNotIn(f"condo-internal-{index}", result)

    @patch("app.get_current_recommendations", return_value=json.dumps({
        "current_recommendations": []
    }))
    def test_empty_folio_recommendation_summary_returns_none(self, _mocked_current):
        self.assertIsNone(
            app_module.build_whatsapp_recommendation_summary("folio-empty")
        )

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="bubble-message-current")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_recommendation_batch")
    @patch("app.build_whatsapp_recommendation_summary")
    @patch("app.get_current_recommendations")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-active", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_typing_indicator")
    def test_recommendation_turn_uses_existing_folio_and_one_coherent_text_message(
        self, _typing, mocked_lead, mocked_folio, mocked_turn, mocked_current,
        mocked_summary, mocked_batch, mocked_send, _latest, _create, mocked_save
    ):
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_lead.return_value = (lead, False)
        mocked_turn.return_value = ("Model recommendation", "resp-1", True)
        listings = [{"listing_id": "listing-1", "property_name": "One Menerung"}]
        mocked_current.return_value = json.dumps({"current_recommendations": listings})
        mocked_summary.return_value = (
            "Three grounded recommendations\n\n"
            "https://www.rentee.asia/folio3/folio-active"
        )
        item = webhook_payload(text="Show me the properties")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        mocked_folio.assert_called_once_with("lead-1", "live")
        mocked_current.assert_called_once_with(
            "folio-active", "live", include_media=True
        )
        mocked_summary.assert_called_once_with(
            "folio-active", "live", listings=listings
        )
        mocked_batch.assert_called_once_with("60123456789", "folio-active", listings)
        mocked_send.assert_not_called()
        mocked_save.assert_called_once_with(
            "bubble-message-current", mocked_summary.return_value, "resp-1", "live"
        )


if __name__ == "__main__":
    unittest.main()
