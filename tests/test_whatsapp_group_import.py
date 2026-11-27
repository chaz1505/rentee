import os
import unittest
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as rentee
import whatsapp_importer


class WhatsappGroupImportTest(unittest.TestCase):
    url = "/whatsapp/group-import"
    token = "a" * 64
    headers = {"Authorization": f"Bearer {token}"}

    def setUp(self):
        self.client = rentee.app.test_client()

    def test_requires_configured_token(self):
        with patch.dict(os.environ, {"RENTEE_GROUP_IMPORT_TOKEN": ""}):
            self.assertEqual(self.client.post(self.url).status_code, 404)
        with patch.dict(os.environ, {"RENTEE_GROUP_IMPORT_TOKEN": self.token}):
            self.assertEqual(self.client.post(self.url, json={"text": "WTL"}).status_code, 401)
            self.assertEqual(self.client.post(self.url, json={}, headers=self.headers).status_code, 400)

    def test_uses_property_importer_without_sending_confirmation(self):
        text = "WTL Bangsar\nRM3500"
        with (
            patch.dict(os.environ, {"RENTEE_GROUP_IMPORT_TOKEN": self.token,
                                    "RENTEE_GROUP_IMPORT_BUBBLE_ENV": "live"}),
            patch.object(whatsapp_importer, "detect_import_intent",
                         return_value={"intent": "listing_import"}) as detect,
            patch.object(whatsapp_importer, "process_whatsapp_import",
                         return_value={"status": "processed", "confirmation": "Created"}) as importer,
            patch.object(rentee, "send_whatsapp_text") as send_confirmation,
        ):
            response = self.client.post(self.url, json={"text": text}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "processed"})
        detect.assert_called_once_with(text)
        importer.assert_called_once_with(text, bubble_env="live", import_type="listing")
        send_confirmation.assert_not_called()

    def test_ignores_chat_and_reports_import_failures_for_retry(self):
        with (
            patch.dict(os.environ, {"RENTEE_GROUP_IMPORT_TOKEN": self.token,
                                    "RENTEE_GROUP_IMPORT_BUBBLE_ENV": "development"}),
            patch.object(whatsapp_importer, "detect_import_intent",
                         return_value={"intent": "unknown"}) as detect,
            patch.object(whatsapp_importer, "process_whatsapp_import",
                         side_effect=RuntimeError) as importer,
        ):
            response = self.client.post(self.url, json={"text": "Thanks"}, headers=self.headers)
            self.assertEqual(response.json, {"status": "ignored"})
            importer.assert_not_called()
            detect.return_value = {"intent": "lead_import"}
            response = self.client.post(self.url, json={"text": "WTR KLCC"}, headers=self.headers)
            self.assertEqual(response.status_code, 503)
