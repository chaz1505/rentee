import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module
from search_flow import (
    area_recommendation_needed,
    apply_search_update,
    dump_search_state,
    empty_search_state,
    load_search_state,
    listing_search_scope,
    next_search_question,
    search_brief_complete,
    set_recommended_condos,
)


def complete_state():
    return apply_search_update(empty_search_state(), {
        "area_status": "known",
        "areas": ["Bangsar"],
        "property_types": ["Condo"],
        "bedroom_requirement": "exactly 4 bedrooms",
        "budget_requirement": "maximum RM12,000",
        "other_requirements": ["furnished", "balcony"],
        "other_requirements_answered": True,
        "priorities": ["location", "bedrooms", "balcony"],
        "priorities_answered": True,
    })


class SearchFlowStateTests(unittest.TestCase):
    @patch("app.update_preferences")
    @patch("app.advance_property_search")
    def test_chat_guided_search_skips_preference_rewrite_call(
        self, mocked_advance, mocked_update_preferences
    ):
        tool_args = {
            "area_status": "unchanged", "areas": [],
            "regular_destinations": [], "property_types": [],
            "bedroom_requirement": "4 bedrooms", "budget_requirement": "",
            "other_requirements": [], "other_requirements_answered": False,
            "priorities": [], "priorities_answered": False,
            "selected_condos": [], "use_full_shortlist": False,
            "search_listings": False,
        }
        tool_call = SimpleNamespace(
            type="function_call", name="advance_property_search",
            call_id="search-call", arguments=json.dumps(tool_args),
        )
        initial = SimpleNamespace(id="initial", output=[tool_call], usage=None)
        final = SimpleNamespace(id="final", output=[], usage=None)
        mocked_advance.return_value = {
            "action": "ask", "text": "What's your monthly rental budget?",
            "state": {}, "lead_id": "lead-1",
        }

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial),
            FakeStream(final, [SimpleNamespace(
                type="response.output_text.delta", delta="What's your budget?"
            )]),
        ]

        with patch.object(
            app_module, "client", SimpleNamespace(responses=responses)
        ):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={"message": "I need 4 bedrooms", "folio_id": "folio-1"},
            )
            body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        self.assertIn("What's your budget?", body)
        mocked_advance.assert_called_once_with("folio-1", "live", tool_args)
        mocked_update_preferences.assert_not_called()
        responses.create.assert_not_called()

    def test_new_lead_starts_with_area_question(self):
        state = empty_search_state()
        self.assertFalse(search_brief_complete(state))
        self.assertEqual(
            next_search_question(state),
            "Do you already know which area you'd like to live in?",
        )

    def test_multiple_first_message_requirements_skip_answered_questions(self):
        state = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
            "property_types": ["condo"],
            "bedroom_requirement": "4 bedrooms",
            "budget_requirement": "maximum RM12,000",
            "other_requirements": ["furnished"],
            "other_requirements_answered": False,
        })
        self.assertTrue(search_brief_complete(state))
        self.assertIsNone(next_search_question(state))

    def test_questions_are_one_at_a_time_and_in_order(self):
        state = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"]
        })
        question = next_search_question(state)
        self.assertEqual(
            question,
            "Are you looking for a condo, landed property, or would you consider both?",
        )
        self.assertEqual(question.count("?"), 1)

    def test_unknown_area_asks_for_regular_destinations(self):
        state = apply_search_update(empty_search_state(), {"area_status": "unknown"})
        self.assertEqual(state["area_status"], "unknown")
        self.assertIn("need to go regularly", next_search_question(state))

    def test_unknown_area_with_destinations_requires_area_recommendation(self):
        state = apply_search_update(empty_search_state(), {
            "area_status": "unknown",
            "regular_destinations": ["KLCC", "Alice Smith School"],
        })
        self.assertTrue(area_recommendation_needed(state))
        self.assertIsNone(next_search_question(state))

    def test_unchanged_update_preserves_unknown_area_and_destinations(self):
        state = apply_search_update(empty_search_state(), {
            "area_status": "unknown",
            "regular_destinations": ["KLCC", "Alice Smith School"],
        })
        state = apply_search_update(state, {
            "area_status": "unchanged", "property_types": ["Condo"]
        })
        self.assertEqual(state["area_status"], "unknown")
        self.assertEqual(state["regular_destinations"], [
            "KLCC", "Alice Smith School"
        ])
        self.assertEqual(state["property_types"], ["Condo"])
        self.assertTrue(area_recommendation_needed(state))

    def test_unknown_area_status_survives_persistence(self):
        state = apply_search_update(empty_search_state(), {
            "area_status": "unknown", "regular_destinations": ["KLCC"]
        })
        restored = load_search_state(dump_search_state(state))
        self.assertEqual(restored["area_status"], "unknown")
        self.assertEqual(restored["regular_destinations"], ["KLCC"])

    def test_legacy_area_unknown_state_is_migrated(self):
        restored = load_search_state({
            "area_unknown": True, "areas": [],
            "regular_destinations": ["KLCC"],
        })
        self.assertEqual(restored["area_status"], "unknown")
        self.assertTrue(area_recommendation_needed(restored))

    @patch("app.save_search_state")
    @patch("app.recommend_areas_for_search")
    @patch("app.bubble")
    def test_advance_search_recommends_areas_from_stored_destinations(
        self, mocked_bubble, mocked_recommend, mocked_save
    ):
        stored = apply_search_update(empty_search_state(), {
            "area_status": "unchanged",
            "regular_destinations": ["KLCC", "Alice Smith School"],
            "property_types": ["Condo"],
        })
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]
        mocked_recommend.return_value = [
            {"area_name": "Bangsar", "reason": "Practical for both destinations."},
            {"area_name": "Damansara Heights", "reason": "A quieter alternative."},
        ]

        result = app_module.advance_property_search("folio-1", "live", {
            "area_status": "unknown", "areas": [],
            "regular_destinations": [], "property_types": [],
        })

        self.assertEqual(result["action"], "recommend_areas")
        self.assertEqual(result["state"]["area_status"], "unknown")
        self.assertEqual(
            result["state"]["regular_destinations"],
            ["KLCC", "Alice Smith School"],
        )
        self.assertIn("Bangsar", result["text"])
        self.assertNotIn("Do you already know", result["text"])
        self.assertNotIn("Where do you", result["text"])
        mocked_save.assert_called_once()

    @patch("app.save_search_state")
    @patch("app.recommend_areas_for_search")
    @patch("app.bubble")
    def test_later_requirement_reuses_saved_area_recommendations(
        self, mocked_bubble, mocked_recommend, _mocked_save
    ):
        stored = apply_search_update(empty_search_state(), {
            "area_status": "unknown", "regular_destinations": ["KLCC"],
        })
        stored["area_recommendations"] = [
            {"area_name": "Bangsar", "reason": "A balanced option."}
        ]
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": dump_search_state(stored)}
        ]

        result = app_module.advance_property_search("folio-1", "live", {
            "area_status": "unchanged", "property_types": ["Condo"]
        })

        self.assertEqual(result["action"], "recommend_areas")
        self.assertEqual(result["state"]["area_status"], "unknown")
        self.assertEqual(result["state"]["property_types"], ["Condo"])
        mocked_recommend.assert_not_called()

    def test_selecting_recommended_area_resolves_area_and_keeps_requirements(self):
        state = apply_search_update(empty_search_state(), {
            "area_status": "unknown", "regular_destinations": ["KLCC"],
            "property_types": ["Condo"], "bedroom_requirement": "4 bedrooms",
        })
        state["area_recommendations"] = [
            {"area_name": "Bangsar", "reason": "A balanced option."}
        ]
        state = apply_search_update(state, {
            "area_status": "known", "areas": ["Bangsar"]
        })
        self.assertEqual(state["area_status"], "known")
        self.assertEqual(state["areas"], ["Bangsar"])
        self.assertEqual(state["property_types"], ["Condo"])
        self.assertEqual(state["bedroom_requirement"], "4 bedrooms")
        self.assertEqual(state["area_recommendations"], [])
        self.assertEqual(next_search_question(state), "What's your monthly rental budget?")

    def test_no_other_requirements_and_no_priorities_are_valid_answers(self):
        state = complete_state()
        state = apply_search_update(state, {
            "other_requirements": [], "other_requirements_answered": True,
            "priorities": [], "priorities_answered": True,
        })
        self.assertTrue(search_brief_complete(state))
        self.assertIsNone(next_search_question(state))

    def test_material_change_invalidates_condo_shortlist(self):
        state = set_recommended_condos(complete_state(), ["One Menerung", "Ken Bangsar"])
        changed = apply_search_update(state, {"budget_requirement": "maximum RM9,000"})
        self.assertEqual(changed["recommended_condos"], [])
        self.assertEqual(changed["stage"], "SEARCH_BRIEF_COMPLETE")

    def test_preference_added_later_preserves_existing_requirements(self):
        state = apply_search_update(empty_search_state(), {
            "other_requirements": ["furnished", "balcony"],
            "other_requirements_answered": True,
        })
        updated = apply_search_update(state, {
            "other_requirements": ["dog allowed"],
            "other_requirements_answered": True,
        })
        self.assertEqual(
            updated["other_requirements"],
            ["furnished", "balcony", "dog allowed"],
        )

    def test_active_instructions_are_small_and_skill_based(self):
        instructions = app_module.build_response_args("Help me find a home")["instructions"]
        self.assertLess(len(instructions), 5000)
        self.assertIn("# Property search", instructions)
        self.assertIn("# Condo advice", instructions)
        self.assertNotIn("NEW_PROPERTY_SEARCH", instructions)
        self.assertNotIn("function_call_output", instructions)

    def test_selected_condos_are_limited_to_recommended_shortlist(self):
        state = set_recommended_condos(
            complete_state(), ["One Menerung", "Ken Bangsar", "The Loft"]
        )
        scope = listing_search_scope(
            state, ["One Menerung", "Unrelated Condo"]
        )
        self.assertEqual(scope, ["One Menerung"])

    def test_just_show_me_uses_full_shortlist_not_all_inventory(self):
        shortlist = ["One Menerung", "Ken Bangsar", "The Loft"]
        state = set_recommended_condos(complete_state(), shortlist)
        self.assertEqual(
            listing_search_scope(state, use_full_shortlist=True), shortlist
        )

    def test_listing_search_cannot_start_before_complete_brief_or_shortlist(self):
        self.assertEqual(
            listing_search_scope(empty_search_state(), use_full_shortlist=True), []
        )
        self.assertEqual(
            listing_search_scope(complete_state(), use_full_shortlist=True), []
        )

    def test_tool_schema_preserves_direct_information_tools(self):
        args = app_module.build_response_args("What is Ken Bangsar like?")
        tools = {tool.get("name"): tool for tool in args["tools"]}
        self.assertIn("advance_property_search", tools)
        self.assertIn("get_condo_info", tools)
        self.assertIn("get_property_details", tools)
        self.assertIn("match_lead", tools)
        self.assertIn("next useful customer-facing result", tools["advance_property_search"]["description"])

    def test_property_search_skill_covers_area_intent_without_a_script(self):
        instructions = app_module.build_response_args(
            "I am looking to rent. Which area would you recommend?"
        )["instructions"]
        self.assertIn("# Property search", instructions)
        self.assertIn("does not know an area", instructions)
        self.assertIn("regular destinations", instructions)
        self.assertNotIn("MUST", instructions)

    def test_personalised_search_routing_prompt_captures_named_area(self):
        args = app_module.build_response_args("I want to rent in Bangsar")
        tool = next(
            item for item in args["tools"]
            if item.get("name") == "advance_property_search"
        )
        self.assertIn("Save requirements", tool["description"])
        self.assertIn("every requirement", tool["description"])

    def test_general_area_question_is_excluded_from_personalised_search(self):
        instructions = app_module.build_response_args(
            "What is Bangsar like?"
        )["instructions"]
        self.assertIn("neighbourhood", instructions)
        self.assertIn("Answer the actual question first", instructions)

    def test_named_condo_question_still_routes_to_condo_knowledge(self):
        args = app_module.build_response_args("What is Ken Bangsar like?")
        instructions = args["instructions"]
        condo_tool = next(
            item for item in args["tools"] if item.get("name") == "get_condo_info"
        )
        self.assertIn("Use `get_condo_info` for named developments", instructions)
        self.assertIn("condo facts", condo_tool["description"])

    @patch("app.requests.patch")
    @patch("app.bubble")
    def test_incomplete_brief_is_saved_without_listing_search(
        self, mocked_bubble, mocked_patch
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": ""}
        ]
        mocked_patch.return_value.raise_for_status.return_value = None
        result = app_module.advance_property_search("folio-1", "live", {
            "area_status": "known", "areas": ["Bangsar"],
            "property_types": [], "regular_destinations": [],
            "bedroom_requirement": "4 bedrooms", "budget_requirement": "",
            "other_requirements": [], "other_requirements_answered": False,
            "priorities": [], "priorities_answered": False,
            "selected_condos": [], "use_full_shortlist": False,
            "search_listings": True,
        })
        self.assertEqual(result["action"], "ask")
        self.assertIn("condo, landed", result["text"])
        payload = mocked_patch.call_args.kwargs["json"]
        self.assertIn("searchBriefJSON", payload)
        self.assertEqual(payload["AIsearchtext"], "Areas: Bangsar\nBedrooms: 4 bedrooms")
        self.assertEqual(payload["AIsearchsummary"], "Area: Bangsar\nBedrooms: 4 bedrooms")

    @patch("app.requests.patch")
    @patch("app.bubble")
    def test_multiple_structured_requirements_are_persisted_without_llm_rewrite(
        self, mocked_bubble, mocked_patch
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": ""}
        ]
        mocked_patch.return_value.raise_for_status.return_value = None

        with patch("app.update_preferences") as mocked_update_preferences, patch(
            "app.recommend_condos_for_search",
            return_value=([{"condo_name": "One Menerung", "reason": "fit"}], "A fit"),
        ) as mocked_recommend:
            result = app_module.advance_property_search("folio-1", "live", {
                "area_status": "known", "areas": ["Bangsar"],
                "regular_destinations": [], "property_types": ["Condo"],
                "bedroom_requirement": "4 bedrooms",
                "budget_requirement": "up to RM12k",
                "other_requirements": [], "other_requirements_answered": False,
                "priorities": [], "priorities_answered": False,
                "selected_condos": [], "use_full_shortlist": False,
                "search_listings": False,
            })

        self.assertEqual(result["state"]["bedroom_requirement"], "4 bedrooms")
        mocked_update_preferences.assert_not_called()
        mocked_recommend.assert_called_once()
        payload = mocked_patch.call_args.kwargs["json"]
        self.assertEqual(
            payload["AIsearchtext"],
            "Areas: Bangsar\nProperty types: Condo\nBedrooms: 4 bedrooms\n"
            "Budget: up to RM12k",
        )
        self.assertIn('"bedroom_requirement":"4 bedrooms"', payload["searchBriefJSON"])

    @patch("app.save_search_state")
    @patch("app.extract_search_update_from_profile")
    @patch("app.bubble")
    def test_existing_structured_lead_does_not_run_profile_extraction(
        self, mocked_bubble, mocked_extract, _mocked_save
    ):
        state = apply_search_update(empty_search_state(), {
            "area_status": "known", "areas": ["Bangsar"],
        })
        mocked_bubble.side_effect = [
            {"lead": "lead-1"},
            {"AIsearchtext": "legacy profile", "searchBriefJSON": json.dumps(state)},
        ]

        app_module.advance_property_search("folio-1", "live", {})

        mocked_extract.assert_not_called()

    def test_structured_profile_text_remains_complete_for_matching(self):
        text = app_module.search_state_to_requirements_text(complete_state())
        self.assertIn("Areas: Bangsar", text)
        self.assertIn("Property types: Condo", text)
        self.assertIn("Bedrooms: exactly 4 bedrooms", text)
        self.assertIn("Budget: maximum RM12,000", text)
        self.assertIn("Other requirements: furnished, balcony", text)
        self.assertIn("Ordered priorities: location, bedrooms, balcony", text)

    @patch("app.requests.patch")
    def test_structured_save_logs_bubble_error_body(self, mocked_patch):
        response = mocked_patch.return_value
        response.status_code = 400
        response.text = '{"error":"invalid field"}'
        response.raise_for_status.side_effect = app_module.requests.HTTPError("bad request")

        with patch("builtins.print") as mocked_print, self.assertRaises(
            app_module.requests.HTTPError
        ):
            app_module.save_search_state("lead-1", empty_search_state(), "https://bubble.test")

        self.assertTrue(any(
            'HTTP 400 body={"error":"invalid field"}' in str(call)
            for call in mocked_print.call_args_list
        ))

    @patch("app.save_search_state")
    @patch("app.extract_search_update_from_profile")
    @patch("app.bubble")
    def test_existing_saved_preferences_are_migrated_and_not_reasked(
        self, mocked_bubble, mocked_extract, _mocked_save
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"},
            {"AIsearchtext": "Bangsar; condo; 4 bedrooms", "searchBriefJSON": ""},
        ]
        mocked_extract.return_value = {
            "area_status": "known", "areas": ["Bangsar"],
            "regular_destinations": [], "property_types": ["condo"],
            "bedroom_requirement": "4 bedrooms", "budget_requirement": "",
            "other_requirements": [], "other_requirements_answered": False,
            "priorities": [], "priorities_answered": False,
        }
        result = app_module.advance_property_search("folio-1", "live", {})
        self.assertEqual(result["text"], "What's your monthly rental budget?")
        mocked_extract.assert_called_once()

    @patch("app.save_search_state")
    @patch("app.recommend_condos_for_search")
    @patch("app.bubble")
    def test_complete_brief_recommends_condos_before_inventory(
        self, mocked_bubble, mocked_recommend, _mocked_save
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"}, {"searchBriefJSON": json.dumps(complete_state())}
        ]
        mocked_recommend.return_value = (
            [{"condo_name": "One Menerung", "reason": "Fits the brief"}],
            "I'd focus on One Menerung.",
        )
        result = app_module.advance_property_search("folio-1", "live", {})
        self.assertEqual(result["action"], "condo_shortlist")
        self.assertIn("Which would you like to explore?", result["text"])

    def test_listing_filter_resolves_bubble_condo_relationship(self):
        listing = {"_id": "listing-1", "condo": "condo-id"}
        with patch("app.bubble", return_value={"name": "One Menerung"}):
            self.assertTrue(app_module._listing_is_in_condo_scope(
                listing, ["One Menerung"], "https://bubble.test", {}
            ))
            self.assertFalse(app_module._listing_is_in_condo_scope(
                listing, ["Ken Bangsar"], "https://bubble.test", {}
            ))

    def test_search_update_preserves_nuance_and_multiple_preferences(self):
        text = app_module.search_update_preference_text({
            "bedroom_requirement": "minimum 4 bedrooms; 3+1 acceptable",
            "other_requirements": ["dog allowed", "two car parks", "balcony"],
            "priorities": ["school commute", "space", "security"],
        })
        self.assertIn("minimum 4 bedrooms; 3+1 acceptable", text)
        self.assertIn("dog allowed, two car parks, balcony", text)
        self.assertIn("school commute, space, security", text)


if __name__ == "__main__":
    unittest.main()
