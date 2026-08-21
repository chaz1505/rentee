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


class CapturingThread:
    instances = []

    def __init__(self, target, args, name, daemon):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self.started = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True


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

    @patch("tests.human_review.requests.get")
    def test_active_codex_is_rejected_but_failed_can_retry(self, mocked_get):
        for status in ("working", "completed"):
            mocked_get.return_value = response({"response": self.record(
                codexStatus=status, codexTaskID="task-id"
            )})
            with self.assertRaises(human_review.CodexAlreadyActive) as caught:
                human_review.update_fix_prompt_with_human_review(
                    "id", "development"
                )
            self.assertEqual(caught.exception.codex_status, status)
            self.assertEqual(caught.exception.codex_task_id, "task-id")
        with patch("tests.human_review.requests.patch", return_value=response(status=204)):
            mocked_get.return_value = response({"response": self.record(
                codexStatus="failed"
            )})
            result = human_review.update_fix_prompt_with_human_review(
                "id", "development"
            )
        self.assertTrue(result["fix_prompt_updated"])

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
        CapturingThread.instances.clear()
        with app_module._codex_task_metadata_lock:
            app_module._codex_task_metadata.clear()

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

    @patch("app.threading.Thread", CapturingThread)
    @patch("automation.codex_client.create_codex_task_id", return_value="codex-task-1")
    @patch("tests.human_review.patch_codex_state")
    @patch("tests.human_review.update_fix_prompt_with_human_review")
    def test_success_response_and_default_development(
        self, mocked_update, mocked_state, _mocked_task_id
    ):
        mocked_update.return_value = {
            "benchmark_run_id": "bubble-id", "run_id": "run-id",
            "environment": "development", "fix_prompt_updated": True,
            "updated_fix_prompt": "automatic prompt\n<!-- HUMAN_REVIEW_START -->human review<!-- HUMAN_REVIEW_END -->",
        }
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            result = self.client.post(
                "/admin/benchmark/bubble-id/fix",
                headers={"X-Benchmark-Key": "right"},
            )
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.get_json()["status"], "working")
        self.assertTrue(result.get_json()["codex_submitted"])
        self.assertEqual(result.get_json()["codex_task_id"], "codex-task-1")
        mocked_update.assert_called_once_with("bubble-id", "development")
        initial_state = mocked_state.call_args.args[2]
        self.assertTrue(initial_state["codexSubmitted"])
        self.assertEqual(initial_state["codexStatus"], "working")
        self.assertEqual(initial_state["codexTaskID"], "codex-task-1")
        self.assertRegex(initial_state["codexSubmittedAt"], r"Z$")
        self.assertEqual(len(CapturingThread.instances), 1)
        self.assertTrue(CapturingThread.instances[0].started)
        background_args = CapturingThread.instances[0].args
        self.assertIn("automatic prompt", background_args[0])
        self.assertIn("human review", background_args[0])
        self.assertEqual(background_args[-1], "codex-task-1")

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

    @patch("tests.human_review.patch_codex_state")
    @patch("automation.codex_client.submit_codex_fix")
    def test_background_codex_failure_marks_failed(
        self, mocked_codex, mocked_state
    ):
        from automation.codex_client import CodexSubmissionError
        mocked_codex.side_effect = CodexSubmissionError("safe failure")
        app_module._run_codex_fix_background(
            "updated prompt with human review", "run-id", "bubble-id",
            "development", "task-id",
        )
        mocked_codex.assert_called_once_with(
            "updated prompt with human review", "run-id", "bubble-id",
            "development", task_id="task-id",
        )
        self.assertEqual(
            mocked_state.call_args_list[-1].args[2],
            {
                "codexSubmitted": False, "codexStatus": "failed",
                "codexTaskID": "task-id",
            },
        )

    @patch("tests.human_review.patch_codex_state")
    @patch("automation.codex_client.submit_codex_fix")
    def test_background_success_marks_completed(
        self, mocked_codex, mocked_state
    ):
        mocked_codex.return_value = {
            "task_id": "task-id", "status": "completed",
            "changes_detected": True, "changed_files": ["app.py"],
        }
        app_module._run_codex_fix_background(
            "prompt", "run-id", "bubble-id", "live", "task-id"
        )
        self.assertEqual(mocked_state.call_args.args[2], {
            "codexSubmitted": True,
            "codexStatus": "completed",
            "codexTaskID": "task-id",
        })
        with app_module._codex_task_metadata_lock:
            self.assertEqual(
                app_module._codex_task_metadata["task-id"]["changed_files"],
                ["app.py"],
            )

    @patch("tests.human_review.update_fix_prompt_with_human_review")
    def test_duplicate_submission_returns_409(self, mocked_update):
        mocked_update.side_effect = human_review.CodexAlreadyActive(
            "submitted", "task-id"
        )
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            result = self.client.post(
                "/admin/benchmark/bubble-id/fix",
                headers={"X-Benchmark-Key": "right"},
            )
        self.assertEqual(result.status_code, 409)
        self.assertEqual(result.get_json()["codex_status"], "submitted")
        self.assertEqual(result.get_json()["codex_task_id"], "task-id")

    def test_status_endpoint_requires_auth(self):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            result = self.client.get("/admin/benchmark/id/fix_status")
        self.assertEqual(result.status_code, 401)

    @patch("tests.human_review.get_benchmark_run_codex_status")
    def test_status_endpoint_returns_bubble_state(self, mocked_status):
        mocked_status.return_value = {
            "benchmark_run_id": "id", "codex_status": "submitted",
            "codex_submitted": True, "codex_task_id": "task-id",
        }
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "right"}, clear=False):
            result = self.client.get(
                "/admin/benchmark/id/fix_status?environment=live",
                headers={"X-Benchmark-Key": "right"},
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["codex_task_id"], "task-id")
        mocked_status.assert_called_once_with("id", "live")


if __name__ == "__main__":
    unittest.main()
