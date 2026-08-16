import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module
from . import human_review


def response(body=None, status=200):
    result = Mock(ok=200 <= status < 300, status_code=status)
    if body is None:
        result.content = b""
        result.text = ""
    else:
        result.content = json.dumps(body).encode()
        result.text = json.dumps(body)
        result.json.return_value = body
    return result


class HumanReviewHelperTests(unittest.TestCase):
    def record(self, **updates):
        record = {
            "runID": "canonical-run-id",
            "environment": "development",
            "humanReviewed": True,
            "humanScore": 2,
            "humanConversationScore": 3,
            "humanRecommendationScore": 1,
            "humanAccuracyScore": 2,
            "humanFeedback": "Recommended too early.",
            "humanInstruction": "Ask for missing requirements first.",
            "humanReviewedAt": "2026-08-16T01:00:00Z",
            "fixPrompt": "# Existing automatic prompt\n\nKeep this content.",
        }
        record.update(updates)
        return record

    @patch("tests.human_review.requests.patch")
    @patch("tests.human_review.requests.get")
    def test_development_inserts_all_review_fields_and_patches_only_prompt(
        self, mocked_get, mocked_patch
    ):
        mocked_get.return_value = response({"response": self.record()})
        mocked_patch.return_value = response(status=204)
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "secret"}, clear=False):
            result = human_review.update_fix_prompt_with_human_review(
                "bubble-id", "development"
            )
        expected_url = "https://www.rentee.asia/version-test/api/1.1/obj/benchmarkRun/bubble-id"
        self.assertEqual(mocked_get.call_args.args[0], expected_url)
        self.assertEqual(mocked_patch.call_args.args[0], expected_url)
        self.assertEqual(set(mocked_patch.call_args.kwargs["json"]), {"fixPrompt"})
        prompt = mocked_patch.call_args.kwargs["json"]["fixPrompt"]
        self.assertIn("Recommended too early.", prompt)
        self.assertIn("Ask for missing requirements first.", prompt)
        self.assertIn("- Overall: 2/5", prompt)
        self.assertIn("- Conversation quality: 3/5", prompt)
        self.assertIn("- Recommendation quality: 1/5", prompt)
        self.assertIn("- Accuracy / agent judgement: 2/5", prompt)
        self.assertEqual(result["run_id"], "canonical-run-id")

    @patch("tests.human_review.requests.patch", return_value=response(status=204))
    @patch("tests.human_review.requests.get")
    def test_live_uses_live_endpoint(self, mocked_get, _mocked_patch):
        mocked_get.return_value = response({"response": self.record(environment="live")})
        human_review.update_fix_prompt_with_human_review("bubble-id", "live")
        self.assertEqual(
            mocked_get.call_args.args[0],
            "https://www.rentee.asia/api/1.1/obj/benchmarkRun/bubble-id",
        )

    def test_missing_scores_are_omitted(self):
        block = human_review.build_human_review_block(self.record(
            humanScore=None, humanRecommendationScore="",
            humanAccuracyScore=None,
        ))
        self.assertNotIn("Overall:", block)
        self.assertIn("Conversation quality: 3/5", block)
        self.assertNotIn("Recommendation quality:", block)
        self.assertNotIn("Accuracy / agent judgement:", block)

    def test_second_merge_replaces_review_block(self):
        first = human_review.merge_human_review("automatic", "<!-- HUMAN_REVIEW_START -->old<!-- HUMAN_REVIEW_END -->")
        second = human_review.merge_human_review(first, "<!-- HUMAN_REVIEW_START -->new<!-- HUMAN_REVIEW_END -->")
        self.assertNotIn("old", second)
        self.assertIn("new", second)
        self.assertEqual(second.count(human_review.HUMAN_REVIEW_START), 1)

    @patch("tests.human_review.requests.get", return_value=response({"error": "missing"}, 404))
    def test_missing_record_returns_not_found(self, _mocked_get):
        with self.assertRaises(human_review.BenchmarkRunNotFound):
            human_review.update_fix_prompt_with_human_review("missing", "development")

    def _assert_validation(self, expected, **updates):
        with patch("tests.human_review.requests.get", return_value=response({"response": self.record(**updates)})):
            with self.assertRaisesRegex(human_review.BenchmarkReviewValidationError, expected):
                human_review.update_fix_prompt_with_human_review("id", "development")

    def test_environment_mismatch_fails(self):
        self._assert_validation("environment does not match", environment="live")

    def test_unreviewed_record_fails(self):
        self._assert_validation("has not been human reviewed", humanReviewed=False)

    def test_no_review_content_fails(self):
        self._assert_validation(
            "contains no human review feedback", humanScore=None,
            humanConversationScore=None, humanRecommendationScore=None,
            humanAccuracyScore=None, humanFeedback="", humanInstruction="",
        )

    def test_missing_fix_prompt_fails(self):
        self._assert_validation("has no generated fix prompt", fixPrompt="")

    @patch("tests.human_review.requests.patch")
    @patch("tests.human_review.requests.get")
    def test_bubble_failure_is_safe_and_redacts_secrets(self, mocked_get, mocked_patch):
        mocked_get.return_value = response({"response": self.record()})
        mocked_patch.return_value = response({"error": "token secret-token key admin-secret"}, 500)
        with patch.dict(os.environ, {
            "BUBBLE_API_TOKEN": "secret-token", "BENCHMARK_API_KEY": "admin-secret"
        }, clear=False):
            with self.assertRaises(human_review.BenchmarkReviewError) as caught:
                human_review.update_fix_prompt_with_human_review("id", "development")
        message = str(caught.exception)
        self.assertNotIn("secret-token", message)
        self.assertNotIn("admin-secret", message)


class HumanReviewEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_auth_is_required_before_processing(self):
        with patch.dict(os.environ, {}, clear=True):
            missing = self.client.post("/admin/benchmark/id/fix")
        self.assertEqual(missing.status_code, 503)
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=True):
            wrong = self.client.post(
                "/admin/benchmark/id/fix", headers={"X-Benchmark-Key": "wrong"}
            )
        self.assertEqual(wrong.status_code, 401)

    def test_invalid_environment_returns_400(self):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            result = self.client.post(
                "/admin/benchmark/id/fix",
                headers={"X-Benchmark-Key": "right"},
                json={"environment": "staging"},
            )
        self.assertEqual(result.status_code, 400)

    @patch("tests.human_review.update_fix_prompt_with_human_review")
    def test_success_response_and_default_development(self, mocked_update):
        mocked_update.return_value = {
            "benchmark_run_id": "bubble-id", "run_id": "run-id",
            "environment": "development", "fix_prompt_updated": True,
        }
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            result = self.client.post(
                "/admin/benchmark/bubble-id/fix",
                headers={"X-Benchmark-Key": "right"},
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["status"], "ready")
        mocked_update.assert_called_once_with("bubble-id", "development")

    @patch("tests.human_review.update_fix_prompt_with_human_review")
    def test_not_found_and_bubble_failure_use_safe_statuses(self, mocked_update):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            mocked_update.side_effect = human_review.BenchmarkRunNotFound(
                "BenchmarkRun not found."
            )
            missing = self.client.post(
                "/admin/benchmark/missing/fix",
                headers={"X-Benchmark-Key": "right"},
            )
            mocked_update.side_effect = human_review.BenchmarkReviewError(
                "Bubble BenchmarkRun read failed: HTTP 500: unavailable"
            )
            failed = self.client.post(
                "/admin/benchmark/id/fix",
                headers={"X-Benchmark-Key": "right"},
            )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json(), {"error": "BenchmarkRun not found."})
        self.assertEqual(failed.status_code, 502)

    def test_secrets_do_not_appear_in_logs(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "admin-secret"}, clear=False):
            with redirect_stdout(output):
                result = self.client.post(
                    "/admin/benchmark/id/fix",
                    headers={"X-Benchmark-Key": "wrong-secret"},
                )
        self.assertEqual(result.status_code, 401)
        self.assertNotIn("admin-secret", output.getvalue())
        self.assertNotIn("wrong-secret", output.getvalue())


if __name__ == "__main__":
    unittest.main()
