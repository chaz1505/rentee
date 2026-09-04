import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import requests

os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import conversation


class ConversationTests(unittest.TestCase):
    def test_creates_general_conversation_without_principal(self):
        with patch("conversation._create", return_value="conversation-general") as create:
            result = conversation.create_conversation(
                None, "+60 12-345 6789", counterparty_user_id="user-1",
                lead_id="lead-1", counterparty_role="Lead",
                rentee_role="Lead Advisor", side="general",
            )
        self.assertEqual(result["_id"], "conversation-general")
        self.assertEqual(create.call_args.args[2], {
            "CounterParty Phone": "60123456789",
            "Status": "Active",
            "Counterparty User": "user-1",
            "Lead": "lead-1",
            "CounterParty Role": "Lead",
            "Rentee Role": "Lead Advisor",
        })
        self.assertNotIn("Principal", create.call_args.args[2])

    def test_unassigned_general_conversation_is_reused(self):
        existing = {
            "_id": "conversation-general", "CounterParty Phone": "60123456789",
            "Status": "Active", "Counterparty User": "user-1", "Lead": "lead-1",
        }
        with patch(
            "conversation.find_active_general_conversation", return_value=existing
        ), patch("conversation.create_conversation") as create:
            result, created = conversation.find_or_create_conversation(
                None, "60123456789", counterparty_user_id="user-1",
                lead_id="lead-1", side="general",
            )
        self.assertEqual((result, created), (existing, False))
        create.assert_not_called()

    def test_enquiry_conversation_still_requires_principal(self):
        with self.assertRaisesRegex(ValueError, "requires Principal"):
            conversation.create_conversation(
                None, "60123456789", enquiry_id="enquiry-1",
                counterparty_user_id="user-1", lead_id="lead-1",
            )

    def test_finds_active_enquiry_conversation_by_counterparty_phone(self):
        matching = {
            "_id": "conversation-owner", "Enquiry": "enquiry-1",
            "CounterParty Phone": "60115551234", "Status": "Active",
        }
        wrong = {
            "_id": "conversation-wrong", "Enquiry": "enquiry-other",
            "CounterParty Phone": "60115551234", "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([matching, wrong])) as records:
            result = conversation.find_active_conversations_by_enquiry_phone(
                "enquiry-1", "+60 11-555 1234"
            )
        self.assertEqual(result, [matching])
        self.assertEqual(records.call_args.args[2], [
            {"key": "Enquiry", "constraint_type": "equals", "value": "enquiry-1"},
            {"key": "CounterParty Phone", "constraint_type": "equals",
             "value": "60115551234"},
            {"key": "Status", "constraint_type": "equals", "value": "Active"},
        ])

    def test_enquiry_specific_creation_copies_lead_and_listing(self):
        with patch("conversation._get", return_value={
                 "Lead": "lead-1", "Listing": "listing-1",
             }), patch("conversation.find_active_conversation", return_value=None), \
             patch("conversation._create", return_value="conversation-1") as create:
            result, created = conversation.find_or_create_conversation(
                "user-1", "60115551234", "enquiry-1"
            )
        self.assertTrue(created)
        self.assertEqual(result["Lead"], "lead-1")
        self.assertEqual(result["Listing"], "listing-1")
        payload = create.call_args.args[2]
        self.assertEqual(payload["Lead"], "lead-1")
        self.assertEqual(payload["Listing"], "listing-1")

    def test_existing_conversation_patches_only_missing_denormalized_fields(self):
        existing = {
            "_id": "conversation-1", "Principal": "user-1",
            "CounterParty Phone": "60115551234", "Enquiry": "enquiry-1",
            "Status": "Active",
        }
        with patch("conversation._get", return_value={
                 "Lead": "lead-1", "Listing": "listing-1",
             }), patch("conversation.find_active_conversation",
                       return_value=existing), patch("conversation._patch") as update:
            result, created = conversation.find_or_create_conversation(
                "user-1", "60115551234", "enquiry-1"
            )
        self.assertFalse(created)
        update.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "conversation", "conversation-1",
            {"Lead": "lead-1", "Listing": "listing-1"},
        )
        self.assertEqual(result["Lead"], "lead-1")
        self.assertEqual(result["Listing"], "listing-1")

    def test_existing_lead_and_listing_are_not_overwritten(self):
        existing = {
            "_id": "conversation-1", "Lead": "lead-existing",
            "Listing": "listing-existing",
        }
        with patch("conversation._get", return_value={
                 "Lead": "lead-new", "Listing": "listing-new",
             }), patch("conversation.find_active_conversation",
                       return_value=existing), patch("conversation._patch") as update:
            result, created = conversation.find_or_create_conversation(
                "user-1", "60115551234", "enquiry-1"
            )
        self.assertFalse(created)
        update.assert_not_called()
        self.assertEqual(result["Lead"], "lead-existing")
        self.assertEqual(result["Listing"], "listing-existing")

    def test_lead_and_listing_are_not_conversation_identity_constraints(self):
        with patch("conversation._records", return_value=iter([])) as records:
            conversation.find_active_conversation(
                "user-1", "60115551234", "enquiry-1"
            )
        keys = [constraint["key"] for constraint in records.call_args.args[2]]
        self.assertNotIn("Lead", keys)
        self.assertNotIn("Listing", keys)

    def test_creates_enquiry_specific_conversation(self):
        with patch("conversation._create", return_value="conversation-1") as create:
            result = conversation.create_conversation(
                "user-1", "+60 11-555 1234", "enquiry-1",
                counterparty_role="Owner Representative",
                rentee_role="Tenant Introducing Agent",
            )
        self.assertEqual(result["_id"], "conversation-1")
        self.assertEqual(create.call_args.args[2], {
            "Principal": "user-1",
            "CounterParty Phone": "60115551234",
            "Status": "Active",
            "Enquiry": "enquiry-1",
            "CounterParty Role": "Owner Representative",
            "Rentee Role": "Tenant Introducing Agent",
        })

    def test_reuses_same_active_conversation(self):
        existing = {
            "_id": "conversation-1", "Principal": "user-1",
            "CounterParty Phone": "60115551234", "Enquiry": "enquiry-1",
            "Status": "Active", "Lead": "lead-1", "Listing": "listing-1",
        }
        with patch("conversation._get", return_value={
                 "Lead": "lead-1", "Listing": "listing-1",
             }), patch("conversation._records", return_value=iter([existing])) as records, \
             patch("conversation.create_conversation") as create:
            result, created = conversation.find_or_create_conversation(
                "user-1", "+60 11-555 1234", "enquiry-1"
            )
        self.assertEqual((result, created), (existing, False))
        create.assert_not_called()
        constraints = records.call_args.args[2]
        self.assertIn({
            "key": "CounterParty Phone", "constraint_type": "equals",
            "value": "60115551234",
        }, constraints)
        self.assertNotIn("Counterparty Phone", [item["key"] for item in constraints])

    def test_same_phone_different_enquiry_creates_different_conversation(self):
        wrong = {
            "_id": "conversation-old", "Principal": "user-1",
            "CounterParty Phone": "60115551234", "Enquiry": "enquiry-old",
            "Status": "Active",
        }
        with patch("conversation._get", return_value={
                 "Lead": "lead-new", "Listing": "listing-new",
             }), patch("conversation._records", return_value=iter([wrong])), \
             patch("conversation.create_conversation",
                   return_value={"_id": "conversation-new"}) as create:
            result, created = conversation.find_or_create_conversation(
                "user-1", "60115551234", "enquiry-new"
            )
        self.assertTrue(created)
        self.assertEqual(result["_id"], "conversation-new")
        create.assert_called_once()

    def test_same_phone_different_principal_does_not_match(self):
        wrong = {
            "_id": "conversation-other", "Principal": "user-other",
            "CounterParty Phone": "60115551234", "Enquiry": "enquiry-1",
            "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([wrong])):
            self.assertIsNone(conversation.find_active_conversation(
                "user-1", "60115551234", "enquiry-1"
            ))

    def test_general_conversation_does_not_match_enquiry_specific(self):
        general = {
            "_id": "general", "Principal": "user-1",
            "CounterParty Phone": "60115551234", "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([general])):
            self.assertIsNone(conversation.find_active_conversation(
                "user-1", "60115551234", "enquiry-1"
            ))

    def test_enquiry_specific_does_not_match_general(self):
        specific = {
            "_id": "specific", "Principal": "user-1",
            "CounterParty Phone": "60115551234", "Enquiry": "enquiry-1",
            "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([specific])):
            self.assertIsNone(conversation.find_active_conversation(
                "user-1", "60115551234"
            ))

    def test_activity_and_previous_response_helpers_use_exact_keys(self):
        now = datetime(2026, 8, 31, 3, 4, tzinfo=timezone.utc)
        with patch("conversation._patch") as update:
            inbound = conversation.update_conversation_last_inbound_at(
                "conversation-1", now=now
            )
            outbound = conversation.update_conversation_last_outbound_at(
                "conversation-1", now=now
            )
            previous = conversation.set_conversation_previous_response_id(
                "conversation-1", "response-1"
            )
        self.assertEqual(inbound, {"Last Inbound At": "2026-08-31T03:04:00Z"})
        self.assertEqual(outbound, {"Last Outbound At": "2026-08-31T03:04:00Z"})
        self.assertEqual(previous, {"Previous Response ID": "response-1"})
        self.assertEqual(
            conversation.get_conversation_previous_response_id(previous),
            "response-1",
        )
        self.assertEqual(update.call_count, 3)

    def test_conversation_http_error_log_is_safe_and_actionable(self):
        response = requests.Response()
        response.status_code = 400
        response._content = b'Invalid field for phone 60115551234'
        error = requests.HTTPError("400 Client Error", response=response)
        with patch("conversation.requests.post", side_effect=error), \
             patch("builtins.print") as logged, \
             self.assertRaises(requests.HTTPError):
            conversation.create_conversation(
                "user-1", "60115551234", "enquiry-1"
            )
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn("action=create_started", logs)
        self.assertIn("action=failed operation=create", logs)
        self.assertIn("method=POST object_type=conversation status=400", logs)
        self.assertIn("Invalid field", logs)
        self.assertNotIn("60115551234", logs)
        self.assertNotIn("test-token", logs)


if __name__ == "__main__":
    unittest.main()
