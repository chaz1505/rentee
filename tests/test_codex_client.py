import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from automation import codex_client


def completed(returncode=0, stdout="", stderr=""):
    return Mock(returncode=returncode, stdout=stdout, stderr=stderr)


class CodexClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_root = Path(self.temp_dir.name) / "rentee-codex"
        self.auth_root = Path(self.temp_dir.name) / "rentee-codex-auth"
        self.root_patch = patch.object(
            codex_client, "WORKSPACE_ROOT", self.workspace_root
        )
        self.auth_root_patch = patch.object(
            codex_client, "AUTH_ROOT", self.auth_root
        )
        self.root_patch.start()
        self.auth_root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.auth_root_patch.stop()
        self.temp_dir.cleanup()

    def test_wrapper_preserves_complete_prompt_and_safety_instructions(self):
        prompt = codex_client.build_codex_prompt("EXACT HUMAN-REVIEWED PROMPT")
        self.assertIn("Repository: chaz1505/rentee", prompt)
        self.assertIn("Base branch: main", prompt)
        self.assertIn("Do not push directly to main.", prompt)
        self.assertIn("EXACT HUMAN-REVIEWED PROMPT", prompt)
        self.assertIn("AUTOMATED FIX EXECUTION RULES", prompt)
        self.assertIn("MUST NOT:\n- run tests/run_benchmark.py", prompt)
        self.assertIn("- run any benchmark runner;", prompt)
        self.assertIn("- call /admin/run_benchmark;", prompt)
        self.assertIn("- call /admin/benchmark/*;", prompt)
        self.assertIn("- call /chat_stream;", prompt)
        self.assertIn(
            "send HTTP requests to rentee.asia or rentee-2.onrender.com",
            prompt,
        )
        self.assertIn("After those tests, STOP.", prompt)
        self.assertIn("Do not run a new benchmark to verify your own fix.", prompt)

    def test_codex_child_environment_strips_rentee_admin_credentials(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "openai-key",
            "BENCHMARK_API_KEY": "benchmark-key",
            "BUBBLE_API_TOKEN": "bubble-token",
            "SAFE_RUNTIME_VALUE": "retained",
        }, clear=True):
            child_environment = codex_client._codex_environment(self.auth_root)
            self.assertEqual(os.environ["BENCHMARK_API_KEY"], "benchmark-key")
            self.assertEqual(os.environ["BUBBLE_API_TOKEN"], "bubble-token")
        self.assertNotIn("BENCHMARK_API_KEY", child_environment)
        self.assertNotIn("BUBBLE_API_TOKEN", child_environment)
        self.assertEqual(child_environment["OPENAI_API_KEY"], "openai-key")
        self.assertEqual(child_environment["SAFE_RUNTIME_VALUE"], "retained")

    def _successful_run(
        self, status=" M app.py\n?? tests/test_new.py\n",
        codex_stdout="Codex finished", codex_stderr="",
    ):
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] == "clone":
                workspace = Path(command[-1])
                (workspace / ".git").mkdir(parents=True)
                return completed()
            if command[1:3] == ["rev-parse", "HEAD"]:
                return completed(stdout="abc123def456\n")
            if command[1:3] == ["checkout", "-b"]:
                return completed()
            if command[1:] == ["--version"]:
                return completed(stdout="codex-cli 0.147.0")
            if command[1:] == ["login", "--help"]:
                return completed(stdout="--with-api-key")
            if command[1:] == ["login", "--with-api-key"]:
                return completed(stdout="Login successful")
            if command[1:] == ["login", "status"]:
                return completed(stdout="Logged in using an API key")
            if command[1] == "exec":
                return completed(stdout=codex_stdout, stderr=codex_stderr)
            if command[1:3] == ["status", "--porcelain"]:
                return completed(stdout=status)
            raise AssertionError(f"Unexpected command: {command}")

        return calls, fake_run

    @patch("automation.codex_client.shutil.which")
    def test_local_clone_branch_exec_and_change_detection(self, mocked_which):
        mocked_which.side_effect = lambda name: f"/usr/bin/{name}"
        calls, fake_run = self._successful_run()
        with patch("automation.codex_client._run", side_effect=fake_run), patch.dict(
            os.environ, {
                "OPENAI_API_KEY": "api-key",
                "BENCHMARK_API_KEY": "benchmark-key",
                "BUBBLE_API_TOKEN": "bubble-token",
            }, clear=True
        ):
            result = codex_client.submit_codex_fix(
                "human-reviewed fix", "run/live", "bubble-id", "live",
                task_id="codex_run-live_unique",
            )

        commands = [call[0] for call in calls]
        clone = commands[0]
        self.assertEqual(clone[:6], [
            "/usr/bin/git", "clone", "--depth", "1", "--branch", "main"
        ])
        self.assertEqual(clone[6], "https://github.com/chaz1505/rentee.git")
        self.assertTrue(str(self.workspace_root) in clone[7])
        self.assertFalse(any("cloud" in command for command in commands))
        self.assertFalse(any("push" in command or "pr" in command for command in commands))
        checkout = next(command for command in commands if command[1:3] == ["checkout", "-b"])
        self.assertTrue(checkout[3].startswith("codex/benchmark-run-live-"))
        self.assertNotEqual(checkout[3], "main")
        codex_call = next(call for call in calls if call[0][1] == "exec")
        self.assertEqual(codex_call[0], [
            "/usr/bin/codex", "exec", "--ignore-user-config", "--ephemeral",
            "--dangerously-bypass-approvals-and-sandbox", "-"
        ])
        self.assertNotIn("workspace-write", codex_call[0])
        self.assertEqual(
            Path(codex_call[1]["cwd"]).resolve(),
            Path(result["workspace"]).resolve(),
        )
        self.assertIn("human-reviewed fix", codex_call[1]["input_text"])
        self.assertEqual(codex_call[1]["env"]["OPENAI_API_KEY"], "api-key")
        self.assertNotIn("BENCHMARK_API_KEY", codex_call[1]["env"])
        self.assertNotIn("BUBBLE_API_TOKEN", codex_call[1]["env"])
        self.assertEqual(result["provider"], "codex_local_cli")
        self.assertEqual(result["base_commit"], "abc123def456")
        self.assertEqual(result["changed_files"], ["app.py", "tests/test_new.py"])
        self.assertTrue(result["changes_detected"])
        self.assertTrue(Path(result["workspace"]).exists())

    def test_unsandboxed_execution_requires_workspace_inside_root(self):
        outside = Path(self.temp_dir.name) / "outside"
        (outside / ".git").mkdir(parents=True)
        with self.assertRaisesRegex(
            codex_client.CodexSubmissionError, "outside the disposable"
        ):
            codex_client._verify_disposable_workspace(outside, "task-id")

    def test_unsandboxed_execution_requires_git_repository(self):
        workspace = self.workspace_root / "task-id"
        workspace.mkdir(parents=True)
        with self.assertRaisesRegex(
            codex_client.CodexSubmissionError, "not a cloned Git repository"
        ):
            codex_client._verify_disposable_workspace(workspace, "task-id")

    def test_deployed_render_checkout_is_never_accepted(self):
        with self.assertRaisesRegex(
            codex_client.CodexSubmissionError, "outside the disposable"
        ):
            codex_client._verify_disposable_workspace(
                Path("/opt/render/project/src"), "task-id"
            )

    @patch("automation.codex_client.shutil.which")
    def test_success_output_is_sanitized_and_truncated(self, mocked_which):
        mocked_which.side_effect = lambda name: f"/usr/bin/{name}"
        secret = "test-secret-key"
        prompt = "PRIVATE BENCHMARK PROMPT"
        final_output = f"Completed {secret} {prompt} " + ("x" * 3000)
        _calls, fake_run = self._successful_run(codex_stdout=final_output)
        output = io.StringIO()
        with patch("automation.codex_client._run", side_effect=fake_run), patch.dict(
            os.environ, {"OPENAI_API_KEY": secret}, clear=True
        ), redirect_stdout(output):
            result = codex_client.submit_codex_fix(
                prompt, "run", "bubble", "development",
                task_id="codex_success_output",
            )
        logs = output.getvalue()
        self.assertIn("[CODEX] Final response:", logs)
        self.assertIn("[REDACTED]", logs)
        self.assertIn("…[truncated]", logs)
        self.assertNotIn(secret, logs)
        self.assertNotIn(prompt, logs)
        self.assertTrue(result["changes_detected"])

    @patch("automation.codex_client.shutil.which")
    def test_noop_is_success_without_changes(self, mocked_which):
        mocked_which.side_effect = lambda name: f"/usr/bin/{name}"
        _calls, fake_run = self._successful_run(status="")
        with patch("automation.codex_client._run", side_effect=fake_run), patch.dict(
            os.environ, {"OPENAI_API_KEY": "api-key"}, clear=True
        ):
            result = codex_client.submit_codex_fix(
                "prompt", "run", "bubble", "development", task_id="codex_noop"
            )
        self.assertFalse(result["changes_detected"])
        self.assertEqual(result["changed_files"], [])

    @patch("automation.codex_client.shutil.which")
    def test_codex_failure_surfaces_sanitized_error(self, mocked_which):
        mocked_which.side_effect = lambda name: f"/usr/bin/{name}"
        secret = "super-secret-key"
        prompt = "PRIVATE COMPLETE PROMPT"
        calls, fake_success = self._successful_run()

        def fake_run(command, **kwargs):
            if command[1] == "exec":
                return completed(1, stdout=prompt, stderr=f"bad auth {secret}")
            return fake_success(command, **kwargs)

        output = io.StringIO()
        with patch("automation.codex_client._run", side_effect=fake_run), patch.dict(
            os.environ, {"OPENAI_API_KEY": secret}, clear=True
        ), redirect_stdout(output):
            with self.assertRaises(codex_client.CodexSubmissionError) as caught:
                codex_client.submit_codex_fix(
                    prompt, "run", "bubble", "live", task_id="codex_failure"
                )
        self.assertIn("Local Codex execution failed: bad auth", str(caught.exception))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(prompt, output.getvalue())

    def test_timeout_is_configurable(self):
        with patch.dict(os.environ, {"CODEX_EXEC_TIMEOUT_SECONDS": "123"}, clear=False):
            self.assertEqual(codex_client._execution_timeout(), 123)
        with patch.dict(os.environ, {"CODEX_EXEC_TIMEOUT_SECONDS": "invalid"}, clear=False):
            with self.assertRaises(codex_client.CodexSubmissionError):
                codex_client._execution_timeout()

    def test_cleanup_removes_only_old_inactive_workspaces(self):
        old = self.workspace_root / "old"
        recent = self.workspace_root / "recent"
        active = self.workspace_root / "active"
        for path in (old, recent, active):
            path.mkdir(parents=True)
        now = time.time()
        os.utime(old, (now - codex_client.WORKSPACE_TTL_SECONDS - 10,) * 2)
        os.utime(active, (now - codex_client.WORKSPACE_TTL_SECONDS - 10,) * 2)
        removed = codex_client.cleanup_old_workspaces("active", now=now)
        self.assertEqual(removed, ["old"])
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(active.exists())

    def test_cloud_environment_is_not_required(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "api-key"}, clear=True):
            first_task_id = codex_client.create_codex_task_id("run")
            second_task_id = codex_client.create_codex_task_id("run")
        self.assertTrue(first_task_id.startswith("codex_run_"))
        self.assertNotEqual(first_task_id, second_task_id)
        self.assertNotIn("CODEX_CLOUD_ENV_ID", codex_client.__dict__)


if __name__ == "__main__":
    unittest.main()
