import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from . import git_results


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class GitResultsTests(unittest.TestCase):
    def _artifacts(self, directory):
        paths = [
            os.path.join(directory, "sofia_01_20260815_174500.json"),
            os.path.join(directory, "sofia_01_20260815_174500_evaluation.json"),
            os.path.join(directory, "sofia_01_20260815_174500_fix_prompt.md")
        ]
        with open(paths[0], "w", encoding="utf-8") as output:
            json.dump({
                "case_id": "sofia_01",
                "started_at_utc": "2026-08-15T17:45:00Z"
            }, output)
        for path in paths[1:]:
            with open(path, "w", encoding="utf-8") as output:
                output.write("artifact")
        return paths

    @patch("tests.git_results.subprocess.run")
    def test_disabled_is_a_git_noop(self, mocked_run):
        with patch.dict(os.environ, {}, clear=True):
            result = git_results.persist_benchmark_results(["unused"])
        self.assertEqual(result["status"], "skipped")
        mocked_run.assert_not_called()

    @patch("tests.git_results.subprocess.run")
    def test_stages_only_artifacts_commits_specific_paths_and_pushes(self, mocked_run):
        with tempfile.TemporaryDirectory() as repo:
            paths = self._artifacts(repo)
            mocked_run.side_effect = [
                completed(stdout="true\n"),
                completed(stdout=f"{repo}\n"),
                completed(stdout="main\n"),
                completed(),
                completed(returncode=1),
                completed(),
                completed(stdout="origin/main\n"),
                completed()
            ]
            with patch.dict(os.environ, {"BENCHMARK_COMMIT_RESULTS": "true"}, clear=True):
                result = git_results.persist_benchmark_results(paths)

        commands = [call.args[0] for call in mocked_run.call_args_list]
        relative = [os.path.basename(path) for path in paths]
        self.assertEqual(commands[3], ["git", "add", "--", *relative])
        self.assertEqual(
            commands[5],
            [
                "git", "commit", "-m",
                "Save benchmark results: sofia_01 2026-08-15 17:45 UTC",
                "--", *relative
            ]
        )
        self.assertEqual(commands[-1], ["git", "push"])
        self.assertFalse(any("--force" in command or "-f" in command for command in commands))
        self.assertFalse(any("unrelated" in item for command in commands for item in command))
        self.assertEqual(result["status"], "pushed")

    @patch("tests.git_results.subprocess.run")
    def test_no_staged_changes_skips_commit_and_push(self, mocked_run):
        with tempfile.TemporaryDirectory() as repo:
            paths = self._artifacts(repo)
            mocked_run.side_effect = [
                completed(stdout="true\n"),
                completed(stdout=f"{repo}\n"),
                completed(stdout="main\n"),
                completed(),
                completed(returncode=0)
            ]
            with patch.dict(os.environ, {"BENCHMARK_COMMIT_RESULTS": " true "}, clear=True):
                result = git_results.persist_benchmark_results(paths)
        commands = [call.args[0] for call in mocked_run.call_args_list]
        self.assertEqual(result["status"], "skipped")
        self.assertFalse(any(command[1] in ("commit", "push") for command in commands))

    @patch("tests.git_results.subprocess.run")
    def test_push_failure_warns_without_raising(self, mocked_run):
        with tempfile.TemporaryDirectory() as repo:
            paths = self._artifacts(repo)
            mocked_run.side_effect = [
                completed(stdout="true\n"),
                completed(stdout=f"{repo}\n"),
                completed(stdout="feature/results\n"),
                completed(),
                completed(returncode=1),
                completed(),
                completed(stdout="origin/feature/results\n"),
                completed(returncode=128, stderr="fatal: Authentication failed")
            ]
            with patch.dict(os.environ, {"BENCHMARK_COMMIT_RESULTS": "true"}, clear=True):
                result = git_results.persist_benchmark_results(paths)
        self.assertEqual(result["status"], "warning")
        self.assertTrue(result["committed"])
        self.assertIn("Authentication failed", result["message"])

    @patch("tests.git_results.subprocess.run")
    def test_missing_upstream_keeps_local_commit(self, mocked_run):
        with tempfile.TemporaryDirectory() as repo:
            paths = self._artifacts(repo)
            mocked_run.side_effect = [
                completed(stdout="true\n"),
                completed(stdout=f"{repo}\n"),
                completed(stdout="main\n"),
                completed(),
                completed(returncode=1),
                completed(),
                completed(returncode=128, stderr="no upstream")
            ]
            with patch.dict(os.environ, {"BENCHMARK_COMMIT_RESULTS": "true"}, clear=True):
                result = git_results.persist_benchmark_results(paths)
        self.assertEqual(result["status"], "warning")
        self.assertIn("main has no configured upstream", result["message"])
        self.assertFalse(any(
            call.args[0] == ["git", "push"] for call in mocked_run.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
