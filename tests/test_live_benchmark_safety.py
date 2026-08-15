import os
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from . import run_benchmark
from . import test_subject


CASE = {
    "id": "generic_01",
    "name": "Generic benchmark",
    "initial_ai_searchtext": "No preferences yet"
}
SUBJECT = {"lead_id": "live-lead", "folio_id": "live-folio"}


class LiveBenchmarkSafetyTests(unittest.TestCase):
    @patch("tests.test_subject.bubble_get")
    def test_live_lead_test_true_and_linked_folio_pass(self, mocked_get):
        mocked_get.side_effect = [
            {"_id": "live-lead", "test": True},
            {"_id": "live-folio", "lead": "live-lead", "folioItems": []}
        ]
        lead, folio = test_subject.verify_live_test_subject(CASE, SUBJECT)
        self.assertIs(lead["test"], True)
        self.assertEqual(folio["lead"], SUBJECT["lead_id"])

    @patch("tests.test_subject.bubble_get")
    def test_live_lead_false_or_missing_test_fails(self, mocked_get):
        for lead in ({"test": False}, {}):
            with self.subTest(lead=lead):
                mocked_get.reset_mock()
                mocked_get.side_effect = [lead]
                with self.assertRaises(test_subject.LiveBenchmarkSafetyError):
                    test_subject.verify_live_test_subject(CASE, SUBJECT)
                self.assertEqual(mocked_get.call_count, 1)

    @patch("tests.test_subject.bubble_get")
    def test_wrong_folio_lead_fails(self, mocked_get):
        mocked_get.side_effect = [
            {"test": True},
            {"lead": "unrelated-customer-lead"}
        ]
        with self.assertRaises(test_subject.LiveBenchmarkSafetyError):
            test_subject.verify_live_test_subject(CASE, SUBJECT)

    @patch("tests.test_subject.bubble_delete")
    @patch("tests.test_subject.bubble_patch")
    @patch("tests.test_subject.bubble_get")
    def test_safety_failure_occurs_before_any_mutation(
        self, mocked_get, mocked_patch, mocked_delete
    ):
        mocked_get.return_value = {"test": False}
        with patch.dict(os.environ, {"BENCHMARK_LIVE_ENABLED": "true"}, clear=True):
            with self.assertRaises(test_subject.LiveBenchmarkSafetyError):
                test_subject.reset_test_subject(CASE, SUBJECT, "live")
        mocked_patch.assert_not_called()
        mocked_delete.assert_not_called()

    @patch("tests.test_subject.bubble_delete")
    @patch("tests.test_subject.bubble_patch")
    @patch("tests.test_subject.bubble_get")
    def test_verified_live_reset_touches_only_referenced_folio_items(
        self, mocked_get, mocked_patch, mocked_delete
    ):
        mocked_get.side_effect = [
            {"test": True},
            {"lead": "live-lead", "folioItems": ["item-1", "item-2"]}
        ]
        with patch.dict(os.environ, {"BENCHMARK_LIVE_ENABLED": "true"}, clear=True):
            test_subject.reset_test_subject(CASE, SUBJECT, "live")
        self.assertEqual(mocked_delete.call_args_list, [
            call("obj/folioItem/item-1", "live"),
            call("obj/folioItem/item-2", "live")
        ])
        self.assertEqual(mocked_patch.call_args_list[0], call(
            "obj/folio/live-folio",
            {"folioItems": [], "newRecommendations": False},
            "live"
        ))
        self.assertEqual(mocked_patch.call_args_list[1], call(
            "obj/lead/live-lead",
            {"AIsearchtext": "No preferences yet", "AIsearchsummary": ""},
            "live"
        ))

    @patch("tests.test_subject._save_state")
    @patch("tests.test_subject._load_state", return_value={
        "development": {}, "live": {}
    })
    @patch("tests.test_subject.bubble_post")
    def test_live_creation_sets_test_true_and_separate_state(
        self, mocked_post, mocked_state, mocked_save
    ):
        mocked_post.side_effect = ["new-live-lead", "new-live-folio"]
        with patch.dict(os.environ, {"BENCHMARK_LIVE_ENABLED": "true"}, clear=True):
            subject = test_subject.ensure_test_subject(CASE, "live")
        lead_payload = mocked_post.call_args_list[0].args[1]
        folio_payload = mocked_post.call_args_list[1].args[1]
        self.assertIs(lead_payload["test"], True)
        self.assertEqual(folio_payload["lead"], "new-live-lead")
        saved = mocked_save.call_args.args[0]
        self.assertEqual(saved["live"][CASE["id"]], subject)
        self.assertEqual(saved["development"], {})

    @patch("tests.test_subject._save_state")
    @patch("tests.test_subject._load_state", return_value={
        "development": {}, "live": {}
    })
    @patch("tests.test_subject.bubble_post")
    def test_development_creation_does_not_require_test_marker(
        self, mocked_post, _mocked_state, _mocked_save
    ):
        mocked_post.side_effect = ["dev-lead", "dev-folio"]
        test_subject.ensure_test_subject(CASE, "development")
        self.assertNotIn("test", mocked_post.call_args_list[0].args[1])

    def test_live_mode_requires_explicit_enable_gate(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(run_benchmark.BenchmarkError, "disabled"):
                run_benchmark.validate_benchmark_environment("live")

    @patch("tests.test_subject.bubble_post")
    def test_subject_helper_also_requires_live_enable_before_create(self, mocked_post):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(test_subject.LiveBenchmarkSafetyError):
                test_subject.ensure_test_subject(CASE, "live")
        mocked_post.assert_not_called()

    @patch("tests.run_benchmark.requests.post")
    def test_live_chat_payload_is_explicit(self, mocked_post):
        response = Mock(ok=True, status_code=200)
        response.iter_lines.return_value = [
            'data: {"delta":"ok"}', "",
            'data: {"done":true,"response_id":"r1"}', ""
        ]
        mocked_post.return_value = response
        run_benchmark.run_turn("hello", "folio", environment="live")
        self.assertEqual(mocked_post.call_args.kwargs["json"]["bubble_env"], "live")

    def test_live_artifact_filename_is_separate(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            run_benchmark, "TESTS_DIR", directory
        ):
            path = run_benchmark._save_result(
                "generic_01", {"bubble_env": "live"}, "live"
            )
        self.assertIn("generic_01_live_", os.path.basename(path))


if __name__ == "__main__":
    unittest.main()
