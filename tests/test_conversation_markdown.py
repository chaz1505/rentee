import os
import tempfile
import unittest
from unittest.mock import mock_open, patch

from .generate_conversation_markdown import generate_conversation_markdown
from . import run_benchmark


class ConversationMarkdownTests(unittest.TestCase):
    def result(self):
        return {
            "run_id": "20260817_201900_synthetic_case_live",
            "case_id": "synthetic_case",
            "case_name": "Synthetic case",
            "bubble_env": "live",
            "turns": [
                {
                    "turn": 1,
                    "tenant_message": "Exact customer message\nwith a second line.",
                    "rentee_response": "Exact Rentee response — unchanged.\n- Alpha Residence",
                    "response_id": "response-1",
                    "previous_response_id_sent": None,
                    "timing": {
                        "first_event_s": 0.5,
                        "first_delta_s": 1.25,
                        "total_s": 4.75,
                    },
                    "errors": [],
                    "done_seen": True,
                },
                {
                    "turn": 2,
                    "tenant_message": "And Beta Heights?",
                    "rentee_response": "Beta Heights is unavailable right now.",
                    "response_id": None,
                    "previous_response_id_sent": "response-1",
                    "timing": {
                        "first_event_s": 0.7,
                        "first_delta_s": None,
                        "total_s": 2.0,
                    },
                    "errors": ["SSE returned an error event"],
                    "done_seen": False,
                },
            ],
            "final_state": {
                "lead": {"AIsearchtext": "Budget RM12,000\nNeeds 4 bedrooms"}
            },
            "failure": None,
            "synthetic_completion": {
                "status": "success",
                "wants_to_view": ["Alpha Residence", "Beta Heights"],
                "customer_messages": 2,
            },
        }

    def synthetic_case(self):
        return {
            "id": "synthetic_case",
            "name": "Synthetic case",
            "conversation_mode": "synthetic",
            "synthetic_persona": {
                "identity": "Hidden family renter",
                "true_requirements": ["4 bedrooms", "RM12,000 maximum"],
            },
        }

    def test_generates_complete_human_readable_synthetic_artifact(self):
        result = self.result()
        with tempfile.TemporaryDirectory() as directory:
            path = generate_conversation_markdown(
                result, self.synthetic_case(), directory
            )
            with open(path, "r", encoding="utf-8") as artifact:
                markdown = artifact.read()

        self.assertEqual(
            os.path.basename(path),
            "20260817_201900_synthetic_case_live_conversation.md",
        )
        self.assertIn("Exact customer message\nwith a second line.", markdown)
        self.assertIn("Exact Rentee response — unchanged.\n- Alpha Residence", markdown)
        self.assertIn("And Beta Heights?", markdown)
        self.assertIn("Beta Heights is unavailable right now.", markdown)
        self.assertIn("- First event: 0.5s", markdown)
        self.assertIn("- First text: unavailable", markdown)
        self.assertIn("SSE returned an error event", markdown)
        self.assertIn("Budget RM12,000\nNeeds 4 bedrooms", markdown)
        self.assertIn("Hidden family renter", markdown)
        self.assertIn("4 bedrooms", markdown)
        self.assertIn("- Alpha Residence", markdown)
        self.assertIn("Termination reason: synthetic success", markdown)
        self.assertIn("Successful completion reached: True", markdown)
        self.assertIn("Maximum 15-customer-turn limit reached: False", markdown)

    def test_scripted_case_has_no_synthetic_ground_truth(self):
        result = self.result()
        result.pop("synthetic_completion")
        result["case_id"] = "sofia_01"
        result["case_name"] = "Sofia"
        with tempfile.TemporaryDirectory() as directory:
            path = generate_conversation_markdown(
                result, {"id": "sofia_01", "name": "Sofia"}, directory
            )
            with open(path, "r", encoding="utf-8") as artifact:
                markdown = artifact.read()
        self.assertIn("Completion reason: scripted completion", markdown)
        self.assertNotIn("Synthetic Customer Ground Truth", markdown)

    def test_partial_failure_preserves_captured_conversation(self):
        result = self.result()
        result.pop("synthetic_completion")
        result["failure"] = {
            "turn": 2, "type": "request_failure", "message": "connection lost"
        }
        with tempfile.TemporaryDirectory() as directory:
            path = generate_conversation_markdown(
                result, {"id": "case", "name": "Case"}, directory
            )
            with open(path, "r", encoding="utf-8") as artifact:
                markdown = artifact.read()
        self.assertIn("Completion reason: error", markdown)
        self.assertIn("Exact customer message", markdown)
        self.assertIn("connection lost", markdown)

    @patch(
        "tests.run_benchmark.generate_conversation_markdown",
        side_effect=OSError("disk unavailable"),
    )
    @patch("tests.run_benchmark.generate_fix_prompt", return_value="fix.md")
    @patch("tests.run_benchmark.generate_evaluation_markdown", return_value="evaluation.md")
    @patch("tests.run_benchmark.save_benchmark_run", return_value="benchmark-id")
    @patch("tests.run_benchmark.get_previous_benchmark_run", return_value=None)
    @patch("tests.run_benchmark.evaluate_run")
    @patch("tests.run_benchmark._save_result", return_value="raw.json")
    @patch("tests.run_benchmark.snapshot_test_subject", return_value={"lead": {}, "folio": {}})
    @patch("tests.run_benchmark.reset_test_subject")
    @patch("tests.run_benchmark.ensure_test_subject", return_value={"lead_id": "lead", "folio_id": "folio"})
    @patch("tests.run_benchmark.get_bubble_base")
    @patch("tests.run_benchmark.run_turn")
    def test_artifact_failure_does_not_fail_benchmark(
        self, run_turn, _base, _subject, _reset, _snapshot, _save,
        evaluate, _previous, _persist, _markdown, _prompt, _artifact,
    ):
        run_turn.return_value = {
            "text": "Hello", "response_id": "response-1",
            "previous_response_id_sent": None, "statuses": [],
            "citations": [], "errors": [],
            "timing": {"first_event_s": 1, "first_delta_s": 1, "total_s": 2},
            "done_seen": True,
        }
        evaluate.return_value = (
            "evaluation.json",
            {
                "overall_status": "pass", "issues": [], "metrics": {},
                "comparison_to_previous_run": {"available": False},
            },
        )
        case = {
            "id": "scripted", "name": "Scripted",
            "initial_ai_searchtext": "none",
            "turns": [{"message": "Hi"}],
        }
        with patch("builtins.open", mock_open(read_data="artifact")):
            result = run_benchmark.run_case(case, run_id="run-id")

        self.assertIsNone(result["failure"])
        self.assertIsNone(result["execution"]["conversation_path"])
        self.assertEqual(
            result["execution"]["conversation_artifact_error"],
            "disk unavailable",
        )
        evaluate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
