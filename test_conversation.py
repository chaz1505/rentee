import os
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import conversation


class ConversationTests(unittest.TestCase):
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
            "Counterparty Phone": "60115551234",
            "Status": "Active",
            "Enquiry": "enquiry-1",
            "Counterparty Role": "Owner Representative",
            "Rentee Role": "Tenant Introducing Agent",
        })

    def test_reuses_same_active_conversation(self):
        existing = {
            "_id": "conversation-1", "Principal": "user-1",
            "Counterparty Phone": "60115551234", "Enquiry": "enquiry-1",
            "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([existing])), \
             patch("conversation.create_conversation") as create:
            result, created = conversation.find_or_create_conversation(
                "user-1", "+60 11-555 1234", "enquiry-1"
            )
        self.assertEqual((result, created), (existing, False))
        create.assert_not_called()

    def test_same_phone_different_enquiry_creates_different_conversation(self):
        wrong = {
            "_id": "conversation-old", "Principal": "user-1",
            "Counterparty Phone": "60115551234", "Enquiry": "enquiry-old",
            "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([wrong])), \
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
            "Counterparty Phone": "60115551234", "Enquiry": "enquiry-1",
            "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([wrong])):
            self.assertIsNone(conversation.find_active_conversation(
                "user-1", "60115551234", "enquiry-1"
            ))

    def test_general_conversation_does_not_match_enquiry_specific(self):
        general = {
            "_id": "general", "Principal": "user-1",
            "Counterparty Phone": "60115551234", "Status": "Active",
        }
        with patch("conversation._records", return_value=iter([general])):
            self.assertIsNone(conversation.find_active_conversation(
                "user-1", "60115551234", "enquiry-1"
            ))

    def test_enquiry_specific_does_not_match_general(self):
        specific = {
            "_id": "specific", "Principal": "user-1",
            "Counterparty Phone": "60115551234", "Enquiry": "enquiry-1",
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


if __name__ == "__main__":
    unittest.main()
