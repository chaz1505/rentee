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
            "GITHUB_TOKEN": "github-token",
            "SAFE_RUNTIME_VALUE": "retained",
        }, clear=True):
            child_environment = codex_client._codex_environment(self.auth_root)
            self.assertEqual(os.environ["BENCHMARK_API_KEY"], "benchmark-key")
            self.assertEqual(os.environ["BUBBLE_API_TOKEN"], "bubble-token")
        self.assertNotIn("BENCHMARK_API_KEY", child_environment)
        self.assertNotIn("BUBBLE_API_TOKEN", child_environment)
        self.assertNotIn("GITHUB_TOKEN", child_environment)
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
        published = {
            "fix_commit": "fix123", "pr_number": 12,
            "pr_url": "https://github.com/chaz1505/rentee/pull/12",
        }
        with patch("automation.codex_client._run", side_effect=fake_run), patch(
            "automation.codex_client.persist_codex_changes_to_github",
            return_value=published,
        ) as mocked_publish, patch.dict(
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
        self.assertEqual(result["status"], "merged")
        mocked_publish.assert_called_once()
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
        with patch("automation.codex_client._run", side_effect=fake_run), patch(
            "automation.codex_client.persist_codex_changes_to_github",
            return_value={
                "fix_commit": "fix123", "pr_number": 12,
                "pr_url": "https://github.com/chaz1505/rentee/pull/12",
            },
        ), patch.dict(
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

    def _publish_workspace(self):
        workspace = self.workspace_root / "task-id"
        (workspace / ".git").mkdir(parents=True)
        return workspace

    def _github_response(self, body, status=200):
        response = Mock(ok=200 <= status < 300, status_code=status)
        response.json.return_value = body
        response.text = str(body)
        return response

    def test_publish_commits_pushes_and_creates_pull_request(self):
        workspace = self._publish_workspace()
        calls = []
        auth_files_seen = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[1] in ("ls-remote", "push"):
                auth_files_seen.append(
                    Path(kwargs["env"]["GIT_ASKPASS"]).is_file()
                )
            if command[1:] == ["diff", "--cached", "--quiet"]:
                return completed(returncode=1)
            if command[1:] == ["rev-parse", "HEAD"]:
                return completed(stdout="fix123\n")
            return completed()

        created_pr = {
            "number": 42,
            "html_url": "https://github.com/chaz1505/rentee/pull/42",
        }
        with patch("automation.codex_client._run", side_effect=fake_run), patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response([]),
        ) as mocked_get, patch(
            "automation.codex_client.requests.post",
            return_value=self._github_response(created_pr, 201),
        ) as mocked_post, patch.dict(os.environ, {
            "GITHUB_TOKEN": "github-secret",
            "OPENAI_API_KEY": "openai-secret",
            "BENCHMARK_API_KEY": "benchmark-secret",
            "BUBBLE_API_TOKEN": "bubble-secret",
        }, clear=True), patch(
            "automation.codex_client._merge_pull_request",
            return_value="merge123",
        ) as mocked_merge:
            result = codex_client.persist_codex_changes_to_github(
                "/usr/bin/git", workspace, "codex/benchmark-run-unique",
                "run-id", "live", "task-id", "base123",
                ["app.py"], "safe summary",
            )

        commands = [call[0] for call in calls]
        self.assertIn(["/usr/bin/git", "add", "-A"], commands)
        commit = next(command for command in commands if "commit" in command)
        self.assertIn("user.name=Rentee Codex", commit)
        self.assertIn("user.email=codex@rentee.asia", commit)
        push_call = next(call for call in calls if call[0][1] == "push")
        self.assertEqual(push_call[0], [
            "/usr/bin/git", "push", "origin",
            "codex/benchmark-run-unique",
        ])
        flattened_commands = "\n".join(" ".join(command) for command in commands)
        self.assertNotIn(" merge ", f" {flattened_commands} ")
        self.assertNotIn("deploy", flattened_commands)
        self.assertNotIn("run_benchmark", flattened_commands)
        self.assertNotIn("OPENAI_API_KEY", push_call[1]["env"])
        self.assertNotIn("BENCHMARK_API_KEY", push_call[1]["env"])
        self.assertNotIn("BUBBLE_API_TOKEN", push_call[1]["env"])
        self.assertNotIn("GITHUB_TOKEN", push_call[1]["env"])
        self.assertEqual(
            push_call[1]["env"]["RENTEE_GITHUB_USERNAME"],
            "x-access-token",
        )
        self.assertEqual(
            push_call[1]["env"]["RENTEE_GITHUB_PASSWORD"],
            "github-secret",
        )
        askpass_path = Path(push_call[1]["env"]["GIT_ASKPASS"])
        self.assertEqual(auth_files_seen, [True, True])
        self.assertFalse(askpass_path.exists())
        remote_call = next(call for call in calls if call[0][1] == "ls-remote")
        self.assertEqual(remote_call[1]["env"], push_call[1]["env"])
        self.assertEqual(remote_call[0][3], "origin")
        self.assertFalse(any(command[1:3] == ["remote", "set-url"] for command in commands))
        self.assertFalse(any(command[1] == "config" for command in commands))
        self.assertNotIn("github-secret", "\n".join(
            " ".join(command) for command in commands
        ))
        self.assertEqual(result["fix_commit"], "fix123")
        self.assertEqual(result["pr_number"], 42)
        self.assertTrue(result["merged"])
        self.assertEqual(result["merge_commit"], "merge123")
        self.assertRegex(result["merged_at"], r"Z$")
        mocked_merge.assert_called_once_with(
            "github-secret", 42, "codex/benchmark-run-unique", "fix123"
        )
        self.assertEqual(mocked_get.call_args.kwargs["params"], {
            "state": "open", "head": "chaz1505:codex/benchmark-run-unique",
            "base": "main",
        })
        pr_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(pr_payload["head"], "codex/benchmark-run-unique")
        self.assertEqual(pr_payload["base"], "main")
        self.assertNotIn("github-secret", pr_payload["body"])
        self.assertNotIn("benchmark-secret", pr_payload["body"])

    def test_existing_pull_request_is_reused(self):
        existing_pr = {
            "number": 9,
            "html_url": "https://github.com/chaz1505/rentee/pull/9",
        }
        with patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response([existing_pr]),
        ), patch("automation.codex_client.requests.post") as mocked_post:
            result = codex_client._find_or_create_pull_request(
                "token", "codex/benchmark-existing", "run", "body"
            )
        self.assertEqual(result, existing_pr)
        mocked_post.assert_not_called()

    def test_existing_remote_commit_skips_push_and_duplicate_commit(self):
        workspace = self._publish_workspace()
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            if command[1:] == ["diff", "--cached", "--quiet"]:
                return completed(returncode=0)
            if command[1:] == ["rev-parse", "HEAD"]:
                return completed(stdout="fix123\n")
            if command[1] == "ls-remote":
                return completed(
                    stdout="fix123\trefs/heads/codex/benchmark-existing\n"
                )
            return completed()

        existing_pr = {
            "number": 9,
            "html_url": "https://github.com/chaz1505/rentee/pull/9",
        }
        with patch("automation.codex_client._run", side_effect=fake_run), patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response([existing_pr]),
        ), patch("automation.codex_client.requests.post") as mocked_post, patch(
            "automation.codex_client._merge_pull_request",
            return_value="merge123",
        ), patch.dict(
            os.environ, {"GITHUB_TOKEN": "token"}, clear=True
        ):
            result = codex_client.persist_codex_changes_to_github(
                "git", workspace, "codex/benchmark-existing", "run", "live",
                "task-id", "base", ["app.py"], "summary",
            )
        self.assertFalse(any("commit" in command for command in calls))
        self.assertFalse(any(command[1] == "push" for command in calls))
        mocked_post.assert_not_called()
        self.assertEqual(result["pr_number"], 9)

    def _pull_detail(self, **updates):
        detail = {
            "state": "open",
            "merged": False,
            "merge_commit_sha": None,
            "base": {
                "ref": "main",
                "repo": {"full_name": "chaz1505/rentee"},
            },
            "head": {
                "ref": "codex/benchmark-safe",
                "sha": "fix123",
                "repo": {"full_name": "chaz1505/rentee"},
            },
        }
        detail.update(updates)
        return detail

    def test_merge_validates_pull_and_uses_squash_api(self):
        with patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response(self._pull_detail()),
        ) as mocked_get, patch(
            "automation.codex_client.requests.put",
            return_value=self._github_response({
                "merged": True, "sha": "merge123",
                "message": "Pull Request successfully merged",
            }),
        ) as mocked_put:
            result = codex_client._merge_pull_request(
                "token", 42, "codex/benchmark-safe", "fix123"
            )
        self.assertEqual(result, "merge123")
        self.assertTrue(mocked_get.call_args.args[0].endswith(
            "/repos/chaz1505/rentee/pulls/42"
        ))
        self.assertTrue(mocked_put.call_args.args[0].endswith(
            "/repos/chaz1505/rentee/pulls/42/merge"
        ))
        self.assertEqual(mocked_put.call_args.kwargs["json"], {
            "merge_method": "squash", "sha": "fix123",
        })

    def test_merge_rejects_wrong_repo_base_head_and_closed_pr(self):
        unsafe_pulls = (
            self._pull_detail(base={
                "ref": "main", "repo": {"full_name": "other/repo"},
            }),
            self._pull_detail(base={
                "ref": "develop",
                "repo": {"full_name": "chaz1505/rentee"},
            }),
            self._pull_detail(head={
                "ref": "feature/unsafe", "sha": "fix123",
                "repo": {"full_name": "chaz1505/rentee"},
            }),
            self._pull_detail(state="closed"),
        )
        for pull in unsafe_pulls:
            with self.subTest(pull=pull), patch(
                "automation.codex_client.requests.get",
                return_value=self._github_response(pull),
            ), patch("automation.codex_client.requests.put") as mocked_put:
                with self.assertRaises(codex_client.CodexSubmissionError):
                    codex_client._merge_pull_request(
                        "token", 42, "codex/benchmark-safe", "fix123"
                    )
                mocked_put.assert_not_called()

    def test_already_merged_pull_is_idempotent_success(self):
        pull = self._pull_detail(
            state="closed", merged=True, merge_commit_sha="merge123"
        )
        with patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response(pull),
        ), patch("automation.codex_client.requests.put") as mocked_put:
            result = codex_client._merge_pull_request(
                "token", 42, "codex/benchmark-safe", "fix123"
            )
        self.assertEqual(result, "merge123")
        mocked_put.assert_not_called()

    def test_unmergeable_pull_fails_without_retry_or_token_leak(self):
        token = "github-secret"
        output = io.StringIO()
        with patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response(self._pull_detail()),
        ), patch(
            "automation.codex_client.requests.put",
            return_value=self._github_response({"message": token}, 405),
        ) as mocked_put, redirect_stdout(output), self.assertRaisesRegex(
            codex_client.CodexSubmissionError, "pull request merge failed"
        ) as caught:
            codex_client._merge_pull_request(
                token, 42, "codex/benchmark-safe", "fix123"
            )
        self.assertEqual(mocked_put.call_count, 1)
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token, str(caught.exception))

    def test_pull_request_creation_failure_is_distinct(self):
        with patch(
            "automation.codex_client.requests.get",
            return_value=self._github_response([]),
        ), patch(
            "automation.codex_client.requests.post",
            return_value=self._github_response({"message": "denied"}, 403),
        ), self.assertRaisesRegex(
            codex_client.CodexSubmissionError,
            "GitHub pull request creation failed: HTTP 403",
        ):
            codex_client._find_or_create_pull_request(
                "token", "codex/benchmark-branch", "run", "body"
            )

    def test_publish_rejects_unsafe_branches_and_requires_token(self):
        for branch in ("main", "feature/not-codex"):
            with self.assertRaisesRegex(
                codex_client.CodexSubmissionError, "unsafe Git branch"
            ):
                codex_client._verify_publish_branch(branch)
        workspace = self._publish_workspace()
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            codex_client.CodexSubmissionError, "GITHUB_TOKEN is required"
        ):
            codex_client.persist_codex_changes_to_github(
                "git", workspace, "codex/benchmark-safe", "run", "live",
                "task-id", "base", ["app.py"], "summary",
            )

    def test_push_failure_is_sanitized_and_stops_before_pull_request(self):
        workspace = self._publish_workspace()
        token = "github-secret"

        def fake_run(command, **kwargs):
            if command[1:] == ["diff", "--cached", "--quiet"]:
                return completed(returncode=1)
            if command[1:] == ["rev-parse", "HEAD"]:
                return completed(stdout="fix123\n")
            if command[1] == "push":
                return completed(1, stderr=f"push rejected {token}")
            return completed()

        output = io.StringIO()
        with patch("automation.codex_client._run", side_effect=fake_run), patch(
            "automation.codex_client.requests.get"
        ) as mocked_get, patch.dict(
            os.environ, {"GITHUB_TOKEN": token}, clear=True
        ), redirect_stdout(output), self.assertRaisesRegex(
            codex_client.CodexSubmissionError, "Git branch push failed"
        ):
            codex_client.persist_codex_changes_to_github(
                "git", workspace, "codex/benchmark-safe", "run", "live",
                "task-id", "base", ["app.py"], "summary",
            )
        mocked_get.assert_not_called()
        self.assertNotIn(token, output.getvalue())

    @patch("automation.codex_client.shutil.which")
    def test_noop_is_success_without_changes(self, mocked_which):
        mocked_which.side_effect = lambda name: f"/usr/bin/{name}"
        calls, fake_run = self._successful_run(status="")
        with patch("automation.codex_client._run", side_effect=fake_run), patch(
            "automation.codex_client.requests.get"
        ) as mocked_get, patch(
            "automation.codex_client.requests.post"
        ) as mocked_post, patch.dict(
            os.environ, {"OPENAI_API_KEY": "api-key"}, clear=True
        ):
            result = codex_client.submit_codex_fix(
                "prompt", "run", "bubble", "development", task_id="codex_noop"
            )
        self.assertFalse(result["changes_detected"])
        self.assertEqual(result["changed_files"], [])
        commands = [call[0] for call in calls]
        self.assertFalse(any("commit" in command for command in commands))
        self.assertFalse(any("push" in command for command in commands))
        mocked_get.assert_not_called()
        mocked_post.assert_not_called()

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
