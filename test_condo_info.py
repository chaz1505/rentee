import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module


CSV_DATA = """Condo name,Address,Persona,Future Column
Ken Bangsar ,Jalan Kapas,Family persona,Future value
,Ignored,Ignored,Ignored
Ken Bangsar,,,Later blank duplicate
One Menerung,Jalan Menerung,,
"""


class CondoInfoTests(unittest.TestCase):
    def setUp(self):
        app_module._condo_cache = None
        app_module._condo_cache_checked_at = 0.0
        app_module.app.config["TESTING"] = True

    def response(self, csv_text=CSV_DATA):
        response = MagicMock()
        response.content = csv_text.encode("utf-8")
        return response

    @patch("app.requests.get")
    def test_loads_dynamic_columns_normalizes_name_and_caches(self, mocked_get):
        mocked_get.return_value = self.response()

        first = app_module.get_condo_info("  KEN   bangsar ")
        second = app_module.get_condo_info("Ken Bangsar")

        self.assertEqual(first["Persona"], "Family persona")
        self.assertEqual(first["Future Column"], "Future value")
        self.assertEqual(first["Condo name"], "Ken Bangsar")
        self.assertEqual(second, first)
        mocked_get.assert_called_once_with(
            app_module.CONDO_SHEET_CSV_URL,
            timeout=app_module.CONDO_SHEET_TIMEOUT_SECONDS
        )

    @patch("app.time.monotonic", side_effect=[0.0, 0.0, 301.0, 301.0])
    @patch("app.requests.get")
    def test_refreshes_after_ttl(self, mocked_get, _mocked_time):
        mocked_get.side_effect = [
            self.response(CSV_DATA),
            self.response(CSV_DATA.replace("Family persona", "Refreshed persona")),
        ]
        self.assertEqual(app_module.get_condo_info("Ken Bangsar")["Persona"], "Family persona")
        self.assertEqual(app_module.get_condo_info("Ken Bangsar")["Persona"], "Refreshed persona")
        self.assertEqual(mocked_get.call_count, 2)

    @patch("app.time.monotonic", side_effect=[0.0, 0.0, 301.0, 301.0])
    @patch("app.requests.get")
    def test_failed_refresh_uses_stale_cache(self, mocked_get, _mocked_time):
        mocked_get.side_effect = [self.response(), RuntimeError("sheet unavailable")]
        original = app_module.get_condo_info("Ken Bangsar")
        stale = app_module.get_condo_info("Ken Bangsar")
        self.assertEqual(stale, original)

    @patch("app.requests.get", side_effect=RuntimeError("sheet unavailable"))
    def test_initial_failure_returns_service_unavailable(self, _mocked_get):
        response = app_module.app.test_client().get("/test_condo?name=Ken%20Bangsar")
        self.assertEqual(response.status_code, 503)
        self.assertIn("temporarily unavailable", response.get_json()["error"])

    @patch("app.requests.get")
    def test_endpoint_success_not_found_and_missing_name(self, mocked_get):
        mocked_get.return_value = self.response()
        client = app_module.app.test_client()

        success = client.get("/test_condo?name=Ken%20Bangsar")
        missing = client.get("/test_condo?name=Unknown")
        blank = client.get("/test_condo")

        self.assertEqual(success.status_code, 200)
        self.assertEqual(success.get_json()["Persona"], "Family persona")
        self.assertEqual(missing.status_code, 404)
        self.assertIn("not found", missing.get_json()["error"])
        self.assertEqual(blank.status_code, 400)
        self.assertIn("name", blank.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
