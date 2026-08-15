import json
import os
import unittest
from unittest.mock import Mock, patch

from . import bubble_test_data
from . import evaluate_run
from . import generate_fix_prompt
from . import run_benchmark


class BenchmarkInfrastructureTests(unittest.TestCase):
    def test_rejects_non_development_bubble_base(self):
        with patch.dict(os.environ, {
            "BUBBLE_DEV_BASE": "https://www.rentee.asia/api/1.1"
        }, clear=False):
            with self.assertRaisesRegex(
                bubble_test_data.BubbleTestDataError,
                "/version-test/"
            ):
                bubble_test_data.get_bubble_dev_base()

    @patch("tests.run_benchmark.requests.post")
    def test_run_turn_parses_sse_and_returns_continuity_id(self, mocked_post):
        events = [
            {"status": "Updating your preferences..."},
            {"delta": "Hello "},
            {"delta": "Sofia"},
            {"citations": [{"url": "https://example.com"}]},
            {"done": True, "response_id": "response-2"}
        ]
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            line
            for event in events
            for line in (f"data: {json.dumps(event)}", "")
        ]
        mocked_post.return_value = response

        result = run_benchmark.run_turn(
            "Hello", "folio-1", previous_response_id="response-1"
        )

        self.assertEqual(result["text"], "Hello Sofia")
        self.assertEqual(result["response_id"], "response-2")
        self.assertEqual(result["previous_response_id_sent"], "response-1")
        self.assertTrue(result["done_seen"])
        sent_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["previous_response_id"], "response-1")
        self.assertEqual(sent_payload["bubble_env"], "development")

    @patch("tests.run_benchmark.requests.post")
    def test_run_turn_requires_response_id(self, mocked_post):
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            'data: {"delta":"Hi"}',
            "",
            'data: {"done":true}',
            ""
        ]
        mocked_post.return_value = response

        with self.assertRaisesRegex(run_benchmark.BenchmarkError, "No response_id"):
            run_benchmark.run_turn("Hello", "folio-1")

    def test_deterministic_evaluation_and_fix_prompt(self):
        case = {
            "id": "generic_01",
            "name": "Generic case",
            "turns": [
                {"message": "We have two cats."},
                {"message": "What do you recommend?", "expectation": "recommendations"}
            ],
            "preference_checks": [
                {"id": "cats", "description": "two cats", "patterns": [r"two cats"]}
            ]
        }
        result = {
            "case_id": "generic_01",
            "case_name": "Generic case",
            "initial_state": {"lead": {}, "folio": {}},
            "final_state": {"lead": {"AIsearchtext": "two cats"}, "folio": {}},
            "turns": [
                {
                    "turn": 1,
                    "tenant_message": "We have two cats.",
                    "rentee_response": "What area? What size? What budget? What date? I’ll contact agents.",
                    "response_id": "r1",
                    "previous_response_id_sent": None,
                    "errors": [], "done_seen": True,
                    "timing": {"first_event_s": 1, "first_delta_s": 25, "total_s": 30}
                },
                {
                    "turn": 2,
                    "tenant_message": "What do you recommend?",
                    "rentee_response": "I can contact the shortlisted listings and will check later.",
                    "response_id": "r2",
                    "previous_response_id_sent": "wrong-id",
                    "errors": [], "done_seen": True,
                    "timing": {"first_event_s": 1, "first_delta_s": 22, "total_s": 30}
                }
            ]
        }
        metrics, issues, _ = evaluate_run.deterministic_evaluation(result, case)
        issue_ids = {issue["id"] for issue in issues}
        self.assertEqual(metrics["critical_slow_turns"], [1, 2])
        self.assertIn("unsupported_actions", issue_ids)
        self.assertIn("excessive_questioning", issue_ids)
        self.assertIn("recommendation_request_not_answered", issue_ids)
        self.assertIn("infrastructure_failure", issue_ids)

        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            result_path = os.path.join(temp_dir, "generic_01_20260815_000000.json")
            evaluation_path = result_path[:-5] + "_evaluation.json"
            with open(result_path, "w", encoding="utf-8") as output:
                json.dump(result, output)
            with open(evaluation_path, "w", encoding="utf-8") as output:
                json.dump({
                    "overall_status": "fail", "metrics": metrics, "issues": issues,
                    "qualitative_evaluation": {}
                }, output)
            prompt_path = generate_fix_prompt.generate_fix_prompt(result_path, evaluation_path)
            with open(prompt_path, "r", encoding="utf-8") as prompt_file:
                prompt = prompt_file.read()
            self.assertIn("Do not special-case Sofia", prompt)
            self.assertIn("Regression constraints", prompt)

    @patch("tests.run_benchmark.generate_fix_prompt", return_value="fix.md")
    @patch("tests.run_benchmark.generate_evaluation_markdown", return_value="evaluation.md")
    @patch("tests.run_benchmark.evaluate_run")
    @patch("tests.run_benchmark._save_result", return_value="partial.json")
    @patch("tests.run_benchmark.snapshot_test_subject")
    @patch("tests.run_benchmark.reset_test_subject")
    @patch("tests.run_benchmark.ensure_test_subject")
    @patch("tests.run_benchmark.get_bubble_base")
    @patch("tests.run_benchmark.run_turn")
    def test_failed_turn_is_saved_evaluated_and_gets_fix_prompt(
        self, mocked_turn, _mocked_base, mocked_subject, _mocked_reset,
        mocked_snapshot, mocked_save, mocked_evaluate, mocked_markdown,
        mocked_prompt
    ):
        partial = {
            "text": "", "response_id": None,
            "previous_response_id_sent": "r4", "statuses": [], "citations": [],
            "errors": ["No tool output found for function call call_123"],
            "timing": {"first_event_s": 1, "first_delta_s": None, "total_s": 2},
            "done_seen": True
        }
        mocked_turn.side_effect = run_benchmark.BenchmarkTurnError(
            "SSE returned an error event", partial
        )
        mocked_subject.return_value = {"lead_id": "lead", "folio_id": "folio"}
        mocked_snapshot.return_value = {"lead": {}, "folio": {}}
        mocked_evaluate.return_value = (
            "evaluation.json",
            {"overall_status": "fail", "issues": [], "comparison_to_previous_run": {"available": False}}
        )
        case = {
            "id": "failure_01", "name": "Failure",
            "initial_ai_searchtext": "none", "turns": [{"message": "hello"}]
        }

        result = run_benchmark.run_case(case)

        self.assertEqual(result["failure"]["turn"], 1)
        self.assertEqual(result["turns"][0]["errors"], partial["errors"])
        mocked_save.assert_called_once()
        mocked_evaluate.assert_called_once_with("partial.json")
        mocked_markdown.assert_called_once_with("partial.json", "evaluation.json")
        mocked_prompt.assert_called_once_with("partial.json", "evaluation.json")


if __name__ == "__main__":
    unittest.main()
