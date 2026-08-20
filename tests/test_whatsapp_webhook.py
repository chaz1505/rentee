import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-bubble-token")

import app as app_module


class WhatsAppWebhookTests(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()
        with app_module._whatsapp_message_ids_lock:
            app_module._whatsapp_message_ids.clear()

    @staticmethod
    def message_payload(message_id="wamid.message-1", sender="60123456789"):
        return {
            "object": "whatsapp_business_account",
            "entry": [{
                "changes": [{
                    "field": "messages",
                    "value": {
                        "messages": [{
                            "from": sender,
                            "id": message_id,
                            "type": "text",
                            "text": {"body": "Hello Rentee"},
                        }]
                    },
                }]
            }],
        }

    @staticmethod
    def outbound_success(status_code=200):
        response = Mock()
        response.ok = True
        response.status_code = status_code
        response.text = '{"messages":[{"id":"outbound-id"}]}'
        return response

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
        payload = self.message_payload()
        output = io.StringIO()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_ACCESS_TOKEN", None)
            os.environ.pop("WHATSAPP_PHONE_NUMBER_ID", None)
            with redirect_stdout(output):
                response = self.client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "received"})
        self.assertIn("WhatsApp webhook received.", output.getvalue())
        self.assertIn('"body": "Hello Rentee"', output.getvalue())

    @patch("app.requests.post")
    def test_inbound_text_triggers_one_correct_acknowledgement(self, mocked_post):
        mocked_post.return_value = self.outbound_success()
        env = {
            "WHATSAPP_ACCESS_TOKEN": "meta-access-token",
            "WHATSAPP_PHONE_NUMBER_ID": "phone-number-id",
        }

        with patch.dict(os.environ, env, clear=False):
            response = self.client.post(
                "/whatsapp/webhook",
                json=self.message_payload(sender="60199887766"),
            )

        self.assertEqual(response.status_code, 200)
        mocked_post.assert_called_once()
        request_call = mocked_post.call_args
        self.assertEqual(
            request_call.args[0],
            "https://graph.facebook.com/v23.0/phone-number-id/messages",
        )
        self.assertEqual(request_call.kwargs["json"]["to"], "60199887766")
        self.assertEqual(
            request_call.kwargs["json"]["text"]["body"],
            "Thanks — Rentee received your message.",
        )
        self.assertEqual(request_call.kwargs["json"]["type"], "text")

    @patch("app.requests.post")
    def test_duplicate_message_id_triggers_only_one_outbound_call(self, mocked_post):
        mocked_post.return_value = self.outbound_success()
        env = {
            "WHATSAPP_ACCESS_TOKEN": "meta-access-token",
            "WHATSAPP_PHONE_NUMBER_ID": "phone-number-id",
        }
        payload = self.message_payload(message_id="wamid.duplicate")

        with patch.dict(os.environ, env, clear=False):
            first = self.client.post("/whatsapp/webhook", json=payload)
            second = self.client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        mocked_post.assert_called_once()

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

        with patch("app.requests.post") as mocked_post:
            response = self.client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "received"})
        mocked_post.assert_not_called()

    @patch("app.requests.post")
    def test_missing_outbound_configuration_still_returns_200(self, mocked_post):
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WHATSAPP_ACCESS_TOKEN", None)
            os.environ.pop("WHATSAPP_PHONE_NUMBER_ID", None)
            with redirect_stdout(output):
                response = self.client.post(
                    "/whatsapp/webhook", json=self.message_payload()
                )

        self.assertEqual(response.status_code, 200)
        mocked_post.assert_not_called()
        self.assertIn("missing configuration", output.getvalue())

    @patch("app.requests.post")
    def test_meta_api_failure_is_logged_safely_and_webhook_returns_200(
        self, mocked_post
    ):
        failed_response = Mock()
        failed_response.ok = False
        failed_response.status_code = 400
        failed_response.text = (
            '{"error":"bad request for meta-access-token",'
            '"type":"OAuthException"}'
        )
        mocked_post.return_value = failed_response
        output = io.StringIO()
        env = {
            "WHATSAPP_ACCESS_TOKEN": "meta-access-token",
            "WHATSAPP_PHONE_NUMBER_ID": "phone-number-id",
        }

        with patch.dict(os.environ, env, clear=False), redirect_stdout(output):
            response = self.client.post(
                "/whatsapp/webhook", json=self.message_payload()
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("HTTP 400", output.getvalue())
        self.assertIn("[REDACTED]", output.getvalue())
        self.assertNotIn("meta-access-token", output.getvalue())

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
