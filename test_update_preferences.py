import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module


class UpdatePreferencesTests(unittest.TestCase):
    EXISTING_PROFILE = (
        "Transaction: Rent\n"
        "Area: Bangsar\n"
        "Budget: RM12k\n"
        "Bedrooms: 4\n"
        "Facilities: Pool\n"
        "secret notes: Keep this exact history entry."
    )

    CASES = (
        (
            "furnished please",
            "Transaction: Rent\nArea: Bangsar\nBudget: RM12k\nBedrooms: 4\n"
            "Facilities: Pool\nFurnishing: Furnished\n"
            "secret notes: Keep this exact history entry.",
            "Area: Bangsar\nBudget: RM12k\nBedrooms: 4\nFurnishing: Furnished",
            "I've updated your preference to furnished homes.",
        ),
        (
            "my budget is now RM15k",
            "Transaction: Rent\nArea: Bangsar\nBudget: RM15k\nBedrooms: 4\n"
            "Facilities: Pool\nsecret notes: Keep this exact history entry.",
            "Area: Bangsar\nBudget: RM15k\nBedrooms: 4\nFacilities: Pool",
            "I've updated your monthly budget to RM15k.",
        ),
        (
            "we no longer need a pool",
            "Transaction: Rent\nArea: Bangsar\nBudget: RM12k\nBedrooms: 4\n"
            "secret notes: Keep this exact history entry.",
            "Area: Bangsar\nBudget: RM12k\nBedrooms: 4",
            "I've removed the pool requirement.",
        ),
        (
            "we're also open to Mont Kiara",
            "Transaction: Rent\nAreas: Bangsar, Mont Kiara\nBudget: RM12k\nBedrooms: 4\n"
            "Facilities: Pool\nsecret notes: Keep this exact history entry.",
            "Areas: Bangsar, Mont Kiara\nBudget: RM12k\nBedrooms: 4\nFacilities: Pool",
            "I've added Mont Kiara to your preferred areas.",
        ),
    )

    def test_minimal_reasoning_and_strict_output_for_common_updates(self):
        for preference_update, updated_text, summary, confirmation in self.CASES:
            with self.subTest(preference_update=preference_update):
                response = SimpleNamespace(
                    output_text=json.dumps({
                        "updated_ai_search_text": updated_text,
                        "ai_search_summary": summary,
                        "confirmation": confirmation,
                    }),
                    usage=None,
                )
                responses = MagicMock()
                responses.create.return_value = response
                fake_client = SimpleNamespace(responses=responses)

                with patch.object(app_module, "client", fake_client), patch(
                    "app.bubble",
                    side_effect=[
                        {"lead": "lead-1"},
                        {"AIsearchtext": self.EXISTING_PROFILE},
                    ],
                ), patch("app.update_lead_ai_searchtext") as mocked_update:
                    result = app_module.update_preferences(
                        "folio-1", preference_update, "live"
                    )

                self.assertEqual(result, confirmation)
                mocked_update.assert_called_once_with(
                    "lead-1", updated_text, summary,
                    "https://www.rentee.asia/api/1.1",
                )
                call = responses.create.call_args.kwargs
                self.assertEqual(call["model"], "gpt-5-mini")
                self.assertEqual(call["reasoning"], {"effort": "minimal"})
                self.assertTrue(call["text"]["format"]["strict"])
                self.assertEqual(
                    set(call["text"]["format"]["schema"]["required"]),
                    {"updated_ai_search_text", "ai_search_summary", "confirmation"},
                )
                self.assertIn(self.EXISTING_PROFILE, call["input"])
                self.assertIn(preference_update, call["input"])
                self.assertIn("minimum reasoning", call["input"])
                self.assertIn("one short, natural sentence", call["instructions"])


if __name__ == "__main__":
    unittest.main()
