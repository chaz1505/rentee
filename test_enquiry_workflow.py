from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock

import enquiry_workflow as workflow


def normalize_phone(value):
    return "".join(character for character in str(value or "") if character.isdigit())


class EnquiryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
        self.patch_user = MagicMock()

    def test_internal_user_phone_matching_tolerates_common_formatting(self):
        formats = ("+60123456789", "+60 12-345 6789", "+60 (12) 345-6789")
        for stored_phone in formats:
            with self.subTest(stored_phone=stored_phone):
                user = {"_id": "user-1", "phone": stored_phone}
                records = MagicMock(side_effect=[iter([]), iter([user])])
                get = MagicMock(return_value=user)
                found = workflow.find_internal_user(
                    "60123456789", "https://bubble.test", records, get,
                    normalize_phone,
                )
                self.assertEqual(found["_id"], "user-1")

    def test_unknown_phone_returns_none(self):
        records = MagicMock(side_effect=[iter([]), iter([
            {"_id": "user-1", "phone": "+60111111111"}
        ])])
        self.assertIsNone(workflow.find_internal_user(
            "60123456789", "https://bubble.test", records, MagicMock(),
            normalize_phone,
        ))

    def test_agent_instruction_variations_are_recognised(self):
        for text in (
            "new agent enquiry coming",
            "agent enquiry next",
            "going to send you an agent enquiry",
            "next one is from an agent",
            "I'm going to forward an agent enquiry",
        ):
            with self.subTest(text=text):
                self.assertEqual(workflow.detect_new_enquiry_instruction(text), "agent")

    def test_lead_instruction_variations_are_recognised(self):
        for text in (
            "new lead enquiry coming",
            "lead enquiry next",
            "going to send you a lead",
            "next one is a lead",
            "I'm going to forward a new lead",
        ):
            with self.subTest(text=text):
                self.assertEqual(workflow.detect_new_enquiry_instruction(text), "lead")

    def test_agent_instruction_sets_state_and_replies(self):
        result = workflow.handle_internal_user_message(
            {"_id": "user-1"}, "new agent enquiry coming",
            "https://bubble.test", self.patch_user, self.now,
        )
        self.assertTrue(result.handled)
        self.assertEqual(result.response_text, "Sure — send me the agent enquiry.")
        payload = self.patch_user.call_args.args[1]
        self.assertEqual(payload[workflow.AWAITING_ENQUIRY_FIELD], True)
        self.assertEqual(payload[workflow.PENDING_AGENT_FIELD], "Yes")
        self.assertTrue(payload[workflow.AWAITING_SINCE_FIELD])

    def test_lead_instruction_sets_no_and_replies(self):
        result = workflow.handle_internal_user_message(
            {"_id": "user-1"}, "I'm going to forward a new lead",
            "https://bubble.test", self.patch_user, self.now,
        )
        self.assertEqual(result.response_text, "Sure — send me the lead enquiry.")
        self.assertEqual(
            self.patch_user.call_args.args[1][workflow.PENDING_AGENT_FIELD], "No"
        )

    def test_valid_pending_agent_message_is_consumed_then_cleared_on_complete(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: (self.now - timedelta(minutes=5)).isoformat(),
        }
        result = workflow.handle_internal_user_message(
            user, "The agent says this lead wants Bangsar",
            "https://bubble.test", self.patch_user, self.now,
        )
        self.assertEqual(result.response_text, "Got it — I've received the agent enquiry.")
        self.patch_user.assert_not_called()
        result.complete()
        self.assertEqual(self.patch_user.call_args.args[1], {
            workflow.AWAITING_ENQUIRY_FIELD: False,
            workflow.PENDING_AGENT_FIELD: "",
            workflow.AWAITING_SINCE_FIELD: None,
        })

    def test_valid_pending_lead_message_is_consumed(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = workflow.handle_internal_user_message(
            user, "Forwarded customer message", "https://bubble.test",
            self.patch_user, self.now,
        )
        self.assertEqual(result.response_text, "Got it — I've received the lead enquiry.")

    def test_expired_state_is_cleared_but_message_is_not_consumed(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: (self.now - timedelta(minutes=31)).isoformat(),
        }
        result = workflow.handle_internal_user_message(
            user, "ordinary internal message", "https://bubble.test",
            self.patch_user, self.now,
        )
        self.assertFalse(result.handled)
        self.assertFalse(
            self.patch_user.call_args.args[1][workflow.AWAITING_ENQUIRY_FIELD]
        )

    def test_unrelated_internal_message_is_unhandled(self):
        result = workflow.handle_internal_user_message(
            {"_id": "user-1"}, "How is the market today?",
            "https://bubble.test", self.patch_user, self.now,
        )
        self.assertFalse(result.handled)
        self.patch_user.assert_not_called()


if __name__ == "__main__":
    unittest.main()
