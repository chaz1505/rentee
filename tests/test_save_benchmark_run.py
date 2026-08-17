import json
import os
import unittest
from unittest.mock import Mock, patch

from . import save_benchmark_run as persistence


class BenchmarkRunPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.result = {
            "run_id": "20260815_183700_sofia_01_live",
            "case_id": "sofia_01",
            "case_name": "Sofia - school bus, cats and teenage bedroom",
            "started_at_utc": "2026-08-15T10:37:00Z",
            "completed_at_utc": "2026-08-15T10:38:00+00:00",
            "turns": [
                {"tenant_message": "hello", "rentee_response": "hi", "timing": {"first_delta_s": 1.2, "total_s": 2.3}},
                {"tenant_message": "more", "rentee_response": "answer", "timing": {"first_delta_s": 4.5, "total_s": 5.6}},
            ],
            "failure": None,
        }
        self.evaluation = {
            "overall_status": "fail",
            "summary": "Three issues",
            "metrics": {"average_first_delta_s": 2.85, "average_total_s": 3.95},
            "issues": [
                {"severity": "HIGH", "evidence": ["a", "b"]},
                {"severity": "critical", "evidence": ["c"]},
                {"severity": "medium", "evidence": []},
            ],
            "qualitative_evaluation": {"scores": {
                "conversation_intelligence": 0,
                "recommendation_reasoning": 1,
                "adaptiveness": 2,
                "question_quality": 3,
            }},
            "comparison_to_previous_run": {"previous_run_id": "prior-canonical-id"},
        }

    def test_exact_payload_mapping_and_complete_artifacts(self):
        conversation_markdown = (
            "# Rentee Benchmark Conversation\n\n"
            "CUSTOMER:\nhello\n\nRENTEE:\nhi\n"
        )
        payload = persistence.build_benchmark_run_payload(
            self.result, self.evaluation,
            "# Evaluation\n\nTenant: hello\nRentee: hi", "complete fix prompt", "live",
            conversation_markdown=conversation_markdown,
        )
        self.assertEqual(set(payload), persistence.BENCHMARK_RUN_FIELDS - {"decisionProgress"})
        self.assertNotIn("converationIntelligence", payload)
        self.assertEqual(payload["conversationIntelligence"], 0)
        self.assertEqual(payload["runID"], self.result["run_id"])
        self.assertEqual(payload["previousRunID"], "prior-canonical-id")
        self.assertEqual(payload["averageFirstText"], 2.85)
        self.assertEqual(payload["averageTotal"], 3.95)
        self.assertEqual(payload["maxFirstText"], 4.5)
        self.assertEqual(payload["criticalIssueCount"], 2)
        self.assertEqual(payload["totalTurns"], 2)
        self.assertEqual(json.loads(payload["rawResultJSON"]), self.result)
        self.assertEqual(json.loads(payload["evaluationJSON"]), self.evaluation)
        self.assertIn("Tenant: hello", payload["evaluationMarkdown"])
        self.assertEqual(payload["conversationMarkdown"], conversation_markdown)
        self.assertEqual(payload["fixPrompt"], "complete fix prompt")
        self.assertRegex(payload["startedAt"], r"Z$")
        self.assertRegex(payload["completedAt"], r"Z$")

    def test_missing_metrics_and_scores_are_omitted_not_zeroed(self):
        evaluation = {"overall_status": "pass", "metrics": {}, "issues": []}
        result = dict(self.result, turns=[])
        payload = persistence.build_benchmark_run_payload(result, evaluation, "md", "prompt", "development")
        for field in ("averageFirstText", "averageTotal", "maxFirstText", "conversationIntelligence"):
            self.assertNotIn(field, payload)
        self.assertEqual(payload["totalTurns"], 0)

    @patch("tests.save_benchmark_run.requests.post")
    def test_development_and_live_use_exact_distinct_endpoints(self, mocked_post):
        response = Mock(ok=True, content=b'{}')
        response.json.side_effect = [
            {"_id": "dev-id"}, {"response": {"_id": "live-id"}}
        ]
        mocked_post.return_value = response
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "secret"}, clear=False):
            self.assertEqual(persistence.save_benchmark_run(self.result, self.evaluation, "md", "prompt", "development"), "dev-id")
            self.assertEqual(persistence.save_benchmark_run(self.result, self.evaluation, "md", "prompt", "live"), "live-id")
        urls = [call.args[0] for call in mocked_post.call_args_list]
        self.assertEqual(urls, [
            "https://www.rentee.asia/version-test/api/1.1/obj/benchmarkRun",
            "https://www.rentee.asia/api/1.1/obj/benchmarkRun",
        ])
        for call in mocked_post.call_args_list:
            self.assertNotIn("secret", json.dumps(call.kwargs["json"]))

    @patch("tests.save_benchmark_run.requests.post")
    def test_failed_and_infrastructure_error_runs_are_persisted(self, mocked_post):
        response = Mock(ok=True, content=b'{}')
        response.json.side_effect = [{"id": "failed-id"}, {"id": "error-id"}]
        mocked_post.return_value = response
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "secret"}, clear=False):
            persistence.save_benchmark_run(self.result, self.evaluation, "md", "prompt", "development")
            error_result = dict(self.result, infrastructure_error="setup failed")
            persistence.save_benchmark_run(error_result, self.evaluation, "md", "prompt", "development")
        self.assertEqual(mocked_post.call_args_list[0].kwargs["json"]["status"], "fail")
        self.assertEqual(mocked_post.call_args_list[1].kwargs["json"]["status"], "error")

    @patch("tests.save_benchmark_run.requests.post")
    def test_complete_synthetic_and_partial_markdown_are_sent_unchanged(
        self, mocked_post
    ):
        response = Mock(ok=True, content=b'{}')
        response.json.side_effect = [{"id": "synthetic-id"}, {"id": "partial-id"}]
        mocked_post.return_value = response
        synthetic_markdown = (
            "# Rentee Benchmark Conversation\n\n"
            "## Synthetic Customer Ground Truth\n\n"
            "Wanted to view:\n- Alpha Residence\n- Beta Heights\n"
        )
        partial_markdown = (
            "# Rentee Benchmark Conversation\n\n"
            "CUSTOMER:\nExact partial message\n\n"
            "Errors:\n- SSE failure\n"
        )
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "secret"}, clear=False):
            persistence.save_benchmark_run(
                self.result, self.evaluation, "md", "prompt", "development",
                conversation_markdown=synthetic_markdown,
            )
            persistence.save_benchmark_run(
                dict(self.result, failure={"turn": 1}),
                self.evaluation, "md", "prompt", "development",
                conversation_markdown=partial_markdown,
            )

        self.assertEqual(
            mocked_post.call_args_list[0].kwargs["json"]["conversationMarkdown"],
            synthetic_markdown,
        )
        self.assertEqual(
            mocked_post.call_args_list[1].kwargs["json"]["conversationMarkdown"],
            partial_markdown,
        )

    @patch("tests.save_benchmark_run.requests.get")
    def test_previous_lookup_is_environment_specific_and_returns_latest(self, mocked_get):
        response = Mock(ok=True, content=b'{}')
        response.json.return_value = {"response": {"results": [{"runID": "prior-id"}]}}
        mocked_get.return_value = response
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "secret"}, clear=False):
            prior = persistence.get_previous_benchmark_run("sofia_01", "live", "current-id")
        self.assertEqual(prior["runID"], "prior-id")
        call = mocked_get.call_args
        self.assertEqual(call.args[0], "https://www.rentee.asia/api/1.1/obj/benchmarkRun")
        constraints = json.loads(call.kwargs["params"]["constraints"])
        self.assertIn({"key": "environment", "constraint_type": "equals", "value": "live"}, constraints)
        self.assertEqual(call.kwargs["params"]["sort_field"], "completedAt")

    @patch("tests.save_benchmark_run.requests.post")
    def test_persistence_error_is_safe(self, mocked_post):
        response = Mock(ok=False, status_code=400, content=b'error', text="bad payload")
        response.json.side_effect = ValueError
        mocked_post.return_value = response
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "super-secret"}, clear=False):
            with self.assertRaisesRegex(persistence.BenchmarkPersistenceError, "HTTP 400") as caught:
                persistence.save_benchmark_run(self.result, self.evaluation, "md", "prompt", "development")
        self.assertNotIn("super-secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
