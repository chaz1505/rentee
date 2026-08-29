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
            normalize_phone=normalize_phone,
            rentee_whatsapp_number="60115551234",
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
        self.assertIn((
            "https://bubble.test", "listing",
            [
                {"key": "owner", "constraint_type": "equals", "value": "user-1"},
                {"key": "sourceURL", "constraint_type": "text contains",
                 "value": "501124208"},
            ],
        ), [call.args for call in self.records.call_args_list])
        self.assertNotIn((
            "https://bubble.test", "listing",
            [{"key": "owner", "constraint_type": "equals", "value": "user-1"}],
        ), [call.args for call in self.records.call_args_list])
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

    def test_same_reference_from_two_portals_runs_one_fast_lookup(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/501124208",
            "availability": True,
        }
        self.records.side_effect = [iter([listing]), iter([])]
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(user, (
            "https://www.propertyguru.com.my/l/501124208 "
            "https://www.iproperty.com.my/l/501124208"
        ))
        self.assertIn("https://wa.me/", result.response_text)
        listing_calls = [
            call for call in self.records.call_args_list
            if call.args[1] == "listing"
        ]
        self.assertEqual(len(listing_calls), 1)
        self.assertEqual(
            listing_calls[0].args[2][1]["value"], "501124208"
        )

    def test_ambiguous_fast_lookup_falls_back_without_guessing(self):
        listings = [{
            "_id": f"listing-{index}", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/501124208",
            "availability": True,
        } for index in (1, 2)]
        self.records.side_effect = [iter(listings), iter(listings)]
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(
            user, "https://www.propertyguru.com.my/l/501124208"
        )
        self.assertIn("I found 2", result.response_text)
        self.assertFalse(any(
            call.args[0].endswith("/obj/enquiry/enquiry-1")
            and "Listing" in call.args[1]
            for call in self.patch_user.call_args_list
        ))
        listing_constraints = [
            call.args[2] for call in self.records.call_args_list
            if call.args[1] == "listing"
        ]
        self.assertEqual(len(listing_constraints), 2)
        self.assertEqual(len(listing_constraints[0]), 2)
        self.assertEqual(len(listing_constraints[1]), 1)

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
        self.assertNotIn("owned_listings_count=", logs)
        self.assertIn("portal_references=['501124208']", logs)
        self.assertIn("match_method=portal_reference_fast_path", logs)
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
            normalize_phone=normalize_phone,
            rentee_whatsapp_number="60115551234",
        )
        self.assertIn("One Menerung", result.response_text)
        listing_hydrations = [
            call for call in bubble_get.call_args_list
            if "/obj/listing/" in call.args[0]
        ]
        self.assertEqual(len(listing_hydrations), 1)
        self.assertEqual(
            listing_hydrations[0].args[0],
            "https://bubble.test/obj/listing/listing-1",
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
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "beds": 4, "priceRent": 18000,
            "sourceURL": "https://www.propertyguru.com.my/l/999999",
        }
        self.records.side_effect = [iter([listing]), iter([listing])]
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
        with patch("enquiry_workflow.ensure_handoff_code") as ensure_code, \
                patch("enquiry_workflow.build_whatsapp_handoff_link") as build_link:
            result = self.consume(user, "https://www.propertyguru.com.my/l/12345")
        self.assertTrue(result.handled)
        self.assertIn("marked as unavailable", result.response_text)
        self.assertNotIn("wa.me", result.response_text)
        ensure_code.assert_not_called()
        build_link.assert_not_called()
        self.assertFalse(any(
            "Handoff Code" in call.args[1]
            for call in self.patch_user.call_args_list
        ))

    def test_null_availability_proceeds_to_existing_handoff(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/12345",
            "availability": None,
        }
        self.records.side_effect = [iter([listing]), iter([])]
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with patch("enquiry_workflow._new_handoff_code", return_value="RNT-7K4M9Q2P"), \
                patch("builtins.print") as mocked_print:
            result = self.consume(user, "https://www.propertyguru.com.my/l/12345")
        self.assertIn("https://wa.me/60115551234?", result.response_text)
        self.assertIn("RNT-7K4M9Q2P", result.response_text)
        logs = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("availability=unknown", logs)
        self.assertIn("handoff_eligible=true availability_not_explicitly_false", logs)

    def test_absent_availability_proceeds_to_existing_handoff(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/12345",
        }
        self.records.side_effect = [iter([listing]), iter([])]
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with patch("enquiry_workflow._new_handoff_code", return_value="RNT-7K4M9Q2P"):
            result = self.consume(user, "https://www.propertyguru.com.my/l/12345")
        self.assertIn("https://wa.me/60115551234?", result.response_text)
        self.assertIn({"Handoff Code": "RNT-7K4M9Q2P"}, [
            call.args[1] for call in self.patch_user.call_args_list
        ])

    def test_availability_date_does_not_affect_boolean_availability_gate(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/12345",
            "availability": None,
            "availability_date": "2030-01-01T00:00:00.000Z",
        }
        self.records.side_effect = [iter([listing]), iter([])]
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with patch("enquiry_workflow._new_handoff_code", return_value="RNT-7K4M9Q2P"):
            result = self.consume(user, "https://www.propertyguru.com.my/l/12345")
        self.assertIn("https://wa.me/60115551234?", result.response_text)

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

    def test_handoff_code_format_storage_and_link(self):
        records = MagicMock(return_value=iter([]))
        patch_bubble = MagicMock()
        with patch("enquiry_workflow._new_handoff_code", return_value="RNT-7K4M9Q2P"):
            code = workflow.ensure_handoff_code(
                "enquiry-1", {}, "https://bubble.test", records, patch_bubble
            )
        self.assertEqual(code, "RNT-7K4M9Q2P")
        patch_bubble.assert_called_once_with(
            "https://bubble.test/obj/enquiry/enquiry-1",
            {"Handoff Code": "RNT-7K4M9Q2P"},
        )
        link = workflow.build_whatsapp_handoff_link(
            code, "+60 11-555 1234", normalize_phone
        )
        self.assertEqual(
            link,
            "https://wa.me/60115551234?text=Hi%2C%20I%27m%20following%20up%20on%20enquiry%20RNT-7K4M9Q2P",
        )

    def test_configured_rentee_number_builds_expected_handoff_link(self):
        link = workflow.build_whatsapp_handoff_link(
            "RNT-7K4M9Q2P", "601112032754", normalize_phone
        )
        self.assertTrue(link.startswith(
            "https://wa.me/601112032754?text="
        ))
        self.assertIn("RNT-7K4M9Q2P", link)

    def test_handoff_number_normalises_supported_formatting(self):
        link = workflow.build_whatsapp_handoff_link(
            "RNT-7K4M9Q2P", "+60 (11)-1203 2754", normalize_phone
        )
        self.assertTrue(link.startswith(
            "https://wa.me/601112032754?text="
        ))

    def test_missing_or_invalid_handoff_number_fails_safely(self):
        for configured_number in (None, "", "+() -", "+60 11 ABC 2754"):
            with self.subTest(configured_number=configured_number):
                with self.assertRaisesRegex(
                    ValueError, "dialable number is not configured"
                ):
                    workflow.build_whatsapp_handoff_link(
                        "RNT-7K4M9Q2P", configured_number, normalize_phone
                    )

    def test_existing_valid_handoff_code_is_reused_without_write(self):
        records = MagicMock()
        patch_bubble = MagicMock()
        code = workflow.ensure_handoff_code(
            "enquiry-1", {"Handoff Code": "rnt-7k4m9q2p"},
            "https://bubble.test", records, patch_bubble,
        )
        self.assertEqual(code, "RNT-7K4M9Q2P")
        records.assert_not_called()
        patch_bubble.assert_not_called()

    def test_handoff_code_collision_generates_another_code(self):
        records = MagicMock(side_effect=[
            iter([{"_id": "existing"}]), iter([]),
        ])
        patch_bubble = MagicMock()
        with patch("enquiry_workflow._new_handoff_code", side_effect=[
            "RNT-AAAAAAAA", "RNT-BBBBBBBB",
        ]):
            code = workflow.ensure_handoff_code(
                "enquiry-1", {}, "https://bubble.test", records, patch_bubble
            )
        self.assertEqual(code, "RNT-BBBBBBBB")
        self.assertEqual(patch_bubble.call_args.args[1], {
            "Handoff Code": "RNT-BBBBBBBB"
        })

    def test_available_match_creates_handoff_and_appends_link(self):
        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/12345",
            "availability": True,
        }
        self.records.side_effect = [iter([listing]), iter([])]
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        with patch("enquiry_workflow._new_handoff_code", return_value="RNT-7K4M9Q2P"):
            result = self.consume(user, "https://www.propertyguru.com.my/l/12345")
        self.assertIn("https://wa.me/60115551234?", result.response_text)
        self.assertIn("RNT-7K4M9Q2P", result.response_text)
        payloads = [call.args[1] for call in self.patch_user.call_args_list]
        self.assertIn({"Listing": "listing-1"}, payloads)
        self.assertIn({"Handoff Code": "RNT-7K4M9Q2P"}, payloads)

    def test_external_handoff_sets_empty_phone(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[
            {"_id": "enquiry-1", "Listing": "listing-1", "Enquirer Phone": ""},
            {"_id": "listing-1"},
        ])
        patch_bubble = MagicMock()
        result = workflow.handle_external_handoff_message(
            "+60 12-345 6789", "follow up rnt-7k4m9q2p please",
            "https://bubble.test", records, bubble_get, patch_bubble,
            normalize_phone,
        )
        self.assertTrue(result.handled)
        self.assertEqual(
            result.response_text,
            "Hi — I've got your enquiry for this property. I'll help you from here.",
        )
        patch_bubble.assert_called_once_with(
            "https://bubble.test/obj/enquiry/enquiry-1",
            {"Enquirer Phone": "60123456789"},
        )

    def test_external_handoff_same_phone_is_idempotent(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[
            {"Listing": "listing-1", "Enquirer Phone": "+60 12-345 6789"},
            {"_id": "listing-1"},
        ])
        patch_bubble = MagicMock()
        result = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, patch_bubble, normalize_phone,
        )
        self.assertTrue(result.handled)
        patch_bubble.assert_not_called()

    def test_external_handoff_different_phone_cannot_overwrite(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[
            {"Listing": "listing-1", "Enquirer Phone": "60111111111"},
            {"_id": "listing-1"},
        ])
        patch_bubble = MagicMock()
        result = workflow.handle_external_handoff_message(
            "60122222222", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, patch_bubble, normalize_phone,
        )
        self.assertTrue(result.handled)
        self.assertIn("fresh one", result.response_text)
        patch_bubble.assert_not_called()

    def test_invalid_or_unknown_handoff_code_falls_through(self):
        records = MagicMock(return_value=iter([]))
        invalid = workflow.handle_external_handoff_message(
            "60123456789", "reference 12345", "https://bubble.test",
            records, MagicMock(), MagicMock(), normalize_phone,
        )
        unknown = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, MagicMock(), MagicMock(), normalize_phone,
        )
        self.assertFalse(invalid.handled)
        self.assertFalse(unknown.handled)

    def test_resolved_handoff_missing_listing_does_not_bind(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        patch_bubble = MagicMock()
        result = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, MagicMock(return_value={"Enquirer Phone": ""}),
            patch_bubble, normalize_phone,
        )
        self.assertTrue(result.handled)
        self.assertIn("fresh one", result.response_text)
        patch_bubble.assert_not_called()


if __name__ == "__main__":
    unittest.main()
