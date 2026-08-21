import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


class FrontDoorPreferenceContextTests(unittest.TestCase):
    SUMMARY = (
        "Budget: RM12,000\nLocation: Bangsar\nBedrooms: 4\n"
        "Household: 2 adults + 2 children\nFurnishing: furnished\n"
        "Other: modern property preferred"
    )

    def test_front_door_input_contains_compact_summary_and_current_message(self):
        args = app_module.build_response_args(
            "Show me current options", renter_summary=self.SUMMARY
        )

        self.assertEqual(
            args["input"],
            "KNOWN RENTER PREFERENCES\n\n"
            f"{self.SUMMARY}\n\n"
            "CURRENT CUSTOMER MESSAGE\n\nShow me current options",
        )
        self.assertNotIn("folio", args["input"].lower())
        self.assertIn("do not ask for it again", args["instructions"])

    @patch("app.log_timing")
    @patch("app.bubble")
    def test_summary_lookup_uses_selected_bubble_environment(
        self, mocked_bubble, mocked_timing
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"},
            {"AIsearchsummary": self.SUMMARY},
        ]

        result = app_module.load_front_door_renter_summary(
            "folio-1", "development"
        )

        self.assertEqual(result, self.SUMMARY)
        self.assertEqual(mocked_bubble.call_args_list, [
            call("https://www.rentee.asia/version-test/api/1.1/obj/folio/folio-1"),
            call("https://www.rentee.asia/version-test/api/1.1/obj/lead/lead-1"),
        ])
        mocked_timing.assert_called_once()

    @patch("app.bubble", side_effect=RuntimeError("temporary Bubble failure"))
    def test_summary_lookup_fails_safe_without_exposing_ids_to_model(self, _bubble):
        summary = app_module.load_front_door_renter_summary("folio-secret", "live")
        args = app_module.build_response_args("Hello", renter_summary=summary)

        self.assertEqual(summary, "Stored preferences are temporarily unavailable.")
        self.assertNotIn("folio-secret", args["input"])


class PreferencePersistenceTests(unittest.TestCase):
    def test_replacement_preserves_unrelated_requirements(self):
        merged = app_module.merge_updated_preference_text(
            "Location: Bangsar; Budget: RM12,000\nBedrooms: 4",
            "Budget: RM15,000",
            "Budget is now RM15,000",
        )

        self.assertIn("Location: Bangsar", merged)
        self.assertIn("Bedrooms: 4", merged)
        self.assertIn("Budget: RM15,000", merged)
        self.assertNotIn("Budget: RM12,000", merged)

    @patch("app.update_lead_ai_searchtext")
    @patch("app.bubble")
    def test_replacement_summary_is_generated_from_guarded_final_profile(
        self, mocked_bubble, mocked_update
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"},
            {"AIsearchtext": "Location: Bangsar\nBudget: RM12,000\nBedrooms: 4"},
        ]
        rewrite = SimpleNamespace(
            output_text=json.dumps({
                "updated_ai_search_text": "Budget: RM15,000",
                "ai_search_summary": "Budget: RM15,000",
                "confirmation": "I’ve updated your budget.",
            }),
            usage=None,
        )
        clean_summary = SimpleNamespace(
            output_text=json.dumps({
                "ai_search_summary": (
                    "Location: Bangsar\nBudget: RM15,000\nBedrooms: 4"
                )
            }),
            usage=None,
        )
        fake_client = MagicMock()
        fake_client.responses.create.side_effect = [rewrite, clean_summary]

        with patch.object(app_module, "client", fake_client):
            result = app_module.update_preferences(
                "folio-1", "Budget is now RM15,000", "development"
            )

        self.assertEqual(result, "I’ve updated your budget.")
        final_text = mocked_update.call_args.args[1]
        final_summary = mocked_update.call_args.args[2]
        self.assertIn("Location: Bangsar", final_text)
        self.assertIn("Bedrooms: 4", final_text)
        self.assertNotIn("RM12,000", final_text)
        self.assertEqual(
            final_summary,
            "Location: Bangsar\nBudget: RM15,000\nBedrooms: 4",
        )
        self.assertEqual(
            fake_client.responses.create.call_args_list[1].kwargs["input"],
            final_text,
        )


class FlexibleMatchingGuidanceTests(unittest.TestCase):
    def test_approximate_budget_allows_explained_tradeoff(self):
        source = app_module.match_lead.__code__
        self.assertIsNotNone(source)
        with open(app_module.__file__, encoding="utf-8") as app_file:
            text = app_file.read()
        self.assertIn("approximate budget", text)
        self.assertIn("may reasonably", text)
        self.assertIn("Explain the compromise clearly", text)
        self.assertIn("Do not use arbitrary stretch percentages", text)

    def test_absolute_budget_and_current_named_scope_remain_hard(self):
        with open(app_module.__file__, encoding="utf-8") as app_file:
            text = app_file.read()
        self.assertIn("absolute budget ceiling", text)
        self.assertIn("Explicit immediate search scope is always hard", text)
        self.assertIn("Do not substitute other", text)


if __name__ == "__main__":
    unittest.main()
