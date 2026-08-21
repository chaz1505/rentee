import base64
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from . import publish_results


def github_response(status_code, body=None, text=""):
    response = Mock(status_code=status_code, text=text)
    if body is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    return response


class PublishResultsTests(unittest.TestCase):
    def _artifacts(self, root, contents=None):
        result_dir = os.path.join(root, "tests", "results")
        os.makedirs(result_dir)
        paths = [
            os.path.join(result_dir, "sofia_01_20260815_095746.json"),
            os.path.join(result_dir, "sofia_01_20260815_095746_evaluation.json"),
            os.path.join(result_dir, "sofia_01_20260815_095746_evaluation.md"),
            os.path.join(result_dir, "sofia_01_20260815_095746_fix_prompt.md")
        ]
        raw = {
            "case_id": "sofia_01",
            "started_at_utc": "2026-08-15T09:57:46Z"
        }
        values = contents or [json.dumps(raw), "evaluation", "human evaluation", "fix prompt"]
        for path, value in zip(paths, values):
            with open(path, "w", encoding="utf-8") as output:
                output.write(value)
        return paths

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_missing_token_skips_without_http(self, mocked_get, mocked_put):
        with patch.dict(os.environ, {}, clear=True):
            result = publish_results.publish_benchmark_results(["unused"])
        self.assertEqual(result["status"], "skipped")
        mocked_get.assert_not_called()
        mocked_put.assert_not_called()

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_explicit_skip_disables_publishing(self, mocked_get, mocked_put):
        with patch.dict(os.environ, {
            "GITHUB_RESULTS_TOKEN": "token",
            "BENCHMARK_SKIP_GITHUB": " true "
        }, clear=True):
            result = publish_results.publish_benchmark_results(["unused"])
        self.assertEqual(result["status"], "skipped")
        mocked_get.assert_not_called()
        mocked_put.assert_not_called()

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_publishes_exact_four_paths_content_repo_branch_and_auth(
        self, mocked_get, mocked_put
    ):
        with tempfile.TemporaryDirectory() as root:
            paths = self._artifacts(root)
            encoded_contents = []
            for path in paths:
                with open(path, "rb") as source:
                    encoded_contents.append(base64.b64encode(source.read()).decode("ascii"))
            mocked_get.return_value = github_response(404, {"message": "Not Found"})
            mocked_put.return_value = github_response(201, {"content": {}})
            with patch.dict(os.environ, {"GITHUB_RESULTS_TOKEN": "secret-token"}, clear=True):
                result = publish_results.publish_benchmark_results(paths)

        self.assertEqual(result["status"], "published")
        self.assertEqual(len(mocked_get.call_args_list), 4)
        self.assertEqual(len(mocked_put.call_args_list), 4)
        expected_paths = [
            "tests/results/sofia_01_20260815_095746.json",
            "tests/results/sofia_01_20260815_095746_evaluation.json",
            "tests/results/sofia_01_20260815_095746_evaluation.md",
            "tests/results/sofia_01_20260815_095746_fix_prompt.md"
        ]
        for index, call in enumerate(mocked_put.call_args_list):
            self.assertIn(
                f"/repos/chaz1505/rentee/contents/{expected_paths[index]}",
                call.args[0]
            )
            self.assertEqual(call.kwargs["json"]["branch"], "main")
            self.assertEqual(
                call.kwargs["headers"]["Authorization"], "Bearer secret-token"
            )
            self.assertEqual(call.kwargs["json"]["content"], encoded_contents[index])
        self.assertNotIn("secret-token", result["message"])

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_existing_file_update_includes_sha(self, mocked_get, mocked_put):
        with tempfile.TemporaryDirectory() as root:
            paths = self._artifacts(root)
            mocked_get.return_value = github_response(200, {"sha": "existing-sha"})
            mocked_put.return_value = github_response(200, {"content": {}})
            with patch.dict(os.environ, {"GITHUB_RESULTS_TOKEN": "token"}, clear=True):
                result = publish_results.publish_benchmark_results(paths)
        self.assertEqual(result["status"], "published")
        for call in mocked_put.call_args_list:
            self.assertEqual(call.kwargs["json"]["sha"], "existing-sha")

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_http_auth_failures_are_safe(self, mocked_get, mocked_put):
        for status in (401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                paths = self._artifacts(root)
                mocked_get.reset_mock()
                mocked_put.reset_mock()
                mocked_get.return_value = github_response(
                    status, {"message": f"permission denied token-{status}"}
                )
                with patch.dict(os.environ, {
                    "GITHUB_RESULTS_TOKEN": f"token-{status}"
                }, clear=True):
                    result = publish_results.publish_benchmark_results(paths)
                self.assertEqual(result["status"], "warning")
                self.assertIn(f"HTTP {status}", result["message"])
                self.assertNotIn(f"token-{status}", result["message"])
                mocked_put.assert_not_called()

    @patch("tests.publish_results.requests.get")
    def test_network_failure_does_not_raise(self, mocked_get):
        with tempfile.TemporaryDirectory() as root:
            paths = self._artifacts(root)
            mocked_get.side_effect = OSError("network unavailable")
            with patch.dict(os.environ, {"GITHUB_RESULTS_TOKEN": "token"}, clear=True):
                result = publish_results.publish_benchmark_results(paths)
        self.assertEqual(result["status"], "warning")
        self.assertIn("network unavailable", result["message"])

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_secret_in_any_artifact_blocks_entire_run(self, mocked_get, mocked_put):
        with tempfile.TemporaryDirectory() as root:
            paths = self._artifacts(
                root, ["raw", "contains bubble-secret", "human", "prompt"]
            )
            with patch.dict(os.environ, {
                "GITHUB_RESULTS_TOKEN": "github-secret",
                "BUBBLE_API_TOKEN": "bubble-secret",
                "OPENAI_API_KEY": "openai-secret"
            }, clear=True):
                result = publish_results.publish_benchmark_results(paths)
        self.assertEqual(result["status"], "warning")
        self.assertIn("SECURITY", result["message"])
        self.assertNotIn("bubble-secret", result["message"])
        mocked_get.assert_not_called()
        mocked_put.assert_not_called()

    @patch("tests.publish_results.requests.put")
    @patch("tests.publish_results.requests.get")
    def test_autotest_state_can_never_be_published(self, mocked_get, mocked_put):
        with tempfile.TemporaryDirectory() as root:
            paths = self._artifacts(root)
            state_path = os.path.join(root, "tests", ".autotest_state.json")
            with open(state_path, "w", encoding="utf-8") as output:
                output.write("{}")
            paths[2] = state_path
            with patch.dict(os.environ, {"GITHUB_RESULTS_TOKEN": "token"}, clear=True):
                result = publish_results.publish_benchmark_results(paths)
        self.assertEqual(result["status"], "warning")
        mocked_get.assert_not_called()
        mocked_put.assert_not_called()


if __name__ == "__main__":
    unittest.main()
