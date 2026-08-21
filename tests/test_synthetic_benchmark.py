import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, mock_open, patch

from . import run_benchmark


class SyntheticBenchmarkTests(unittest.TestCase):
    def test_default_suite_contains_only_synthetic_cases(self):
        self.assertEqual(run_benchmark.get_benchmark_case_ids(), [
            "synthetic_expat_family_01",
            "synthetic_refinement_01",
            "synthetic_specific_search_01",
        ])
        selected = run_benchmark._select_cases()
        self.assertTrue(selected)
        self.assertTrue(all(
            case.get("conversation_mode") == "synthetic" for case in selected
        ))

    @patch("tests.run_benchmark.validate_benchmark_environment")
    @patch("tests.run_benchmark.run_case")
    def test_default_batch_does_not_run_sofia(self, run_case, _validate):
        run_case.side_effect = lambda case, **_kwargs: {
            "case_id": case["id"], "failure": None
        }
        suite = run_benchmark.run_all_benchmarks()
        run_ids = [call.args[0]["id"] for call in run_case.call_args_list]
        self.assertNotIn("sofia_01", run_ids)
        self.assertEqual(run_ids, run_benchmark.get_benchmark_case_ids())
        self.assertFalse(suite["failed"])

    @patch("tests.run_benchmark.validate_benchmark_environment")
    @patch("tests.run_benchmark.run_case")
    def test_sofia_still_exists_and_can_run_explicitly(
        self, run_case, _validate
    ):
        run_case.side_effect = lambda case, **_kwargs: {
            "case_id": case["id"], "failure": None
        }
        suite = run_benchmark.run_all_benchmarks(case_ids=["sofia_01"])
        selected_case = run_case.call_args.args[0]
        self.assertEqual(selected_case["id"], "sofia_01")
        self.assertNotEqual(
            selected_case.get("conversation_mode"), "synthetic"
        )
        self.assertEqual(len(selected_case["turns"]), 5)
        self.assertEqual(suite["results"][0]["case_id"], "sofia_01")

    def test_existing_scripted_case_is_unchanged(self):
        sofia = next(
            case for case in run_benchmark._load_cases()
            if case["id"] == "sofia_01"
        )
        self.assertNotEqual(sofia.get("conversation_mode"), "synthetic")
        self.assertEqual(len(sofia["turns"]), 5)
        self.assertIn("two cats", sofia["turns"][1]["message"].lower())
        self.assertIn(
            "sofia_01",
            run_benchmark.get_benchmark_case_ids(include_scripted=True),
        )

    def test_three_synthetic_cases_are_available(self):
        cases = {
            case["id"]: case for case in run_benchmark._load_cases()
        }
        expected = {
            "synthetic_expat_family_01",
            "synthetic_refinement_01",
            "synthetic_specific_search_01",
        }
        self.assertEqual(
            {case_id for case_id in cases if case_id.startswith("synthetic_")},
            expected,
        )
        self.assertTrue(all(
            cases[case_id]["conversation_mode"] == "synthetic"
            for case_id in expected
        ))

    @patch("tests.run_benchmark.OpenAI")
    def test_synthetic_model_receives_persona_and_complete_conversation(self, client):
        create = client.return_value.responses.create
        create.return_value = SimpleNamespace(output_text=json.dumps({
            "finished": False,
            "wants_to_view": [],
            "message": "Could you show me more?",
        }))
        case = {
            "synthetic_persona": {"identity": "Hidden renter persona"}
        }
        conversation = [
            {"tenant_message": "Hello", "rentee_response": "Hi, what do you need?"},
            {"tenant_message": "Four bedrooms", "rentee_response": "Here is Alpha Residence"},
        ]

        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}):
            decision = run_benchmark.generate_synthetic_user_turn(
                case, conversation
            )

        self.assertEqual(decision["message"], "Could you show me more?")
        payload = json.loads(create.call_args.kwargs["input"])
        self.assertEqual(payload["hidden_customer_persona"], case["synthetic_persona"])
        self.assertEqual(len(payload["conversation_so_far"]), 2)
        self.assertEqual(
            payload["conversation_so_far"][1]["rentee"],
            "Here is Alpha Residence",
        )
        self.assertEqual(create.call_args.kwargs["model"], "gpt-5-mini")

    def test_wanted_listings_must_have_been_presented_by_rentee(self):
        conversation = [{
            "rentee_response": "I can show you Alpha Residence and Beta Heights."
        }]
        self.assertEqual(
            run_benchmark.validate_wants_to_view(
                ["Alpha Residence", "Invented Towers", "Beta Heights"],
                conversation,
            ),
            ["Alpha Residence", "Beta Heights"],
        )

    def test_one_wanted_listing_does_not_finish(self):
        action = run_benchmark.resolve_synthetic_decision(
            {
                "finished": True,
                "wants_to_view": ["Alpha Residence"],
                "message": "Do you have another option?",
            },
            [{"rentee_response": "Alpha Residence is available."}],
            3,
        )
        self.assertEqual(action["status"], "continue")
        self.assertEqual(action["message"], "Do you have another option?")

    def test_two_presented_wanted_listings_finish_before_limit(self):
        action = run_benchmark.resolve_synthetic_decision(
            {
                "finished": True,
                "wants_to_view": ["Alpha Residence", "Beta Heights"],
                "message": None,
            },
            [{"rentee_response": "Consider Alpha Residence and Beta Heights."}],
            4,
        )
        self.assertEqual(action["status"], "success")
        self.assertEqual(len(action["wants_to_view"]), 2)

    def test_fifteen_customer_messages_force_distinct_max_turn_status(self):
        action = run_benchmark.resolve_synthetic_decision(
            {
                "finished": False,
                "wants_to_view": ["Alpha Residence"],
                "message": "Anything else?",
            },
            [{"rentee_response": "Alpha Residence is available."}],
            15,
        )
        self.assertEqual(action["status"], "max_turns")
        self.assertNotEqual(action["status"], "success")

    def test_hidden_persona_is_not_part_of_rentee_turn_payload(self):
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            'data: {"delta":"Hello"}', "",
            'data: {"done":true,"response_id":"response-1"}', "",
        ]
        with patch("tests.run_benchmark.requests.post", return_value=response) as post:
            run_benchmark.run_turn("Customer-visible message", "folio-1")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["message"], "Customer-visible message")
        self.assertNotIn("persona", json.dumps(payload).lower())
        self.assertNotIn("evaluation", json.dumps(payload).lower())

    def test_synthetic_model_is_configurable_without_changing_evaluator(self):
        self.assertEqual(
            run_benchmark.SYNTHETIC_USER_MODEL,
            os.environ.get("RENTEE_SYNTHETIC_USER_MODEL", "gpt-5-mini"),
        )
        self.assertEqual(run_benchmark.SYNTHETIC_MAX_CUSTOMER_MESSAGES, 15)

    def test_completed_synthetic_conversation_uses_existing_evaluation_pipeline(self):
        case = {
            "id": "synthetic_test", "name": "Synthetic test",
            "conversation_mode": "synthetic",
            "initial_ai_searchtext": "No preferences provided yet.",
            "synthetic_persona": {"identity": "Hidden"},
            "turns": [{}] * 15,
        }
        decisions = [
            {"finished": False, "wants_to_view": [], "message": "Show me homes"},
            {
                "finished": True,
                "wants_to_view": ["Alpha Residence", "Beta Heights"],
                "message": None,
            },
        ]
        rentee_turn = {
            "text": "Consider Alpha Residence and Beta Heights.",
            "response_id": "response-1", "previous_response_id_sent": None,
            "statuses": [], "citations": [], "errors": [],
            "timing": {"first_event_s": 1, "first_delta_s": 2, "total_s": 3},
            "done_seen": True,
        }
        evaluation = {
            "overall_status": "pass", "issues": [], "metrics": {},
            "comparison_to_previous_run": {"available": False},
        }
        with (
            patch("tests.run_benchmark.get_bubble_base"),
            patch("tests.run_benchmark.ensure_test_subject", return_value={
                "lead_id": "lead", "folio_id": "folio"
            }),
            patch("tests.run_benchmark.reset_test_subject"),
            patch("tests.run_benchmark.snapshot_test_subject", return_value={
                "lead": {}, "folio": {}
            }),
            patch("tests.run_benchmark.generate_synthetic_user_turn", side_effect=decisions),
            patch("tests.run_benchmark.run_turn", return_value=rentee_turn) as run_turn,
            patch("tests.run_benchmark._save_result", return_value="raw.json"),
            patch("tests.run_benchmark.get_previous_benchmark_run", return_value=None),
            patch("tests.run_benchmark.evaluate_run", return_value=(
                "evaluation.json", evaluation
            )) as evaluate,
            patch("tests.run_benchmark.generate_evaluation_markdown", return_value="evaluation.md"),
            patch("tests.run_benchmark.generate_fix_prompt", return_value="fix.md"),
            patch("tests.run_benchmark.save_benchmark_run", return_value="run-id"),
            patch("builtins.open", mock_open(read_data="artifact")),
        ):
            result = run_benchmark.run_case(case, run_id="synthetic-run")

        run_turn.assert_called_once_with(
            "Show me homes", "folio", None, "development"
        )
        evaluate.assert_called_once_with(
            "raw.json", previous_benchmark_run=None
        )
        self.assertEqual(result["synthetic_completion"]["status"], "success")
        self.assertEqual(result["synthetic_completion"]["customer_messages"], 1)


if __name__ == "__main__":
    unittest.main()
