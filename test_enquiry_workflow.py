from datetime import datetime, timezone, timedelta
import requests
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

    def complete_handoff_with_lead(
        self, enquiry, lead, created=False, sender_phone="+60 12-345 6789",
        finder=None, patcher=None, folio_finder=None,
    ):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[enquiry, {"_id": "listing-1"}])
        patch_bubble = patcher or MagicMock()
        finder = finder or MagicMock(return_value=(lead, created))
        result = workflow.handle_external_handoff_message(
            sender_phone, "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, patch_bubble, normalize_phone,
            find_or_create_lead=finder,
            find_or_create_folio=folio_finder,
        )
        return result, finder, patch_bubble

    def test_new_handoff_lead_ensures_folio(self):
        folio_finder = MagicMock(return_value=("folio-1", True))
        result, _finder, _patch = self.complete_handoff_with_lead(
            {"Listing": "listing-1", "Enquirer Phone": ""},
            {"_id": "lead-1"}, created=True, folio_finder=folio_finder,
        )
        self.assertIn("I've got your enquiry", result.response_text)
        folio_finder.assert_called_once_with("lead-1")

    def test_existing_handoff_lead_without_folio_creates_one(self):
        folio_finder = MagicMock(return_value=("folio-new", True))
        result, _finder, _patch = self.complete_handoff_with_lead(
            {"Listing": "listing-1", "Enquirer Phone": "60123456789"},
            {"_id": "lead-existing"}, folio_finder=folio_finder,
        )
        self.assertEqual(result.lead_id, "lead-existing")
        folio_finder.assert_called_once_with("lead-existing")

    def test_existing_handoff_lead_reuses_existing_folio(self):
        folio_finder = MagicMock(return_value=("folio-existing", False))
        with patch("builtins.print") as logged:
            result, _finder, _patch = self.complete_handoff_with_lead(
                {"Listing": "listing-1", "Enquirer Phone": "60123456789"},
                {"_id": "lead-existing"}, folio_finder=folio_finder,
            )
        self.assertIn("I've got your enquiry", result.response_text)
        logs = "\n".join(str(call) for call in logged.call_args_list)
        self.assertIn(
            "lead_id=lead-existing folio_id=folio-existing folio=existing", logs
        )

    def test_repeated_handoff_callback_reuses_folio_without_duplicate(self):
        folios = {}
        creates = []

        def find_or_create(lead_id):
            if lead_id in folios:
                return folios[lead_id], False
            folios[lead_id] = "folio-1"
            creates.append(lead_id)
            return "folio-1", True

        for _ in range(2):
            result, _finder, _patch = self.complete_handoff_with_lead(
                {"Listing": "listing-1", "Enquirer Phone": "60123456789",
                 "Lead": "lead-existing"},
                {"_id": "lead-existing"}, folio_finder=find_or_create,
            )
            self.assertIn("I've got your enquiry", result.response_text)
        self.assertEqual(creates, ["lead-existing"])

    def test_folio_creation_failure_fails_handoff_safely(self):
        folio_finder = MagicMock(side_effect=RuntimeError("Bubble unavailable"))
        result, _finder, _patch = self.complete_handoff_with_lead(
            {"Listing": "listing-1", "Enquirer Phone": "60123456789"},
            {"_id": "lead-existing"}, folio_finder=folio_finder,
        )
        self.assertTrue(result.handled)
        self.assertIn("fresh one", result.response_text)
        self.assertIsNone(result.lead_id)

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
            "sending you an agent lead",
            "new agent lead",
            "sending you a lead from another agent",
            "agent enquiry coming",
            "agent lead incoming",
            "I've got an agent enquiry",
            "another agent is enquiring",
            "this enquiry is from an agent",
            "agent coming through",
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
            "sending you a lead",
            "new lead coming",
            "tenant enquiry coming",
            "direct lead",
            "customer enquiry",
            "sending you a tenant lead",
        ):
            with self.subTest(text=text):
                self.assertEqual(workflow.detect_new_enquiry_instruction(text), "lead")

    def test_explicit_agent_always_precedes_generic_lead_wording(self):
        for text in (
            "sending you an agent lead",
            "new agent lead",
            "sending you a lead from another agent",
            "sending you an agency lead",
            "im sending you and agency lead",
            "actually it's an agent enquiry",
            "sorry, agency lead",
            "another agent",
            "cobroke enquiry",
            "co-broke enquiry",
        ):
            with self.subTest(text=text):
                self.assertEqual(workflow.detect_new_enquiry_instruction(text), "agent")

    def test_minor_setup_typo_is_handled_deterministically(self):
        self.assertEqual(
            workflow.detect_new_enquiry_instruction("sneding you a lead"), "lead"
        )

    def test_pending_setup_correction_is_not_consumed_as_enquiry(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(user, "im sending you and agency lead")
        self.assertEqual(result.response_text, "Sure — send me the agent enquiry.")
        self.create.assert_not_called()
        self.assertEqual(self.patch_user.call_args.args[1][
            workflow.AWAITING_ENQUIRY_FIELD
        ], True)
        self.assertEqual(self.patch_user.call_args.args[1][
            workflow.PENDING_AGENT_FIELD
        ], "Yes")

    def test_pending_cancel_phrases_clear_state_without_enquiry(self):
        for text in ("cancel this enquiry", "never mind", "start over"):
            with self.subTest(text=text):
                self.patch_user.reset_mock()
                self.create.reset_mock()
                user = {
                    "_id": "user-1",
                    workflow.AWAITING_ENQUIRY_FIELD: True,
                    workflow.PENDING_AGENT_FIELD: "Yes",
                    workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
                }
                result = self.consume(user, text)
                self.assertEqual(
                    result.response_text,
                    "Okay — cancelled. Send me another enquiry whenever you're ready.",
                )
                self.create.assert_not_called()
                self.assertEqual(self.patch_user.call_args.args[1], {
                    workflow.AWAITING_ENQUIRY_FIELD: False,
                    workflow.PENDING_AGENT_FIELD: "",
                    workflow.AWAITING_SINCE_FIELD: None,
                })

    def test_unrelated_pending_message_is_retained_without_enquiry(self):
        user = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = self.consume(user, "How are you today?")
        self.assertEqual(
            result.response_text,
            "I'm still waiting for the forwarded enquiry. Send it through, "
            "or say 'cancel enquiry' to stop.",
        )
        self.create.assert_not_called()
        self.patch_user.assert_not_called()

    def test_production_setup_sequence_waits_for_actual_property_enquiry(self):
        first = workflow.handle_internal_user_message(
            {"_id": "user-1"}, "sneding you a lead",
            "https://bubble.test", self.patch_user, self.now,
        )
        self.assertEqual(first.response_text, "Sure — send me the lead enquiry.")
        self.create.assert_not_called()

        pending_lead = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        second = self.consume(pending_lead, "sending you an agency lead")
        self.assertEqual(second.response_text, "Sure — send me the agent enquiry.")
        self.create.assert_not_called()

        pending_agent = {
            "_id": "user-1",
            workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        third = self.consume(pending_agent, "im sending you and agency lead")
        self.assertEqual(third.response_text, "Sure — send me the agent enquiry.")
        self.create.assert_not_called()

        listing = {
            "_id": "listing-1", "owner": "user-1", "condo": "condo-1",
            "sourceURL": "https://www.propertyguru.com.my/l/501124208",
            "priceRent": 15000, "beds": 3, "availability": True,
        }
        self.records.return_value = iter([listing])
        self.relationship_names.return_value = {"condo-1": "One Menerung"}
        actual = self.consume(
            pending_agent,
            "PropertyGuru 3 beds RM15,000 "
            "https://www.propertyguru.com.my/l/501124208",
        )
        self.assertIn("One Menerung", actual.response_text)
        self.create.assert_called_once()
        self.assertEqual(self.create.call_args.args[2]["Agent?"], "Yes")
        self.assertIn("propertyguru.com.my", self.create.call_args.args[2][
            "Original Enquiry"
        ].lower())

    def test_agent_instruction_sets_state_and_replies(self):
        result = workflow.handle_internal_user_message(
            {"_id": "user-1"}, "sending you an agent lead",
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
        with patch("builtins.print") as mocked_print:
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
            ("https://bubble.test/obj/enquiry/enquiry-1", {
                "Listing": "listing-1", "TransactionType": ["Rent/Let"],
            }),
            [call.args for call in self.patch_user.call_args_list],
        )
        listing_payload = next(
            call.args[1] for call in self.patch_user.call_args_list
            if "Listing" in call.args[1]
        )
        self.assertEqual(listing_payload["Listing"], listing["_id"])
        self.assertNotEqual(listing_payload["Listing"], listing["condo"])
        logs = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn(
            "enquiry_id=enquiry-1 matched_listing_id=listing-1 condo_id=condo-1",
            logs,
        )
        self.assertIn(
            "enquiry_id=enquiry-1 listing_relationship_written=listing-1",
            logs,
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
            "priceRent": 15000,
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
            ("https://bubble.test/obj/enquiry/enquiry-1", {
                "Listing": "listing-1", "TransactionType": ["Rent/Let"],
            }),
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
            "priceRent": 15000,
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
            "priceRent": 15000,
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
            "priceRent": 15000,
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
            "priceRent": 15000,
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
        self.assertIn({
            "Listing": "listing-1", "TransactionType": ["Rent/Let"],
        }, payloads)
        self.assertIn({"Handoff Code": "RNT-7K4M9Q2P"}, payloads)

    def test_external_handoff_sets_empty_phone(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[
            {"_id": "enquiry-1", "Listing": "listing-1", "Enquirer Phone": "",
             "TransactionType": ["Rent/Let"]},
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
        self.assertEqual(result.followup_text, workflow.TENANT_PROFILE_REQUEST)
        self.assertEqual(result.enquiry_id, "enquiry-1")
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

    def test_invalid_code_falls_through_but_unknown_valid_code_is_owned(self):
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
        self.assertIsNone(invalid.followup_text)
        self.assertTrue(unknown.handled)
        self.assertIn("fresh one", unknown.response_text)
        self.assertIsNone(unknown.followup_text)

    def test_originating_agent_cannot_bind_own_handoff(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(return_value={
            "_id": "enquiry-1", "Agent": "user-origin", "Listing": "listing-1",
            "Enquirer Phone": "",
        })
        patch_bubble = MagicMock()
        result = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, patch_bubble, normalize_phone,
            sender_user_id="user-origin",
        )
        self.assertTrue(result.handled)
        self.assertIn("for the enquirer", result.response_text)
        self.assertIsNone(result.followup_text)
        patch_bubble.assert_not_called()
        self.assertEqual(bubble_get.call_count, 1)

    def test_tenant_profile_request_matches_fixed_template(self):
        self.assertEqual(workflow.TENANT_PROFILE_REQUEST, """Hi there, thanks for reaching out. Would you be able to share the below info for the owner and let me know when you’d like to view?

TENANT PROFILE
🚩Nationality:
👨‍👩‍👦‍👦Pax (adults/kids/helpers):
🛏️How many rooms do you need?
🪑Furnished or Unfurnished?
💻Occupation:
🐶Pet?
🗓️Start date:
💰Budget:""")

    def test_different_bubble_user_can_complete_handoff(self):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[
            {"Agent": "user-origin", "Listing": "listing-1", "Enquirer Phone": ""},
            {"_id": "listing-1"},
        ])
        patch_bubble = MagicMock()
        result = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, patch_bubble, normalize_phone,
            sender_user_id="user-other",
        )
        self.assertTrue(result.handled)
        patch_bubble.assert_called_once_with(
            "https://bubble.test/obj/enquiry/enquiry-1",
            {"Enquirer Phone": "60123456789"},
        )

    def test_handoff_creates_and_links_agent_lead_with_normalized_phone(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "", "Agent?": "Yes"
        }
        result, finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "Yes"}, created=True
        )
        self.assertIn("I've got your enquiry", result.response_text)
        finder.assert_called_once_with(
            "60123456789", customer_name=None, agent_classification="Yes",
            owner_user_id=None,
        )
        self.assertIn(
            {"Lead": "lead-1"},
            [call.args[1] for call in patch_bubble.call_args_list],
        )
        self.assertIn(
            {"ActiveForwardedEnquiry": "enquiry-1"},
            [call.args[1] for call in patch_bubble.call_args_list],
        )

    def test_later_handoff_replaces_active_forwarded_enquiry(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Lead": "lead-1",
        }
        lead = {
            "_id": "lead-1", "Agent?": "No",
            "ActiveForwardedEnquiry": "enquiry-old",
        }
        _result, _finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, lead
        )
        self.assertIn(
            {"ActiveForwardedEnquiry": "enquiry-1"},
            [call.args[1] for call in patch_bubble.call_args_list],
        )
        self.assertEqual(lead["ActiveForwardedEnquiry"], "enquiry-1")

    def test_handoff_creates_normal_lead_with_text_no(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No",
        }
        _result, finder, _patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "No"}, created=True
        )
        finder.assert_called_once_with(
            "60123456789", customer_name=None, agent_classification="No",
            owner_user_id=None,
        )

    def test_explicit_enquirer_name_patterns_are_conservative(self):
        cases = {
            "Hi Gwen, I'm Sarah Lim and I'm interested in this unit": "Sarah Lim",
            "Hi Gwen, this is John Tan. Is this available?": "John Tan",
            "Name: Melissa Wong\nI am interested in this unit": "Melissa Wong",
            "I spoke to Sarah Lim about this unit": None,
            "Can you ask Mr Tan whether the owner will accept?": None,
            "Gwen Delhumeau sent me this property": None,
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(workflow.extract_enquirer_name(message), expected)

    def test_original_name_precedes_profile_and_agent_suffix_is_idempotent(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "Yes",
            "Original Enquiry": "Hi Gwen, I'm Sarah Lim and I'm interested",
        }
        finder = MagicMock(return_value=(
            {"_id": "lead-1", "Agent?": "Yes", "name": "Sarah Lim (Agent)"},
            True,
        ))
        _result, finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {}, finder=finder
        )
        finder.assert_called_once_with(
            "60123456789", customer_name="Sarah Lim (Agent)",
            agent_classification="Yes", owner_user_id=None,
        )
        name_updates = [
            call.args[1] for call in patch_bubble.call_args_list
            if call.args[0].endswith("/obj/lead/lead-1") and "name" in call.args[1]
        ]
        self.assertEqual(name_updates, [])

    def test_whatsapp_profile_name_is_fallback_for_blank_original_name(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Original Enquiry": "Is this still available?",
        }
        finder = MagicMock(return_value=(
            {"_id": "lead-1", "Agent?": "No", "name": "Aisha Rahman"}, True
        ))
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        result = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, MagicMock(side_effect=[enquiry, {"_id": "listing-1"}]),
            MagicMock(), normalize_phone, find_or_create_lead=finder,
            whatsapp_profile_name="Aisha Rahman",
        )
        self.assertTrue(result.handled)
        finder.assert_called_once_with(
            "60123456789", customer_name="Aisha Rahman",
            agent_classification="No", owner_user_id=None,
        )

    def test_new_lead_gets_originating_enquiry_agent(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Agent": "user-gwen",
        }
        finder = MagicMock(return_value=(
            {"_id": "lead-1", "Agent?": "No", "owner": "user-gwen"}, True
        ))
        result, finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {}, finder=finder
        )
        self.assertIn("I've got your enquiry", result.response_text)
        finder.assert_called_once_with(
            "60123456789", customer_name=None, agent_classification="No",
            owner_user_id="user-gwen",
        )
        self.assertFalse(any(
            call.args[0].endswith("/obj/lead/lead-1") and "owner" in call.args[1]
            for call in patch_bubble.call_args_list
        ))

    def test_existing_lead_with_blank_agent_gets_originating_agent(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Agent": "user-gwen",
        }
        _result, _finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "No", "owner": ""}
        )
        self.assertIn(
            {"owner": "user-gwen"},
            [call.args[1] for call in patch_bubble.call_args_list],
        )

    def test_existing_lead_with_same_agent_is_unchanged(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Agent": "user-gwen",
        }
        with patch("builtins.print") as mocked_print:
            _result, _finder, patch_bubble = self.complete_handoff_with_lead(
                enquiry,
                {"_id": "lead-1", "Agent?": "No", "owner": "user-gwen"},
            )
        self.assertNotIn(
            {"owner": "user-gwen"},
            [call.args[1] for call in patch_bubble.call_args_list],
        )
        self.assertIn(
            "owner_user_id=user-gwen action=kept",
            " ".join(str(call) for call in mocked_print.call_args_list),
        )

    def test_conflicting_lead_agent_is_preserved_without_blocking_handoff(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Agent": "user-gwen",
        }
        with patch("builtins.print") as mocked_print:
            result, _finder, patch_bubble = self.complete_handoff_with_lead(
                enquiry,
                {"_id": "lead-1", "Agent?": "No", "owner": "user-other"},
            )
        self.assertIn("I've got your enquiry", result.response_text)
        self.assertNotIn(
            {"owner": "user-gwen"},
            [call.args[1] for call in patch_bubble.call_args_list],
        )
        self.assertIn(
            "existing_owner_user_id=user-other enquiry_agent_user_id=user-gwen "
            "action=owner_conflict_preserved",
            " ".join(str(call) for call in mocked_print.call_args_list),
        )

    def test_existing_name_is_preserved_but_agent_suffix_is_added(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "Yes", "Original Enquiry": "I'm Different Person",
        }
        _result, _finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "No", "name": "Sarah Lim"}
        )
        lead_updates = [
            call.args[1] for call in patch_bubble.call_args_list
            if call.args[0].endswith("/obj/lead/lead-1")
        ]
        self.assertIn({"Agent?": "Yes"}, lead_updates)
        self.assertIn({"name": "Sarah Lim (Agent)"}, lead_updates)
        self.assertNotIn({"name": "Different Person (Agent)"}, lead_updates)

    def test_blank_agent_name_does_not_become_suffix_only(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "Yes", "Original Enquiry": "Interested in this unit",
        }
        _result, _finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "Yes", "name": ""}
        )
        self.assertFalse(any(
            call.args[0].endswith("/obj/lead/lead-1") and "name" in call.args[1]
            for call in patch_bubble.call_args_list
        ))

    def test_existing_lead_agent_classification_is_sticky(self):
        cases = (
            ("", "No", "No", "set"),
            ("No", "Yes", "Yes", "upgraded"),
            ("Yes", "No", "Yes", "kept"),
            ("Yes", "Yes", "Yes", "kept"),
        )
        for existing, enquiry_value, expected, expected_action in cases:
            with self.subTest(existing=existing, enquiry=enquiry_value):
                enquiry = {
                    "Listing": "listing-1", "Enquirer Phone": "60123456789",
                    "Agent?": enquiry_value,
                }
                with patch("builtins.print") as mocked_print:
                    _result, _finder, patch_bubble = self.complete_handoff_with_lead(
                        enquiry, {"_id": "lead-1", "Agent?": existing}
                    )
                lead_updates = [
                    call.args[1] for call in patch_bubble.call_args_list
                    if call.args[0].endswith("/obj/lead/lead-1")
                    and "Agent?" in call.args[1]
                ]
                expected_updates = (
                    [] if expected_action == "kept" else [{"Agent?": expected}]
                )
                self.assertEqual(lead_updates, expected_updates)
                logs = " ".join(str(call) for call in mocked_print.call_args_list)
                self.assertIn(
                    f"agent_classification={expected} action={expected_action}", logs
                )

    def test_same_linked_lead_is_idempotent(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Lead": "lead-1",
        }
        result, _finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "No"}
        )
        self.assertIn("I've got your enquiry", result.response_text)
        self.assertFalse(any(
            "Lead" in call.args[1] for call in patch_bubble.call_args_list
        ))

    def test_conflicting_enquiry_lead_is_not_overwritten(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No", "Lead": "lead-other",
        }
        result, _finder, patch_bubble = self.complete_handoff_with_lead(
            enquiry, {"_id": "lead-1", "Agent?": "No"}
        )
        self.assertIn("fresh one", result.response_text)
        self.assertFalse(any(
            "Lead" in call.args[1] for call in patch_bubble.call_args_list
        ))

    def test_lead_creation_failure_does_not_complete_handoff(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "Yes",
        }
        finder = MagicMock(side_effect=RuntimeError("Bubble unavailable"))
        with patch("builtins.print") as mocked_print:
            result, _finder, patch_bubble = self.complete_handoff_with_lead(
                enquiry, {}, finder=finder
            )
        self.assertIn("fresh one", result.response_text)
        self.assertFalse(any(
            "Lead" in call.args[1] for call in patch_bubble.call_args_list
        ))
        self.assertNotIn(
            "handoff completed",
            " ".join(str(call) for call in mocked_print.call_args_list),
        )

    def test_lead_link_failure_does_not_report_completed_handoff(self):
        enquiry = {
            "Listing": "listing-1", "Enquirer Phone": "60123456789",
            "Agent?": "No",
        }
        patcher = MagicMock(side_effect=RuntimeError("Bubble patch failed"))
        with patch("builtins.print") as mocked_print:
            result, _finder, _patcher = self.complete_handoff_with_lead(
                enquiry, {"_id": "lead-1", "Agent?": "No"}, patcher=patcher
            )
        self.assertIn("fresh one", result.response_text)
        logs = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("Lead linking failed", logs)
        self.assertNotIn("handoff completed", logs)

    def test_broad_portal_fallback_matches_220_records_without_hydrating_all(self):
        listings = [{
            "_id": f"listing-{index}", "owner": "user-1",
            "sourceURL": f"https://www.propertyguru.com.my/l/{900000000 + index}",
            "condo": "condo-1", "beds": 3, "priceRent": 15000,
            "availability": True,
        } for index in range(220)]
        listings[137]["sourceURL"] = "https://www.iproperty.com.my/l/501124208"
        records = MagicMock(side_effect=[iter([]), iter(listings), iter([])])
        bubble_get = MagicMock(return_value={})
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "Yes",
            workflow.AWAITING_SINCE_FIELD: self.now.isoformat(),
        }
        result = workflow.handle_internal_user_message(
            user, "https://www.propertyguru.com.my/l/501124208",
            "https://bubble.test", self.patch_user, self.now,
            bubble_create=self.create, bubble_records=records,
            relationship_names=self.relationship_names, bubble_get=bubble_get,
            normalize_phone=normalize_phone,
            rentee_whatsapp_number="60115551234",
        )
        self.assertIn("https://wa.me/", result.response_text)
        listing_gets = [
            call for call in bubble_get.call_args_list
            if "/obj/listing/" in call.args[0]
        ]
        self.assertEqual(listing_gets, [])

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


class TransactionIntentTests(unittest.TestCase):
    def test_enquiry_creation_payload_uses_native_text_lists(self):
        rent = workflow.build_enquiry_creation_payload(
            "user-1", "No", "rental enquiry", "Rent/Let"
        )
        buy = workflow.build_enquiry_creation_payload(
            "user-1", "No", "buyer enquiry", "Buy/Sell"
        )
        self.assertEqual(rent["TransactionType"], ["Rent/Let"])
        self.assertEqual(buy["TransactionType"], ["Buy/Sell"])
        self.assertIsInstance(rent["TransactionType"], list)
        self.assertIsInstance(buy["TransactionType"], list)

    def test_unresolved_transaction_is_omitted_from_creation_payload(self):
        for value in (None, "", "Rent", "Buy"):
            with self.subTest(value=value):
                payload = workflow.build_enquiry_creation_payload(
                    "user-1", "No", "enquiry", value
                )
                self.assertNotIn("TransactionType", payload)
                self.assertEqual(set(payload), {
                    "Agent", "Agent?", "Original Enquiry",
                })

    def test_enquiry_http_400_logs_safe_response_and_keeps_pending(self):
        response = requests.Response()
        response.status_code = 400
        response._content = (
            b'Invalid TransactionType in buyer enquiry forwarded secret text'
        )
        error = requests.HTTPError("400 Client Error", response=response)
        create = MagicMock(side_effect=error)
        patch_bubble = MagicMock()
        user = {
            "_id": "user-1", workflow.AWAITING_ENQUIRY_FIELD: True,
            workflow.PENDING_AGENT_FIELD: "No",
            workflow.AWAITING_SINCE_FIELD: datetime.now(timezone.utc).isoformat(),
        }
        with patch("builtins.print") as mocked_print:
            with self.assertRaises(requests.HTTPError):
                workflow.handle_internal_user_message(
                    user, "buyer enquiry forwarded secret text",
                    "https://bubble.test", patch_bubble,
                    bubble_create=create,
                    bubble_records=MagicMock(),
                    relationship_names=MagicMock(),
                )
        log = " ".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("status=400", log)
        self.assertIn("transaction_type_type=list", log)
        self.assertIn("Invalid TransactionType", log)
        self.assertNotIn("buyer enquiry forwarded secret text", log)
        self.assertIn("pending state retained", log)
        patch_bubble.assert_not_called()

    def test_explicit_rental_and_tenant_wording(self):
        for text in ("new rental enquiry", "tenant enquiry", "client wants to rent"):
            with self.subTest(text=text):
                self.assertEqual(
                    workflow.explicit_transaction_type(text), "Rent/Let"
                )

    def test_explicit_buyer_and_purchase_wording(self):
        for text in ("buyer enquiry", "client wants to purchase", "for sale"):
            with self.subTest(text=text):
                self.assertEqual(
                    workflow.explicit_transaction_type(text), "Buy/Sell"
                )

    def test_listing_transaction_inference(self):
        cases = (
            ({"priceRent": 12000}, "Rent/Let"),
            ({"priceSale": 2500000}, "Buy/Sell"),
            ({"priceRent": 12000, "priceSale": 2500000}, None),
            ({}, None),
            ({"TransactionType": ["Rent/Let"]}, "Rent/Let"),
            ({"TransactionType": ["Buy/Sell"]}, "Buy/Sell"),
        )
        for listing, expected in cases:
            with self.subTest(listing=listing):
                self.assertEqual(
                    workflow.listing_transaction_type(listing), expected
                )

    def test_explicit_wording_wins_over_ambiguous_listing(self):
        explicit = workflow.explicit_transaction_type("this one is for rent")
        inferred = workflow.listing_transaction_type({
            "priceRent": 12000, "priceSale": 2500000,
        })
        self.assertEqual(explicit or inferred, "Rent/Let")

    def test_gwen_confirmation_persists_and_resumes_handoff(self):
        for answer, expected in (("rent", "Rent/Let"), ("buy", "Buy/Sell")):
            with self.subTest(answer=answer):
                enquiry = {
                    "_id": "enquiry-1", "Agent": "user-1",
                    "Listing": "listing-1", "TransactionType": [],
                }
                records = MagicMock(side_effect=[iter([enquiry]), iter([])])
                patch_bubble = MagicMock()
                bubble_get = MagicMock(side_effect=[
                    {"_id": "listing-1", "availability": True},
                    dict(enquiry),
                ])
                with patch(
                    "enquiry_workflow._new_handoff_code",
                    return_value="RNT-7K4M9Q2P",
                ):
                    result = workflow.handle_internal_user_message(
                        {"_id": "user-1"}, answer, "https://bubble.test",
                        patch_bubble, bubble_records=records,
                        bubble_get=bubble_get, normalize_phone=normalize_phone,
                        rentee_whatsapp_number="60115551234",
                    )
                self.assertIn("https://wa.me/", result.response_text)
                self.assertIn(
                    unittest.mock.call(
                        "https://bubble.test/obj/enquiry/enquiry-1",
                        {"TransactionType": [expected]},
                    ),
                    patch_bubble.call_args_list,
                )

    def _external_template(self, transaction_type):
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[
            {"_id": "enquiry-1", "Listing": "listing-1",
             "Enquirer Phone": "60123456789",
             "TransactionType": transaction_type},
            {"_id": "listing-1"},
        ])
        return workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, MagicMock(), normalize_phone,
        ).followup_text

    def test_transaction_specific_profile_templates(self):
        self.assertEqual(
            self._external_template(["Rent/Let"]),
            workflow.TENANT_PROFILE_REQUEST,
        )
        self.assertEqual(
            self._external_template(["Buy/Sell"]),
            workflow.BUYER_PROFILE_REQUEST,
        )
        self.assertIsNone(self._external_template([]))


class LeadTransactionPropagationTests(unittest.TestCase):
    def test_cumulative_transaction_merge_rules(self):
        cases = (
            ({}, ["Rent/Let"], ["Rent/Let"]),
            ({}, ["Buy/Sell"], ["Buy/Sell"]),
            ({"TransactionType": ["Rent/Let"]}, ["Rent/Let"], None),
            ({"TransactionType": ["Rent/Let"]}, ["Buy/Sell"],
             ["Rent/Let", "Buy/Sell"]),
            ({"TransactionType": ["Buy/Sell"]}, ["Rent/Let"],
             ["Buy/Sell", "Rent/Let"]),
            ({"TransactionType": ["Rent/Let", "Buy/Sell"]},
             ["Buy/Sell"], None),
            ({"TransactionType": ["Rent/Let"]}, [], None),
            ({"TransactionType": ["Rent/Let"]}, ["Unknown"], None),
        )
        for lead, enquiry_values, expected in cases:
            with self.subTest(lead=lead, enquiry=enquiry_values):
                self.assertEqual(
                    workflow.merged_lead_transaction_types(
                        lead, {"TransactionType": enquiry_values}
                    ),
                    expected,
                )

    def test_handoff_patches_new_lead_with_enquiry_transaction(self):
        enquiry = {
            "_id": "enquiry-1", "Listing": "listing-1",
            "Enquirer Phone": "60123456789",
            "TransactionType": ["Rent/Let"],
        }
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[enquiry, {"_id": "listing-1"}])
        bubble_patch = MagicMock()
        finder = MagicMock(return_value=({"_id": "lead-1"}, True))

        result = workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, bubble_patch, normalize_phone,
            find_or_create_lead=finder,
        )

        self.assertTrue(result.handled)
        self.assertIn(
            unittest.mock.call(
                "https://bubble.test/obj/lead/lead-1",
                {"TransactionType": ["Rent/Let"]},
            ),
            bubble_patch.call_args_list,
        )

    def test_handoff_patches_existing_lead_cumulatively(self):
        enquiry = {
            "_id": "enquiry-1", "Listing": "listing-1",
            "Enquirer Phone": "60123456789",
            "TransactionType": ["Buy/Sell"],
        }
        records = MagicMock(return_value=iter([{"_id": "enquiry-1"}]))
        bubble_get = MagicMock(side_effect=[enquiry, {"_id": "listing-1"}])
        bubble_patch = MagicMock()
        finder = MagicMock(return_value=({
            "_id": "lead-1", "TransactionType": ["Rent/Let"],
            "Agent?": "No",
        }, False))

        workflow.handle_external_handoff_message(
            "60123456789", "RNT-7K4M9Q2P", "https://bubble.test",
            records, bubble_get, bubble_patch, normalize_phone,
            find_or_create_lead=finder,
        )

        self.assertIn(
            unittest.mock.call(
                "https://bubble.test/obj/lead/lead-1",
                {"TransactionType": ["Rent/Let", "Buy/Sell"]},
            ),
            bubble_patch.call_args_list,
        )


if __name__ == "__main__":
    unittest.main()
