from datetime import datetime, timezone, timedelta
import unittest
from unittest.mock import MagicMock, patch

import enquiry_workflow as workflow


def normalize_phone(value):
    return "".join(character for character in str(value or "") if character.isdigit())


class EnquiryWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 29, 8, 0, tzinfo=timezone.utc)
        self.patch_user = MagicMock()
        self.create = MagicMock(return_value="enquiry-1")
        self.records = MagicMock(return_value=iter([]))
        self.relationship_names = MagicMock(return_value={})

    def consume(self, user, text):
        return workflow.handle_internal_user_message(
            user, text, "https://bubble.test", self.patch_user, self.now,
            bubble_create=self.create, bubble_records=self.records,
            relationship_names=self.relationship_names,
        )

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

    def test_pending_agent_creates_enquiry_with_text_agent_flag_and_clears_state(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: (self.now - timedelta(minutes=5)).isoformat(),
        }
        forwarded = "The agent says this lead wants Bangsar"
        result = self.consume(user, forwarded)
        self.assertIn("created the enquiry", result.response_text)
        self.create.assert_called_once_with("https://bubble.test", "enquiry", {
            "Agent": "user-1",
            "Agent?": "Yes",
            "Original Enquiry": forwarded,
        })
        payload = self.create.call_args.args[2]
        self.assertIsInstance(payload["Agent?"], str)
        for empty_field in ("Enquirer Phone", "Handoff Code", "Lead", "Listing"):
            self.assertNotIn(empty_field, payload)
        self.assertEqual(self.patch_user.call_args.args[1], {
            workflow.AWAITING_ENQUIRY_FIELD: False,
            workflow.PENDING_AGENT_FIELD: "",
            workflow.AWAITING_SINCE_FIELD: None,
        })

    def test_valid_pending_lead_enquiry_stores_text_no(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        self.consume(user, "Forwarded customer message")
        self.assertEqual(self.create.call_args.args[2]["Agent?"], "No")

    def test_enquiry_creation_failure_keeps_pending_state(self):
        self.create.side_effect = RuntimeError("Bubble unavailable")
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with self.assertRaises(RuntimeError):
            self.consume(user, "Forwarded enquiry")
        self.patch_user.assert_not_called()

    def test_exact_source_url_match_attaches_owned_listing(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "beds": 3, "priceRent": 15000, "availability": True,
            "sourceURL": "https://www.propertyguru.com.my/l/501124208/",
        }
        self.records.return_value = iter([listing])
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(
            user, "See https://www.propertyguru.com.my/l/501124208"
        )
        self.records.assert_called_once_with(
            "https://bubble.test", "listing",
            [{"key": "owner", "constraint_type": "equals", "value": "user-1"}],
        )
        self.assertIn("One Menerung 3-bed at RM15,000", result.response_text)
        self.assertIn(
            ("https://bubble.test/obj/enquiry/enquiry-1", {"Listing": "listing-1"}),
            [call.args for call in self.patch_user.call_args_list],
        )

    def test_matching_never_selects_another_users_listing(self):
        other_listing = {
            "_id": "listing-other", "owner": "user-2", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/501124208",
            "availability": True,
        }
        self.records.return_value = iter([other_listing])
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(
            user, "https://www.propertyguru.com.my/l/501124208"
        )
        self.assertIn("couldn't confidently match", result.response_text)
        self.assertFalse(any(
            "/obj/enquiry/" in call.args[0]
            for call in self.patch_user.call_args_list
        ))

    def test_propertyguru_reference_matches_iproperty_reference(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.iproperty.com.my/l/501124208",
            "availability": True,
        }
        matched, method, ambiguous = workflow.match_owned_listing(
            "https://www.propertyguru.com.my/l/501124208", [listing], {}
        )
        self.assertEqual(matched["_id"], "listing-1")
        self.assertEqual(method, "portal_reference")
        self.assertEqual(ambiguous, [])

    def test_markdown_forwarded_urls_extract_recognised_reference(self):
        text = (
            '[https://www.propertyguru.com.my/l/501124208]'
            '(https://www.propertyguru.com.my/l/501124208 '
            '"https://www.propertyguru.com.my/l/501124208")'
        )
        urls = workflow._extract_urls(text)
        self.assertEqual(urls, ["https://www.propertyguru.com.my/l/501124208"])
        self.assertEqual(
            {workflow._portal_reference(url) for url in urls}, {"501124208"}
        )

    def test_url_normalisation_ignores_scheme_trailing_slash_and_tracking_query(self):
        listing = {
            "_id": "listing-1",
            "sourceURL": "http://www.propertyguru.com.my/l/501124208/?utm_source=test",
        }
        matched, method, _ambiguous = workflow.match_owned_listing(
            "https://www.propertyguru.com.my/l/501124208", [listing], {}
        )
        self.assertEqual(matched, listing)
        self.assertEqual(method, "source_url")

    def test_owner_constrained_listing_without_visible_owner_is_not_discarded(self):
        listing = {
            "_id": "listing-1",  # owner intentionally hidden by Bubble privacy
            "condo": "condo-1", "beds": 3, "priceRent": 15000,
            "sourceURL": "https://www.iproperty.com.my/l/501124208",
            "availability": True,
        }
        self.records.return_value = iter([listing])
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with patch("builtins.print") as mocked_print:
            result = self.consume(
                user, "https://www.propertyguru.com.my/l/501124208"
            )
        self.assertIn("One Menerung", result.response_text)
        self.assertIn(
            ("https://bubble.test/obj/enquiry/enquiry-1", {"Listing": "listing-1"}),
            [call.args for call in self.patch_user.call_args_list],
        )
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("owned_listings_count=1", logs)
        self.assertIn("portal_references=['501124208']", logs)
        self.assertIn(
            "enquiry_id=enquiry-1 listing_id=listing-1 match_method=portal_reference",
            logs,
        )
        self.assertIn("listing_id=listing-1 availability=true", logs)
        self.assertNotIn("[ENQUIRY WORKFLOW] candidate", logs)

    def test_sparse_constrained_listing_is_hydrated_before_matching(self):
        self.records.return_value = iter([{"_id": "listing-1"}])
        bubble_get = MagicMock(return_value={
            "_id": "listing-1", "condo": "condo-1", "beds": 3,
            "priceRent": 15000, "availability": True,
            "sourceURL": "https://www.propertyguru.com.my/l/501124208",
        })
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = workflow.handle_internal_user_message(
            user, "https://www.propertyguru.com.my/l/501124208",
            "https://bubble.test", self.patch_user, self.now,
            bubble_create=self.create, bubble_records=self.records,
            relationship_names=self.relationship_names, bubble_get=bubble_get,
        )
        self.assertIn("One Menerung", result.response_text)
        bubble_get.assert_called_once_with(
            "https://bubble.test/obj/listing/listing-1"
        )

    def test_fallback_condo_beds_and_rent_parsing_matches_exactly(self):
        listing = {
            "_id": "listing-1", "condo": "condo-1", "beds": 3,
            "priceRent": 15000,
        }
        matched, method, _ambiguous = workflow.match_owned_listing(
            "RENT - One Menerung, Bangsar\n3 Beds / RM 15,000 /mo",
            [listing], {"condo-1": "One Menerung"},
        )
        self.assertEqual(matched, listing)
        self.assertEqual(method, "condo_beds_price")
        self.assertEqual(workflow._extract_beds("3 bedrooms"), 3)
        self.assertEqual(workflow._extract_rent("15k per month"), 15000)

    def test_ambiguous_matches_do_not_attach_listing(self):
        listings = [{
            "_id": f"listing-{index}", "owner": "user-1", "condo": "condo-1",
            "beds": 3, "priceRent": 15000, "availability": True,
        } for index in (1, 2)]
        self.records.return_value = iter(listings)
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with patch("builtins.print") as mocked_print:
            result = self.consume(user, "One Menerung 3 beds RM15,000")
        self.assertIn("I found 2", result.response_text)
        enquiry_updates = [
            call for call in self.patch_user.call_args_list
            if "/obj/enquiry/" in call.args[0]
        ]
        self.assertEqual(enquiry_updates, [])
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("ambiguous listing match", logs)
        self.assertIn("method=condo_beds_price", logs)
        self.assertIn("listing_ids=['listing-1', 'listing-2']", logs)
        self.assertNotIn("[ENQUIRY WORKFLOW] candidate", logs)

    def test_no_match_logs_one_concise_summary_without_candidate_dump(self):
        self.records.return_value = iter([{
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "beds": 4, "priceRent": 18000,
            "sourceURL": "https://www.propertyguru.com.my/l/999999",
        }])
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        message = (
            "One Menerung 3 Beds / RM 15,000 /mo "
            "https://www.propertyguru.com.my/l/501124208"
        )
        with patch("builtins.print") as mocked_print:
            result = self.consume(user, message)
        self.assertIn("couldn't confidently match", result.response_text)
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("no listing match", logs)
        self.assertIn("portal_refs=['501124208']", logs)
        self.assertIn("parsed_condos=['One Menerung']", logs)
        self.assertIn("parsed_beds=3.0", logs)
        self.assertIn("parsed_rent=15000.0", logs)
        self.assertIn("owned_listings_count=1", logs)
        self.assertNotIn("[ENQUIRY WORKFLOW] candidate", logs)

    def test_unavailable_match_reports_known_status_without_future_actions(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/12345",
            "beds": 3, "priceRent": 15000, "availability": False,
        }
        self.records.return_value = iter([listing])
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(user, "https://www.propertyguru.com.my/l/12345")
        self.assertIn("already marked unavailable", result.response_text)

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
