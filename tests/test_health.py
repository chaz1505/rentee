import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


class HealthEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_health_returns_deployed_render_commit(self):
        with patch.dict(
            os.environ, {"RENDER_GIT_COMMIT": "abc123def456"}, clear=False
        ):
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "status": "ok",
            "commit": "abc123def456",
        })

    def test_health_returns_null_when_commit_is_unavailable(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RENDER_GIT_COMMIT", None)
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "status": "ok",
            "commit": None,
        })

    def test_health_has_no_external_or_workflow_side_effects(self):
        with patch.object(app_module.requests, "get") as requests_get, patch.object(
            app_module.requests, "post"
        ) as requests_post, patch.object(
            app_module.requests, "patch"
        ) as requests_patch, patch.object(
            app_module, "client"
        ) as openai_client, patch.object(
            app_module, "_run_benchmark_background"
        ) as benchmark_runner:
            response = self.client.get("/health")

        self.assertEqual(response.status_code, 200)
        requests_get.assert_not_called()
        requests_post.assert_not_called()
        requests_patch.assert_not_called()
        openai_client.assert_not_called()
        benchmark_runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
