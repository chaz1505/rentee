import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module
from search_flow import (
    apply_search_update,
    empty_search_state,
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
        self.assertIn("anything else", next_search_question(state))

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
        self.assertIn("need to go regularly", next_search_question(state))

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
        self.assertIn("asks one next question", tools["advance_property_search"]["description"])

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
