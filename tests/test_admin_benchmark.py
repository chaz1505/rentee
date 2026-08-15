import io
import os
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module
from . import run_benchmark


class FakeThread:
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


class AdminBenchmarkTests(unittest.TestCase):
    def setUp(self):
        app_module._benchmark_run_lock = threading.Lock()
        with app_module._benchmark_state_lock:
            app_module._benchmark_state.clear()
            app_module._benchmark_state.update({"status": "idle"})
        FakeThread.instances.clear()
        self.client = app_module.app.test_client()

    def _headers(self, key="correct-key"):
        return {"X-Benchmark-Key": key}

    def test_missing_configuration_returns_503(self):
        with patch.dict(os.environ, {}, clear=True):
            response = self.client.post("/admin/run_benchmark")
        self.assertEqual(response.status_code, 503)

    def test_incorrect_key_returns_401(self):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "correct-key"}, clear=True):
            response = self.client.post(
                "/admin/run_benchmark", headers=self._headers("wrong-key")
            )
        self.assertEqual(response.status_code, 401)

    @patch("app.threading.Thread", FakeThread)
    @patch("tests.run_benchmark.get_benchmark_case_ids", return_value=["sofia_01"])
    def test_correct_key_starts_background_and_returns_immediately(self, _mocked_cases):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "correct-key"}, clear=True):
            response = self.client.post(
                "/admin/run_benchmark", headers=self._headers()
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["status"], "started")
        self.assertEqual(response.get_json()["case"], "sofia_01")
        self.assertEqual(len(FakeThread.instances), 1)
        self.assertTrue(FakeThread.instances[0].started)

    @patch("app.threading.Thread", FakeThread)
    @patch("tests.run_benchmark.get_benchmark_case_ids", return_value=["sofia_01"])
    def test_second_request_while_running_returns_409(self, _mocked_cases):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "correct-key"}, clear=True):
            first = self.client.post("/admin/run_benchmark", headers=self._headers())
            second = self.client.post("/admin/run_benchmark", headers=self._headers())
        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.get_json()["run_id"], first.get_json()["run_id"])
        self.assertEqual(len(FakeThread.instances), 1)

    @patch("tests.run_benchmark.run_all_benchmarks")
    def test_lock_clears_and_state_completes_after_success(self, mocked_run):
        run_id = "run-success"
        mocked_run.return_value = {"results": [{"execution": {
            "benchmark_status": "pass", "result_path": "raw.json",
            "evaluation_path": "evaluation.json",
            "evaluation_markdown_path": "evaluation.md",
            "fix_prompt_path": "prompt.md",
            "benchmark_run_id": "bubble-run-id", "result_persisted": True,
            "persistence_error": None
        }}]}
        app_module._benchmark_run_lock.acquire()
        with app_module._benchmark_state_lock:
            app_module._benchmark_state.update({"status": "running", "run_id": run_id})
        app_module._run_benchmark_background(run_id)
        self.assertTrue(app_module._benchmark_run_lock.acquire(blocking=False))
        app_module._benchmark_run_lock.release()
        with app_module._benchmark_state_lock:
            state = dict(app_module._benchmark_state)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["result_path"], "raw.json")
        self.assertEqual(state["evaluation_markdown_path"], "evaluation.md")
        self.assertTrue(state["result_persisted"])
        self.assertEqual(state["benchmark_run_id"], "bubble-run-id")

    @patch("tests.run_benchmark.run_all_benchmarks", side_effect=RuntimeError("boom"))
    def test_background_exception_is_logged_and_lock_clears(self, _mocked_run):
        run_id = "run-failure"
        app_module._benchmark_run_lock.acquire()
        with app_module._benchmark_state_lock:
            app_module._benchmark_state.update({"status": "running", "run_id": run_id})
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            app_module._run_benchmark_background(run_id)
        self.assertIn("[BENCHMARK] BENCHMARK FAILED: boom", output.getvalue())
        self.assertIn("Traceback", output.getvalue())
        self.assertTrue(app_module._benchmark_run_lock.acquire(blocking=False))
        app_module._benchmark_run_lock.release()
        with app_module._benchmark_state_lock:
            self.assertEqual(app_module._benchmark_state["status"], "failed")

    @patch("tests.run_benchmark.run_all_benchmarks")
    def test_persistence_failure_is_reported_without_crashing_worker(self, mocked_run):
        mocked_run.return_value = {"results": [{"execution": {
            "benchmark_status": "fail", "result_path": "raw.json",
            "evaluation_path": "evaluation.json",
            "evaluation_markdown_path": "evaluation.md",
            "fix_prompt_path": "prompt.md", "benchmark_run_id": None,
            "result_persisted": False, "persistence_error": "HTTP 400: bad payload"
        }}]}
        run_id = "persistence-failure"
        app_module._benchmark_run_lock.acquire()
        with app_module._benchmark_state_lock:
            app_module._benchmark_state.update({"status": "running", "run_id": run_id})
        app_module._run_benchmark_background(run_id)
        with app_module._benchmark_state_lock:
            state = dict(app_module._benchmark_state)
        self.assertEqual(state["status"], "complete")
        self.assertFalse(state["result_persisted"])
        self.assertEqual(state["persistence_error"], "HTTP 400: bad payload")
        self.assertEqual(state["result_path"], "raw.json")

    def test_status_endpoint_idle_running_and_complete(self):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "correct-key"}, clear=True):
            idle = self.client.get("/admin/benchmark_status", headers=self._headers())
            self.assertEqual(idle.get_json(), {"status": "idle"})

            with app_module._benchmark_state_lock:
                app_module._benchmark_state.clear()
                app_module._benchmark_state.update({
                    "status": "running", "run_id": "r1", "case": "sofia_01",
                    "started_at": "now", "current_turn": 3
                })
            running = self.client.get("/admin/benchmark_status", headers=self._headers())
            self.assertEqual(running.get_json()["current_turn"], 3)

            with app_module._benchmark_state_lock:
                app_module._benchmark_state.update({
                    "status": "complete", "completed_at": "later",
                    "benchmark_status": "fail", "result_path": "raw.json",
                    "evaluation_path": "evaluation.json",
                    "evaluation_markdown_path": "evaluation.md",
                    "fix_prompt_path": "prompt.md",
                    "benchmark_run_id": "bubble-run-id", "result_persisted": True
                })
            complete = self.client.get("/admin/benchmark_status", headers=self._headers())
            self.assertEqual(complete.get_json()["status"], "complete")
            self.assertEqual(complete.get_json()["fix_prompt_path"], "prompt.md")
            self.assertEqual(
                complete.get_json()["evaluation_markdown_path"], "evaluation.md"
            )

    def test_secret_never_appears_in_endpoint_logs(self):
        secret = "do-not-log-this-key"
        output = io.StringIO()
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": secret}, clear=True):
            with redirect_stdout(output), redirect_stderr(output):
                response = self.client.post(
                    "/admin/run_benchmark",
                    headers={"X-Benchmark-Key": "incorrect-secret"}
                )
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn("incorrect-secret", output.getvalue())

    def test_live_endpoint_requires_enable_and_ignores_arbitrary_ids(self):
        with patch.dict(os.environ, {"BENCHMARK_API_KEY": "correct-key"}, clear=True):
            disabled = self.client.post(
                "/admin/run_benchmark",
                headers=self._headers(),
                json={
                    "environment": "live",
                    "lead_id": "customer-lead",
                    "folio_id": "customer-folio"
                }
            )
        self.assertEqual(disabled.status_code, 403)
        with app_module._benchmark_state_lock:
            self.assertEqual(app_module._benchmark_state, {"status": "idle"})

    @patch("app.threading.Thread", FakeThread)
    @patch("tests.run_benchmark.get_benchmark_case_ids", return_value=["sofia_01"])
    def test_enabled_live_endpoint_passes_only_environment_to_worker(
        self, _mocked_cases
    ):
        with patch.dict(os.environ, {
            "BENCHMARK_API_KEY": "correct-key",
            "BENCHMARK_LIVE_ENABLED": "true"
        }, clear=True):
            response = self.client.post(
                "/admin/run_benchmark",
                headers=self._headers(),
                json={
                    "environment": "live",
                    "lead_id": "customer-lead",
                    "folio_id": "customer-folio"
                }
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["environment"], "live")
        self.assertEqual(FakeThread.instances[0].args[1], "live")
        self.assertNotIn("customer-lead", FakeThread.instances[0].args)
        self.assertNotIn("customer-folio", FakeThread.instances[0].args)

    @patch("tests.run_benchmark.run_all_benchmarks", return_value={"failed": False})
    def test_cli_main_uses_reusable_suite(self, mocked_suite):
        self.assertEqual(run_benchmark.main(), 0)
        mocked_suite.assert_called_once_with(environment="development")


if __name__ == "__main__":
    unittest.main()
