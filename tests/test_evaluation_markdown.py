import json
import os
import tempfile
import unittest
from unittest.mock import patch

from .generate_evaluation_markdown import (
    _speed_label,
    generate_evaluation_markdown,
)


class EvaluationMarkdownTests(unittest.TestCase):
    def _write(self, directory, result, evaluation):
        result_path = os.path.join(directory, "case_01_20260815_184000.json")
        evaluation_path = result_path[:-5] + "_evaluation.json"
        with open(result_path, "w", encoding="utf-8") as output:
            json.dump(result, output)
        with open(evaluation_path, "w", encoding="utf-8") as output:
            json.dump(evaluation, output)
        return result_path, evaluation_path

    def _normal_result(self):
        long_response = "A detailed recommendation. " * 300
        return {
            "case_id": "case_01",
            "case_name": "Sofia — school bus, cats and teenage bedroom",
            "started_at_utc": "2026-08-15T18:40:00Z",
            "failure": None,
            "turns": [
                {
                    "turn": 1,
                    "tenant_message": "Tenant’s exact message — keep this verbatim.",
                    "rentee_response": "Rentee’s exact response — unchanged.",
                    "errors": [],
                    "timing": {"first_event_s": 2.2, "first_delta_s": 8.2, "total_s": 11.0}
                },
                {
                    "turn": 2,
                    "tenant_message": "Recommend something now.",
                    "rentee_response": long_response,
                    "errors": [],
                    "timing": {"first_event_s": 3.0, "first_delta_s": 24.3, "total_s": 29.1}
                }
            ]
        }, long_response

    def _failing_evaluation(self):
        issues = []
        for index, severity in enumerate(("critical", "high", "medium", "low"), 1):
            issues.append({
                "id": f"issue_{index}", "category": "conversation",
                "severity": severity, "turns": [1] if index == 1 else [2],
                "evidence": [f"Evidence {index}A", f"Evidence {index}B", f"Evidence {index}C", f"Evidence {index}D"],
                "diagnosis": f"Plain diagnosis {index}.",
                "recommended_fix": f"Priority direction {index}."
            })
        return {
            "overall_status": "fail",
            "summary": "Four issue groups detected.",
            "metrics": {"average_first_delta_s": 16.25, "average_total_s": 20.05},
            "issues": issues,
            "passes": ["No unsupported action promises were detected."],
            "qualitative_evaluation": {
                "scores": {
                    "conversation_intelligence": 2,
                    "recommendation_reasoning": 1,
                    "adaptiveness": 2,
                    "question_quality": 1,
                    "decision_progress": 2
                },
                "strengths": ["Remembered the tenant's cats."],
                "highest_priority_improvement": "Ask fewer questions."
            },
            "comparison_to_previous_run": {
                "available": True,
                "first_delta_change_pct": -50.0,
                "total_latency_change_pct": 25.0,
                "slow_turns": {"previous": 2, "current": 1},
                "unsupported_actions": {"previous": 4, "current": 0},
                "excessive_questioning": {"previous": 1, "current": 2},
                "repeated_questions": {"previous": 2, "current": 2},
                "preference_persistence_failures": {"previous": 0, "current": 0}
            }
        }

    def test_normal_failing_report_has_full_human_review_content(self):
        result, long_response = self._normal_result()
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write(directory, result, self._failing_evaluation())
            markdown_path = generate_evaluation_markdown(*paths)
            with open(markdown_path, "r", encoding="utf-8") as source:
                report = source.read()
        self.assertIn("**Result:** FAIL", report)
        self.assertIn("| 1 | 8.2s | 11.0s | Acceptable |", report)
        self.assertIn("| 2 | 24.3s | 29.1s | Poor |", report)
        self.assertIn("Average first text", report)
        self.assertIn("50.0% faster", report)
        self.assertIn("25.0% slower", report)
        self.assertIn("No unsupported action promises", report)
        self.assertIn("Remembered the tenant's cats", report)
        self.assertIn("| Conversation Intelligence | 2/3 |", report)
        self.assertEqual(report.count("Priority direction "), 7)  # four issue fixes + top three
        self.assertIn("**Issues detected:** Issue 1", report)
        self.assertIn("Tenant’s exact message — keep this verbatim.", report)
        self.assertIn("Rentee’s exact response — unchanged.", report)
        self.assertIn(long_response, report)
        self.assertIn("First event 2.2s · First text 8.2s · Total 11.0s", report)

    def test_passing_report_and_no_previous_run(self):
        result, _ = self._normal_result()
        evaluation = {
            "overall_status": "pass", "summary": "No issues.",
            "metrics": {"average_first_delta_s": 4.0, "average_total_s": 5.0},
            "issues": [], "passes": ["All checks passed."],
            "qualitative_evaluation": {"scores": {
                "conversation_intelligence": 3, "recommendation_reasoning": 3,
                "adaptiveness": 3, "question_quality": 3, "decision_progress": 3
            }, "strengths": []},
            "comparison_to_previous_run": {"available": False}
        }
        with tempfile.TemporaryDirectory() as directory:
            paths = self._write(directory, result, evaluation)
            markdown_path = generate_evaluation_markdown(*paths)
            with open(markdown_path, encoding="utf-8") as source:
                report = source.read()
        self.assertIn("**Result:** PASS", report)
        self.assertIn("No previous run available for comparison.", report)
        self.assertIn("All checks passed.", report)
        self.assertIn("No problems were detected.", report)

    def test_partial_failure_and_qualitative_failure_are_clear_and_secret_safe(self):
        secret = "super-secret-benchmark-token"
        result = {
            "case_id": "case_01", "case_name": "Partial", "failure": {
                "turn": 1, "message": "chain failed"
            },
            "turns": [{
                "turn": 1, "tenant_message": "Exact failed request",
                "rentee_response": "", "errors": [f"Authorization: Bearer {secret}"],
                "timing": {"first_event_s": 3.1, "first_delta_s": None, "total_s": 8.4}
            }]
        }
        evaluation = {
            "overall_status": "fail", "summary": "Infrastructure failure.",
            "metrics": {"average_first_delta_s": None, "average_total_s": 8.4},
            "issues": [], "passes": [],
            "qualitative_evaluation": {"error": f"Evaluator failed with {secret}"},
            "comparison_to_previous_run": {"available": False}
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"BENCHMARK_API_KEY": secret}, clear=True
        ):
            paths = self._write(directory, result, evaluation)
            markdown_path = generate_evaluation_markdown(*paths)
            with open(markdown_path, encoding="utf-8") as source:
                report = source.read()
        self.assertIn("FAIL — infrastructure failure", report)
        self.assertIn("benchmark stopped on Turn 1", report)
        self.assertIn("⚠️ **Response failed**", report)
        self.assertIn("Qualitative evaluation unavailable", report)
        self.assertIn("[REDACTED]", report)
        self.assertNotIn(secret, report)

    def test_speed_threshold_labels(self):
        self.assertEqual(_speed_label(5), "Good")
        self.assertEqual(_speed_label(5.1), "Acceptable")
        self.assertEqual(_speed_label(10), "Acceptable")
        self.assertEqual(_speed_label(10.1), "Slow")
        self.assertEqual(_speed_label(20), "Slow")
        self.assertEqual(_speed_label(20.1), "Poor")


if __name__ == "__main__":
    unittest.main()
