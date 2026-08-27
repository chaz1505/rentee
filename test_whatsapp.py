import json
import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "verify-me")
os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "wa-token")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "phone-number-id")

import app as app_module


def webhook_payload(message_id="wamid.1", phone="60123456789", text="Hi"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "contacts": [{"wa_id": phone, "profile": {"name": "Aisha"}}],
            "messages": [{
                "from": phone, "id": message_id, "timestamp": "1787839570",
                "type": "text", "text": {"body": text},
            }],
        }}]}],
    }


class ImmediateThread:
    def __init__(self, target, args=(), **_kwargs):
        self.target, self.args = target, args
    def start(self):
        self.target(*self.args)


class WhatsAppTests(unittest.TestCase):
    def setUp(self):
        app_module._whatsapp_processing_ids.clear()
        app_module._whatsapp_phone_locks.clear()

    def test_webhook_verification_accepts_valid_token_and_rejects_invalid(self):
        client = app_module.app.test_client()
        valid = client.get("/whatsapp/webhook", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "verify-me",
            "hub.challenge": "challenge-123",
        })
        invalid = client.get("/whatsapp/webhook", query_string={
            "hub.mode": "subscribe", "hub.verify_token": "wrong",
            "hub.challenge": "challenge-123",
        })
        self.assertEqual(valid.status_code, 200)
        self.assertEqual(valid.get_data(as_text=True), "challenge-123")
        self.assertEqual(invalid.status_code, 403)

    def test_phone_normalization_equates_common_formats(self):
        values = ["+60123456789", "60123456789", "60 12 345 6789", "+60-12-345-6789"]
        self.assertEqual({app_module.normalize_phone(value) for value in values}, {"60123456789"})

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    def test_new_whatsapp_phone_creates_one_lead(self, _mocked_find, mocked_create):
        lead, created = app_module.find_or_create_whatsapp_lead("+60 12-345-6789", "Aisha")
        self.assertTrue(created)
        self.assertEqual(lead["_id"], "lead-new")
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "lead", {"phone": "60123456789"}
        )

    @patch("app._bubble_create")
    @patch("app.find_lead_by_phone")
    def test_existing_whatsapp_phone_reuses_lead(self, mocked_find, mocked_create):
        mocked_find.return_value = {"_id": "lead-existing", "phone": "+60 12 345 6789"}
        lead, created = app_module.find_or_create_whatsapp_lead("60123456789")
        self.assertFalse(created)
        self.assertEqual(lead["_id"], "lead-existing")
        mocked_create.assert_not_called()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_status_callback_is_ignored(self, mocked_process):
        payload = {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": {"statuses": [{"status": "read"}]}}]}],
        }
        response = app_module.app.test_client().post("/whatsapp/webhook", json=payload)
        self.assertEqual(response.status_code, 200)
        mocked_process.assert_not_called()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_text_webhook_acknowledges_and_dispatches_customer_text(self, mocked_process):
        response = app_module.app.test_client().post(
            "/whatsapp/webhook", json=webhook_payload()
        )
        self.assertEqual(response.status_code, 200)
        message = mocked_process.call_args.args[0]
        self.assertEqual(message["from"], "60123456789")
        self.assertEqual(message["text"]["body"], "Hi")
        self.assertEqual(message["customer_name"], "Aisha")

    @patch("app._persist_whatsapp_state")
    @patch("app.bubble")
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    def test_message_reuses_lead_context_and_sends_only_clean_final_answer(
        self, mocked_lead, _mocked_folio, mocked_turn, mocked_send,
        mocked_bubble, mocked_persist
    ):
        lead = {
            "_id": "lead-1", "phone": "60123456789",
            "searchBriefJSON": json.dumps({
                "channel_state": {"previous_response_id": "response-1"}
            }),
        }
        mocked_lead.return_value = (lead, False)
        mocked_turn.return_value = ("Here are two suitable Bangsar homes.", "response-2")
        mocked_bubble.return_value = lead

        app_module._process_whatsapp_message(
            webhook_payload(text="Find me a 3-bed in Bangsar")["entry"][0]["changes"][0]["value"]["messages"][0]
        )

        mocked_turn.assert_called_once_with(
            "Find me a 3-bed in Bangsar", "folio-1",
            previous_response_id="response-1", message_id=None, bubble_env="live",
        )
        mocked_send.assert_called_once_with(
            "60123456789", "Here are two suitable Bangsar homes."
        )
        self.assertEqual(mocked_persist.call_count, 2)
        outgoing = mocked_send.call_args.args[1]
        for leaked in ("function_call", "# to=functions", "match_lead tool",
                       "advance_property_search", "awaiting results"):
            self.assertNotIn(leaked, outgoing)

    @patch("app._persist_whatsapp_state")
    @patch("app.find_or_create_whatsapp_lead")
    def test_duplicate_message_does_not_run_ai_or_send_again(self, mocked_lead, _persist):
        mocked_lead.return_value = ({
            "_id": "lead-1", "phone": "60123456789",
            "searchBriefJSON": json.dumps({
                "channel_state": {"processed_message_ids": ["wamid.1"]}
            }),
        }, False)
        with patch("app.run_rentee_turn") as mocked_turn, patch(
            "app.send_whatsapp_text"
        ) as mocked_send:
            item = webhook_payload()["entry"][0]["changes"][0]["value"]["messages"][0]
            app_module._process_whatsapp_message(item)
        mocked_turn.assert_not_called()
        mocked_send.assert_not_called()

    @patch("app.bubble")
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    def test_two_messages_reuse_persisted_openai_continuation(
        self, mocked_lead, _folio, mocked_turn, _send, mocked_bubble
    ):
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_lead.return_value = (lead, False)
        mocked_bubble.return_value = lead
        mocked_turn.side_effect = [("First reply", "response-1"),
                                   ("Second reply", "response-2")]

        def persist(_lead_id, current_lead, channel_state, _env):
            state = app_module.load_search_state(current_lead.get("searchBriefJSON"))
            state["channel_state"] = dict(channel_state)
            current_lead["searchBriefJSON"] = app_module.dump_search_state(state)

        first = webhook_payload("wamid.1", text="Looking in Bangsar")
        second = webhook_payload("wamid.2", text="Budget is 12k")
        with patch("app._persist_whatsapp_state", side_effect=persist):
            app_module._process_whatsapp_message(
                first["entry"][0]["changes"][0]["value"]["messages"][0])
            app_module._process_whatsapp_message(
                second["entry"][0]["changes"][0]["value"]["messages"][0])

        self.assertIsNone(mocked_turn.call_args_list[0].kwargs["previous_response_id"])
        self.assertEqual(
            mocked_turn.call_args_list[1].kwargs["previous_response_id"], "response-1"
        )

    @patch("app.requests.post")
    def test_send_whatsapp_uses_meta_text_endpoint_and_splits_only_long_text(self, mocked_post):
        mocked_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"messages": [{"id": "wamid.out"}]},
        )
        mocked_post.return_value.raise_for_status.return_value = None
        ids = app_module.send_whatsapp_text("+60123456789", "Hello from Rentee")
        self.assertEqual(ids, ["wamid.out"])
        self.assertEqual(mocked_post.call_count, 1)
        self.assertEqual(mocked_post.call_args.kwargs["json"]["to"], "60123456789")
        self.assertEqual(mocked_post.call_args.kwargs["json"]["text"]["body"], "Hello from Rentee")


if __name__ == "__main__":
    unittest.main()
