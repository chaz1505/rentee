import os
import unittest
from unittest.mock import Mock, patch

from automation import codex_client


class CodexClientTests(unittest.TestCase):
    def test_wrapper_targets_correct_repo_and_forbids_main_push(self):
        prompt = codex_client.build_codex_prompt("EXACT FIX PROMPT")
        self.assertIn("Repository: chaz1505/rentee", prompt)
        self.assertIn("Base branch: main", prompt)
        self.assertIn("Do not push directly to main.", prompt)
        self.assertIn("EXACT FIX PROMPT", prompt)

    @patch("automation.codex_client._ensure_authenticated")
    @patch("automation.codex_client._run")
    @patch("automation.codex_client.shutil.which", return_value="/usr/bin/codex")
    def test_cloud_submission_returns_native_task_id(
        self, _mocked_which, mocked_run, _mocked_auth
    ):
        mocked_run.return_value = Mock(
            returncode=0,
            stdout="Task submitted: https://chatgpt.com/codex/tasks/task_native123",
            stderr="",
        )
        with patch.dict(os.environ, {"CODEX_CLOUD_ENV_ID": "env_rentee"}, clear=False):
            result = codex_client.submit_codex_fix(
                "fix prompt", "run-id", "bubble-id", "live"
            )
        self.assertEqual(result["task_id"], "task_native123")
        self.assertEqual(result["task_id_source"], "codex_cloud")
        command = mocked_run.call_args.args[0]
        self.assertEqual(command[:5], [
            "/usr/bin/codex", "cloud", "exec", "--env", "env_rentee"
        ])
        self.assertEqual(command[5:7], ["--branch", "main"])
        self.assertIn("fix prompt", command[7])

    @patch("automation.codex_client._ensure_authenticated")
    @patch("automation.codex_client._run")
    @patch("automation.codex_client.shutil.which", return_value="/usr/bin/codex")
    def test_missing_native_id_is_clearly_rentee_generated(
        self, _mocked_which, mocked_run, _mocked_auth
    ):
        mocked_run.return_value = Mock(returncode=0, stdout="Submitted", stderr="")
        with patch.dict(os.environ, {"CODEX_CLOUD_ENV_ID": "env"}, clear=False):
            result = codex_client.submit_codex_fix(
                "prompt", "run-id", "bubble-id", "development"
            )
        self.assertTrue(result["task_id"].startswith("codex_run-id_"))
        self.assertEqual(result["task_id_source"], "rentee_generated")

    @patch("automation.codex_client.shutil.which", return_value=None)
    def test_missing_cli_fails_safely(self, _mocked_which):
        with self.assertRaisesRegex(codex_client.CodexSubmissionError, "not installed"):
            codex_client.submit_codex_fix("prompt", "run", "bubble", "live")

    def test_configured_secrets_are_rejected(self):
        with patch.dict(os.environ, {"BUBBLE_API_TOKEN": "never-submit-me"}, clear=False):
            with self.assertRaisesRegex(codex_client.CodexSubmissionError, "secret material"):
                codex_client._reject_configured_secrets(
                    "prompt containing never-submit-me"
                )


if __name__ == "__main__":
    unittest.main()
