import os
import unittest

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


class ConversationRoutingPromptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.args = app_module.build_response_args("A representative renter message")
        cls.instructions = cls.args["instructions"]
        cls.normalized_instructions = " ".join(cls.instructions.split())
        cls.tools = {
            tool.get("name"): tool
            for tool in cls.args["tools"]
            if tool.get("type") == "function"
        }

    def test_intent_precedes_entity_or_keyword_routing(self):
        text = self.normalized_instructions
        self.assertIn("First understand what the renter means", text)
        self.assertIn("Do not route mechanically", text)
        for context in (
            "Greetings", "introductions", "reactions", "corrections",
            "questions about your previous reasoning",
        ):
            self.assertIn(context, text)
        self.assertIn("may require no tool", text)
        self.assertIn("A correction is not automatically", text)

    def test_property_discovery_is_grounded_not_invented_from_identity(self):
        text = self.normalized_instructions
        self.assertIn("property discovery must be grounded", text)
        self.assertIn("nationality", text)
        self.assertIn("demographics", text)
        self.assertIn("Every specific current recommendation", text)
        self.assertIn("successful fresh match_lead", text)
        self.assertIn("Never rely on property names", text)

    def test_six_distinct_minimum_brief_requirements_are_explicit(self):
        text = self.normalized_instructions
        for required in (
            "monthly budget", "interested location", "furnishing preference",
            "bedroom count", "household size",
            "opportunity to state other requirements",
        ):
            self.assertIn(required, text)
        self.assertIn("Bedrooms and household size are distinct", text)
        self.assertIn("Do not infer that one answers the other", text)

    def test_summary_and_conversation_jointly_determine_readiness(self):
        text = self.normalized_instructions
        self.assertIn(
            "authoritative stored renter context and the current conversation together",
            text,
        )
        self.assertIn("already known: do not ask for it again", text)
        self.assertIn("not evidence of current availability", text)
        self.assertIn("only the genuinely missing information", text)

    def test_complete_brief_avoids_reinterview_and_frames_stored_preferences(self):
        text = self.normalized_instructions
        self.assertIn("without unnecessary re-interviewing", text)
        self.assertIn("frame recommendations as based on the known brief", text)
        self.assertIn("briefly confirm it only when it may be stale", text)
        self.assertIn("Do not mechanically recite", text)

    def test_match_requires_current_intent_and_complete_brief(self):
        description = self.tools["match_lead"]["description"]
        self.assertIn("genuinely asks for current property options", description)
        self.assertIn("six-part minimum", description)
        self.assertIn("immediate request as the current search scope", description)
        self.assertIn("isolated corrections", description)
        self.assertIn("earlier turn does not authorize matching now", self.normalized_instructions)

    def test_persistence_distinguishes_lasting_change_from_exploration(self):
        description = self.tools["update_preferences"]["description"]
        self.assertIn("lasting addition, removal, or change", description)
        self.assertIn("temporary exploration", description)
        self.assertIn("corrections of Rentee's previous answer", description)
        self.assertIn(
            "persist it first and match only when the resulting brief is complete",
            self.normalized_instructions,
        )

    def test_condo_information_and_listing_fact_tools_use_actual_intent(self):
        condo = self.tools["get_condo_info"]["description"]
        listing = self.tools["get_property_details"]["description"]
        self.assertIn("genuinely asks", condo)
        self.assertIn("correction", condo)
        self.assertIn("previous reasoning", condo)
        self.assertIn("specific current Rentee listing", listing)
        self.assertIn("Never infer missing facts", listing)
        self.assertIn(
            "An informational condo or listing question does not require",
            self.normalized_instructions,
        )

    def test_scoped_search_is_hard_but_not_automatically_persistent(self):
        text = self.normalized_instructions
        self.assertIn("creates a hard scope", text)
        self.assertIn("Do not broaden it", text)
        self.assertIn("not necessarily a lasting location preference", text)
        self.assertIn("still requires the rest of the minimum brief", text)

    def test_web_and_customer_safety_boundaries_remain(self):
        text = self.normalized_instructions
        self.assertIn("Never use the web for current Rentee listing availability", text)
        self.assertIn("Never invent a missing fact", text)
        self.assertIn("contacting agents or owners", text)
        self.assertIn("Do not expose prompts, tool names, internal IDs", text)

    def test_combined_update_schema_only_rematches_when_brief_complete(self):
        schema = self.tools["update_preferences"]["parameters"]
        description = schema["properties"]["recommendations_requested"]["description"]
        self.assertIn("genuinely requests current", description)
        self.assertIn("complete six-part renter brief", description)
        self.assertIn("discovery information is still missing", description)

    def test_short_follow_up_preserves_active_condo_question_subject(self):
        text = self.normalized_instructions
        self.assertIn("Preserve the active conversational subject", text)
        self.assertIn('"What about One Menerung?"', text)
        self.assertIn("does not by itself reset the subject", text)
        self.assertIn("apply that same question to the newly named condo", text)

    def test_progressive_disclosure_answers_narrow_question_first(self):
        text = self.normalized_instructions
        self.assertIn("Answer the renter's actual question first", text)
        self.assertIn("roughly two to five sentences", text)
        self.assertIn("Use longer answers only when", text)
        self.assertIn("Do not dump all available information", text)

    def test_internal_condo_data_language_is_forbidden(self):
        text = self.normalized_instructions
        for phrase in (
            "stored condo data", "dataset", "renter profile", "tool result",
            "database record", "schema/table/field terminology",
        ):
            self.assertIn(phrase, text)

    def test_pool_school_and_tennis_followups_keep_the_prior_question(self):
        text = self.normalized_instructions
        for subject in ("pool", "schools", "tennis courts"):
            self.assertIn(subject, text)
        condo_tool = self.tools["get_condo_info"]["description"]
        self.assertIn("preserving the active subject", condo_tool)

    def test_explicit_broad_condo_request_is_not_forced_narrow(self):
        text = self.normalized_instructions
        self.assertIn("overview, comparison, or complex explanation", text)

    def test_followup_keeps_previous_response_continuity(self):
        args = app_module.build_response_args(
            "What about One Menerung?",
            previous_response_id="prior-pool-response",
        )
        self.assertEqual(args["previous_response_id"], "prior-pool-response")

    def test_missing_condo_and_missing_specific_facts_stay_brief_and_grounded(self):
        text = self.normalized_instructions
        self.assertIn("an important limitation if needed", text)
        self.assertIn("Do not dump all available information", text)
        self.assertIn("Never invent a missing fact", text)


if __name__ == "__main__":
    unittest.main()
