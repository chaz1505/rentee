import json
import os
import unittest
from unittest.mock import Mock, patch

from . import bubble_test_data
from . import run_benchmark


class BenchmarkInfrastructureTests(unittest.TestCase):
    def test_rejects_non_development_bubble_base(self):
        with patch.dict(os.environ, {
            "BUBBLE_DEV_BASE": "https://www.rentee.asia/api/1.1"
        }, clear=False):
            with self.assertRaisesRegex(
                bubble_test_data.BubbleTestDataError,
                "/version-test/"
            ):
                bubble_test_data.get_bubble_dev_base()

    @patch("tests.run_benchmark.requests.post")
    def test_run_turn_parses_sse_and_returns_continuity_id(self, mocked_post):
        events = [
            {"status": "Updating your preferences..."},
            {"delta": "Hello "},
            {"delta": "Sofia"},
            {"citations": [{"url": "https://example.com"}]},
            {"done": True, "response_id": "response-2"}
        ]
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            line
            for event in events
            for line in (f"data: {json.dumps(event)}", "")
        ]
        mocked_post.return_value = response

        result = run_benchmark.run_turn(
            "Hello", "folio-1", previous_response_id="response-1"
        )

        self.assertEqual(result["text"], "Hello Sofia")
        self.assertEqual(result["response_id"], "response-2")
        self.assertTrue(result["done_seen"])
        sent_payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(sent_payload["previous_response_id"], "response-1")
        self.assertEqual(sent_payload["bubble_env"], "development")

    @patch("tests.run_benchmark.requests.post")
    def test_run_turn_requires_response_id(self, mocked_post):
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            'data: {"delta":"Hi"}',
            "",
            'data: {"done":true}',
            ""
        ]
        mocked_post.return_value = response

        with self.assertRaisesRegex(run_benchmark.BenchmarkError, "No response_id"):
            run_benchmark.run_turn("Hello", "folio-1")


if __name__ == "__main__":
    unittest.main()
