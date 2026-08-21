import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


class WhatsAppWebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_get_correct_token_returns_challenge(self):
        with patch.dict(
            os.environ, {"WHATSAPP_VERIFY_TOKEN": "expected-token"}, clear=False
        ):
            response = self.client.get(
                "/whatsapp/webhook",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "expected-token",
                    "hub.challenge": "meta-challenge-123",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(as_text=True), "meta-challenge-123")
        self.assertEqual(response.mimetype, "text/plain")

    def test_get_incorrect_token_returns_403(self):
        with patch.dict(
            os.environ, {"WHATSAPP_VERIFY_TOKEN": "expected-token"}, clear=False
        ):
            response = self.client.get(
                "/whatsapp/webhook",
                query_string={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong-token",
                    "hub.challenge": "must-not-be-returned",
                },
            )

        self.assertEqual(response.status_code, 403)
        self.assertNotIn("must-not-be-returned", response.get_data(as_text=True))

    def test_get_missing_config_fails_safely_without_logging_supplied_token(self):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_VERIFY_TOKEN", None)
            with redirect_stdout(output):
                response = self.client.get(
                    "/whatsapp/webhook",
                    query_string={
                        "hub.mode": "subscribe",
                        "hub.verify_token": "incoming-secret-token",
                        "hub.challenge": "challenge",
                    },
                )

        self.assertEqual(response.status_code, 403)
        self.assertIn("WHATSAPP_VERIFY_TOKEN is missing", output.getvalue())
        self.assertNotIn("incoming-secret-token", output.getvalue())

    def test_post_message_payload_is_logged_and_acknowledged(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messages": [{
                            "from": "60123456789",
                            "type": "text",
                            "text": {"body": "Hello Rentee"},
                        }]
                    },
                }]
            }],
        }
        output = io.StringIO()

        with redirect_stdout(output):
            response = self.client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "received"})
        self.assertIn("WhatsApp webhook received.", output.getvalue())
        self.assertIn('"body": "Hello Rentee"', output.getvalue())

    def test_post_arbitrary_status_payload_returns_200(self):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "statuses": [{
                            "id": "wamid.status-id",
                            "status": "delivered",
                            "timestamp": "1787190000",
                        }]
                    },
                }]
            }],
        }

        response = self.client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "received"})

    def test_post_redacts_token_like_fields_and_has_no_external_side_effects(self):
        output = io.StringIO()
        payload = {
            "object": "whatsapp_business_account",
            "access_token": "payload-secret",
            "nested": {"authorization": "Bearer payload-secret"},
        }
        with patch.object(app_module.requests, "get") as requests_get, patch.object(
            app_module.requests, "post"
        ) as requests_post, patch.object(
            app_module.requests, "patch"
        ) as requests_patch, patch.object(
            app_module, "client"
        ) as openai_client, redirect_stdout(output):
            response = self.client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("payload-secret", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())
        requests_get.assert_not_called()
        requests_post.assert_not_called()
        requests_patch.assert_not_called()
        openai_client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
