import json
import os
import unittest
import requests
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
        app_module._whatsapp_processed_ids.clear()
        app_module._whatsapp_processed_order.clear()
        app_module._whatsapp_phone_locks.clear()
        self.internal_user_patcher = patch("app.find_internal_user", return_value=None)
        self.mocked_internal_user = self.internal_user_patcher.start()
        self.addCleanup(self.internal_user_patcher.stop)

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

    @patch("app.requests.get")
    def test_bubble_get_authenticates_and_preserves_caller_headers(self, mocked_get):
        mocked_get.return_value.json.return_value = {"response": {"ok": True}}
        mocked_get.return_value.raise_for_status.return_value = None
        result = app_module.bubble(
            "https://bubble.test/api/1.1/obj/lead",
            headers={
                "X-Request-Source": "whatsapp",
                "Authorization": "Bearer caller-must-not-replace-server-token",
            },
            params={"cursor": 0},
        )
        self.assertEqual(result, {"ok": True})
        headers = mocked_get.call_args.kwargs["headers"]
        self.assertEqual(
            headers["Authorization"], f"Bearer {app_module.BUBBLE_API_TOKEN}"
        )
        self.assertEqual(headers["X-Request-Source"], "whatsapp")
        self.assertEqual(mocked_get.call_args.kwargs["params"], {"cursor": 0})

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble")
    def test_new_whatsapp_phone_creates_one_lead(
        self, mocked_bubble, _mocked_find, mocked_create
    ):
        mocked_bubble.return_value = {
            "_id": "lead-new", "phone": "60123456789", "searchBriefJSON": ""
        }
        lead, created = app_module.find_or_create_whatsapp_lead("+60 12-345-6789", "Aisha")
        self.assertTrue(created)
        self.assertEqual(lead["_id"], "lead-new")
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "lead", {"phone": "60123456789"}
        )

    @patch("app._bubble_create", return_value="lead-new")
    @patch("app.find_lead_by_phone", return_value=None)
    @patch("app.bubble", return_value={"_id": "lead-new"})
    def test_new_lead_continues_when_bubble_hides_phone_on_read(
        self, _mocked_bubble, _mocked_find, _mocked_create
    ):
        with patch("builtins.print") as mocked_print:
            lead, created = app_module.find_or_create_whatsapp_lead("60123456789")
        self.assertTrue(created)
        self.assertEqual(lead["_id"], "lead-new")
        self.assertEqual(lead["phone"], "60123456789")
        self.assertEqual(lead["searchBriefJSON"], "")
        self.assertIn(
            "phone is not readable",
            "\n".join(str(call) for call in mocked_print.call_args_list),
        )

    @patch("app.bubble")
    def test_exact_phone_constraint_reuses_lead_when_phone_is_hidden(self, mocked_bubble):
        mocked_bubble.side_effect = [
            {"results": [{"_id": "lead-existing"}], "remaining": 0},
            {"_id": "lead-existing", "searchBriefJSON": ""},
        ]
        lead = app_module.find_lead_by_phone("60123456789")
        self.assertEqual(lead["_id"], "lead-existing")
        self.assertEqual(lead["phone"], "60123456789")

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

    @patch("app.requests.post")
    def test_typing_indicator_uses_inbound_wamid_and_native_meta_payload(
        self, mocked_post
    ):
        mocked_post.return_value.status_code = 200
        mocked_post.return_value.raise_for_status.return_value = None

        app_module.send_whatsapp_typing_indicator("wamid.inbound-1")

        mocked_post.assert_called_once_with(
            "https://graph.facebook.com/v23.0/phone-number-id/messages",
            headers={
                "Authorization": "Bearer wa-token",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": "wamid.inbound-1",
                "typing_indicator": {"type": "text"},
            },
            timeout=30,
        )

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app._process_whatsapp_message")
    def test_duplicate_webhook_dispatches_message_only_once(self, mocked_process):
        client = app_module.app.test_client()
        payload = webhook_payload(message_id="wamid.duplicate")

        first = client.post("/whatsapp/webhook", json=payload)
        second = client.post("/whatsapp/webhook", json=payload)

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        mocked_process.assert_called_once()

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

    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_internal_user_handled_message_skips_external_lead_flow(
        self, _typing, mocked_workflow, mocked_send, mocked_lead
    ):
        self.mocked_internal_user.return_value = {
            "_id": "user-1", "phone": "+60 12-345 6789"
        }
        result = MagicMock(
            handled=True, response_text="Sure — send me the agent enquiry."
        )
        mocked_workflow.return_value = result
        item = webhook_payload(text="new agent enquiry coming")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        mocked_workflow.assert_called_once()
        mocked_send.assert_called_once_with(
            "60123456789", "Sure — send me the agent enquiry."
        )
        result.complete.assert_called_once_with()
        mocked_lead.assert_not_called()

    @patch("app.threading.Thread", ImmediateThread)
    @patch("app.send_whatsapp_text")
    @patch("app.handle_internal_user_message")
    @patch("app.send_whatsapp_typing_indicator")
    def test_completed_duplicate_internal_webhook_is_not_consumed_twice(
        self, _typing, mocked_workflow, mocked_send
    ):
        self.mocked_internal_user.return_value = {
            "_id": "user-1", "phone": "60123456789"
        }
        result = MagicMock(
            handled=True,
            response_text="Got it — I've received the agent enquiry.",
        )
        mocked_workflow.return_value = result
        client = app_module.app.test_client()
        payload = webhook_payload(message_id="wamid.forwarded-enquiry")

        client.post("/whatsapp/webhook", json=payload)
        client.post("/whatsapp/webhook", json=payload)

        mocked_workflow.assert_called_once()
        mocked_send.assert_called_once()
        result.complete.assert_called_once_with()

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-1")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn", return_value=("Normal reply", "response-1", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead", return_value=({"_id": "lead-1"}, False))
    @patch("app.send_whatsapp_typing_indicator")
    def test_unknown_internal_user_falls_through_to_existing_whatsapp_flow(
        self, _typing, mocked_lead, _folio, mocked_turn, mocked_send,
        _latest, _create, _save
    ):
        item = webhook_payload(text="Find me a home")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        self.mocked_internal_user.assert_called_once()
        mocked_lead.assert_called_once()
        mocked_turn.assert_called_once()
        mocked_send.assert_called_once_with("60123456789", "Normal reply")

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="bubble-message-2")
    @patch("app.find_latest_ai_message")
    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_recommendation_batch")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_typing_indicator")
    def test_message_reuses_lead_context_and_sends_only_clean_final_answer(
        self, mocked_typing, mocked_lead, _mocked_folio, mocked_turn, mocked_batch,
        mocked_send, mocked_latest, _mocked_create, mocked_save
    ):
        events = []
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_typing.side_effect = lambda _message_id: events.append("typing")
        mocked_lead.side_effect = lambda *_args: (events.append("lead") or (lead, False))
        mocked_latest.return_value = {
            "_id": "bubble-message-1", "lead": "lead-1",
            "own_Sent?": "No", "response_ID": "response-1",
        }
        mocked_turn.return_value = (
            "Here are two suitable Bangsar homes.", "response-2", False
        )

        app_module._process_whatsapp_message(
            webhook_payload(text="Find me a 3-bed in Bangsar")["entry"][0]["changes"][0]["value"]["messages"][0]
        )

        mocked_typing.assert_called_once_with("wamid.1")
        self.assertEqual(events[:2], ["typing", "lead"])
        mocked_turn.assert_called_once_with(
            "Find me a 3-bed in Bangsar", "folio-1",
            previous_response_id="response-1",
            message_id="bubble-message-2", bubble_env="live",
        )
        mocked_send.assert_called_once_with(
            "60123456789", "Here are two suitable Bangsar homes."
        )
        mocked_batch.assert_not_called()
        mocked_save.assert_called_once_with(
            "bubble-message-2", "Here are two suitable Bangsar homes.",
            "response-2", "live",
        )
        outgoing = mocked_send.call_args.args[1]
        for leaked in ("function_call", "# to=functions", "match_lead tool",
                       "advance_property_search", "awaiting results"):
            self.assertNotIn(leaked, outgoing)

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", side_effect=[
        "message-1", "message-2", "message-3"
    ])
    @patch("app.find_latest_ai_message", side_effect=[
        None,
        {"_id": "message-1", "lead": "lead-1", "own_Sent?": "No",
         "response_ID": "response-1", "Created Date": "2026-08-28T01:00:00Z"},
        {"_id": "message-2", "lead": "lead-1", "own_Sent?": "No",
         "response_ID": "response-2", "Created Date": "2026-08-28T02:00:00Z"},
    ])
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_typing_indicator")
    def test_three_messages_reuse_latest_persisted_openai_continuation(
        self, mocked_typing, mocked_lead, _folio, mocked_turn, _send, _latest,
        _create, mocked_save
    ):
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_lead.return_value = (lead, False)
        mocked_turn.side_effect = [
            ("First reply", "response-1", False),
            ("Second reply", "response-2", False),
            ("Third reply", "response-3", False),
        ]

        first = webhook_payload("wamid.1", text="Looking in Bangsar")
        second = webhook_payload("wamid.2", text="Budget is 12k")
        third = webhook_payload("wamid.3", text="Rent please")
        app_module._process_whatsapp_message(
            first["entry"][0]["changes"][0]["value"]["messages"][0])
        app_module._process_whatsapp_message(
            second["entry"][0]["changes"][0]["value"]["messages"][0])
        app_module._process_whatsapp_message(
            third["entry"][0]["changes"][0]["value"]["messages"][0])

        self.assertIsNone(mocked_turn.call_args_list[0].kwargs["previous_response_id"])
        self.assertEqual(
            mocked_turn.call_args_list[1].kwargs["previous_response_id"], "response-1"
        )
        self.assertEqual(
            mocked_turn.call_args_list[2].kwargs["previous_response_id"], "response-2"
        )
        self.assertEqual(
            _folio.call_args_list[1].args, ("lead-1", "live")
        )
        self.assertEqual(
            mocked_turn.call_args_list[0].kwargs["message_id"], "message-1"
        )
        self.assertEqual(
            mocked_turn.call_args_list[1].kwargs["message_id"], "message-2"
        )
        self.assertEqual(
            mocked_turn.call_args_list[2].kwargs["message_id"], "message-3"
        )
        self.assertEqual(mocked_save.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in mocked_typing.call_args_list],
            ["wamid.1", "wamid.2", "wamid.3"],
        )

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="message-current")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.run_rentee_turn", return_value=("Reply", "resp-1", False))
    @patch("app.find_or_create_lead_folio", return_value=("folio-1", False))
    @patch("app.find_or_create_whatsapp_lead", return_value=({"_id": "lead-1"}, False))
    @patch(
        "app.send_whatsapp_typing_indicator",
        side_effect=requests.RequestException("Meta unavailable"),
    )
    def test_typing_indicator_failure_does_not_block_customer_reply(
        self, mocked_typing, _lead, _folio, mocked_turn, mocked_send, _latest,
        _create, mocked_save
    ):
        item = webhook_payload(message_id="wamid.failure")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        mocked_typing.assert_called_once_with("wamid.failure")
        mocked_turn.assert_called_once()
        mocked_save.assert_called_once()
        mocked_send.assert_called_once_with("60123456789", "Reply")

    @patch("app._bubble_records")
    def test_response_continuity_survives_python_memory_restart(self, mocked_records):
        mocked_records.return_value = iter([{
            "_id": "message-previous", "lead": "lead-a", "own_Sent?": "No",
            "response_ID": "resp-durable", "Created Date": "2026-08-28T03:00:00Z",
        }])
        app_module._whatsapp_processing_ids.clear()
        app_module._whatsapp_phone_locks.clear()
        previous = app_module.find_latest_ai_message("lead-a")
        self.assertEqual(previous["response_ID"], "resp-durable")

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

    def test_listing_image_prefers_cover_photo(self):
        self.assertEqual(
            app_module.get_listing_whatsapp_image({
                "coverPhoto": "https://cdn.test/cover.jpg",
                "photos": ["https://cdn.test/first.jpg"],
            }),
            "https://cdn.test/cover.jpg",
        )

    def test_listing_image_falls_back_to_first_photo(self):
        self.assertEqual(
            app_module.get_listing_whatsapp_image({
                "photos": ["https://cdn.test/first.jpg", "https://cdn.test/second.jpg"]
            }),
            "https://cdn.test/first.jpg",
        )

    def test_listing_image_missing_or_empty_photos_returns_none(self):
        for listing in ({}, {"photos": None}, {"photos": []}):
            with self.subTest(listing=listing):
                self.assertEqual(
                    app_module.get_listing_whatsapp_image(listing), None
                )

    def test_listing_image_malformed_cover_uses_first_valid_photo(self):
        self.assertEqual(
            app_module.get_listing_whatsapp_image({
                "coverPhoto": "not a URL",
                "photos": ["//cdn.test/first.jpg"],
            }),
            "https://cdn.test/first.jpg",
        )

    @patch("app.requests.post")
    def test_send_whatsapp_image_uses_meta_image_payload(self, mocked_post):
        mocked_post.return_value.status_code = 200
        mocked_post.return_value.raise_for_status.return_value = None
        app_module.send_whatsapp_image("+60 12 345 6789", "https://cdn.test/a.jpg")
        payload = mocked_post.call_args.kwargs["json"]
        self.assertEqual(payload, {
            "messaging_product": "whatsapp",
            "to": "60123456789",
            "type": "image",
            "image": {"link": "https://cdn.test/a.jpg"},
        })

    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_image")
    def test_recommendation_batch_interleaves_images_and_text_with_cap(
        self, mocked_image, mocked_text
    ):
        events = []
        mocked_image.side_effect = lambda *_args: (
            events.append("image") or MagicMock(status_code=200)
        )
        mocked_text.side_effect = lambda *_args: events.append("text")
        listings = [{
            "listing_id": f"listing-{index}",
            "property_name": f"Home {index}",
            "coverPhoto": f"https://cdn.test/{index}.jpg",
        } for index in range(1, 7)]

        app_module.send_whatsapp_recommendation_batch(
            "60123456789", "folio-1", listings, top_count=6
        )

        self.assertEqual(mocked_image.call_count, 4)
        self.assertEqual(mocked_text.call_count, 6)  # intro + four cards + footer
        self.assertEqual(events, [
            "text", "image", "text", "image", "text",
            "image", "text", "image", "text", "text",
        ])

    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_image")
    def test_missing_and_failed_images_still_send_all_recommendation_text(
        self, mocked_image, mocked_text
    ):
        mocked_image.side_effect = requests.RequestException("Meta rejected image")
        listings = [
            {"listing_id": "a", "property_name": "A",
             "coverPhoto": "https://cdn.test/a.jpg"},
            {"listing_id": "b", "property_name": "B", "photos": []},
            {"listing_id": "c", "property_name": "C",
             "photos": ["https://cdn.test/c.jpg"]},
        ]
        with patch("builtins.print") as mocked_print:
            app_module.send_whatsapp_recommendation_batch(
                "60123456789", "folio-1", listings
            )
        self.assertEqual(mocked_image.call_count, 2)
        self.assertEqual(mocked_text.call_count, 5)  # intro + three cards + footer
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("failed", logs)
        self.assertIn("no image available", logs)

    @patch("app.bubble")
    @patch("app._bubble_records", return_value=iter([]))
    def test_exact_phone_lookup_no_match_returns_none_without_enumeration(
        self, mocked_records, mocked_bubble
    ):
        found = app_module.find_lead_by_phone("60123456789")
        self.assertIsNone(found)
        mocked_records.assert_called_once()
        constraints = mocked_records.call_args.args[2]
        self.assertEqual(constraints, [{
            "key": "phone", "constraint_type": "equals", "value": "60123456789"
        }])
        mocked_bubble.assert_not_called()

    @patch("app._bubble_records")
    def test_exact_phone_lookup_propagates_api_error_without_fallback(
        self, mocked_records
    ):
        mocked_records.side_effect = requests.RequestException("Bubble unavailable")
        with patch("builtins.print") as mocked_print, self.assertRaises(
            requests.RequestException
        ):
            app_module.find_lead_by_phone("60123456789")
        mocked_records.assert_called_once()
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("...6789", logs)
        self.assertNotIn("60123456789", logs)

    @patch("app._bubble_records")
    def test_latest_ai_message_is_lead_scoped_filtered_and_newest(self, mocked_records):
        mocked_records.return_value = iter([
            {"_id": "own", "lead": "lead-a", "own_Sent?": "Yes",
             "response_ID": "ignore-own", "Created Date": "2026-08-28T05:00:00Z"},
            {"_id": "empty", "lead": "lead-a", "own_Sent?": "No",
             "response_ID": "", "Created Date": "2026-08-28T04:00:00Z"},
            {"_id": "other", "lead": "lead-b", "own_Sent?": "No",
             "response_ID": "ignore-other", "Created Date": "2026-08-28T06:00:00Z"},
            {"_id": "older", "lead": "lead-a", "own_Sent?": "No",
             "response_ID": "resp-1", "Created Date": "2026-08-28T01:00:00Z"},
            {"_id": "newer", "lead": "lead-a", "own_Sent?": "No",
             "response_ID": "resp-2", "Created Date": "2026-08-28T02:00:00Z"},
        ])
        with patch("builtins.print") as mocked_print:
            latest = app_module.find_latest_ai_message("lead-a")
        self.assertEqual(latest["_id"], "newer")
        self.assertEqual(latest["response_ID"], "resp-2")
        args = mocked_records.call_args.args
        self.assertEqual(args[1], "message")
        constraints = args[2]
        self.assertEqual(constraints, [
            {"key": "lead", "constraint_type": "equals", "value": "lead-a"}
        ])
        self.assertEqual(len(args), 3)
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("messages_fetched=5", logs)
        self.assertIn("eligible_messages=2", logs)
        self.assertIn("own_sent_values=['No', 'Yes']", logs)

    @patch("app._bubble_create", return_value="message-current")
    def test_current_ai_message_uses_existing_bubble_semantics(self, mocked_create):
        message_id = app_module.create_whatsapp_ai_message("lead-a")
        self.assertEqual(message_id, "message-current")
        mocked_create.assert_called_once_with(
            "https://www.rentee.asia/api/1.1", "message",
            {"lead": "lead-a", "own_Sent?": "No", "messageContent": ""},
        )

    @patch("app.requests.patch")
    def test_completed_ai_message_saves_answer_and_response_id(self, mocked_patch):
        mocked_patch.return_value.raise_for_status.return_value = None
        app_module.save_whatsapp_ai_message(
            "message-current", "Final answer", "resp-2"
        )
        self.assertTrue(mocked_patch.call_args.args[0].endswith(
            "/obj/message/message-current"
        ))
        self.assertEqual(mocked_patch.call_args.kwargs["json"], {
            "messageContent": "Final answer", "response_ID": "resp-2",
        })

    def test_search_brief_has_no_whatsapp_conversation_state(self):
        state = app_module.load_search_state("")
        self.assertNotIn("channel_state", state)

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_existing_folio_is_reused_when_exact_constraint_returns_nothing(
        self, mocked_records, mocked_create
    ):
        mocked_records.side_effect = [iter([]), iter([
            {"_id": "folio-x", "lead": "lead-a", "Created Date": "2026-01-01"}
        ])]
        folio_id, created = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual((folio_id, created), ("folio-x", False))
        mocked_create.assert_not_called()

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_duplicate_folios_choose_existing_deterministically_without_creation(
        self, mocked_records, mocked_create
    ):
        mocked_records.return_value = iter([
            {"_id": "folio-b", "lead": "lead-a", "Created Date": "2026-02-01"},
            {"_id": "folio-a", "lead": "lead-a", "Created Date": "2026-01-01"},
        ])
        with patch("builtins.print") as mocked_print:
            folio_id, created = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual((folio_id, created), ("folio-a", False))
        mocked_create.assert_not_called()
        self.assertIn(
            "duplicate_folios=2",
            "\n".join(str(call) for call in mocked_print.call_args_list),
        )

    @patch("app._bubble_create")
    @patch("app._bubble_records")
    def test_folio_relationship_survives_process_memory_restart(
        self, mocked_records, mocked_create
    ):
        mocked_records.return_value = iter([{"_id": "folio-x", "lead": "lead-a"}])
        app_module._whatsapp_phone_locks.clear()
        app_module._whatsapp_processing_ids.clear()
        folio_id, created = app_module.find_or_create_lead_folio("lead-a")
        self.assertEqual((folio_id, created), ("folio-x", False))
        mocked_create.assert_not_called()

    @patch("app._bubble_create")
    @patch("app.bubble", return_value={"_id": "lead-existing", "phone": "60123456789"})
    @patch("app._bubble_records", return_value=iter([{"_id": "lead-existing"}]))
    def test_exact_lookup_makes_whatsapp_reuse_lead_without_duplicate_creation(
        self, _mocked_records, _mocked_bubble, mocked_create
    ):
        lead, created = app_module.find_or_create_whatsapp_lead("+60 12 345 6789")
        self.assertEqual(lead["_id"], "lead-existing")
        self.assertFalse(created)
        mocked_create.assert_not_called()

    def test_build_folio_url_uses_customer_folio_route(self):
        folio_id = "1787842581873x206575709934321660"
        self.assertEqual(
            app_module.build_folio_url(folio_id),
            "https://www.rentee.asia/folio3/1787842581873x206575709934321660",
        )

    @patch("app.get_current_recommendations")
    def test_recommendation_summary_shows_top_three_and_links_to_full_folio(
        self, mocked_current
    ):
        listings = []
        for index, name in enumerate(
            ("One Menerung", "The Loft", "Ken Bangsar", "Serai", "Nadi Bangsar"), 1
        ):
            listings.append({
                "listing_id": f"listing-internal-{index}",
                "condo": f"condo-internal-{index}",
                "property_name": name,
                "priceRent": 10000 + index * 500,
                "beds": 4,
                "recommendation_reason": f"Fits the customer's budget and four-bedroom requirement {index}.",
            })
        mocked_current.return_value = json.dumps({"current_recommendations": listings})

        result = app_module.build_whatsapp_recommendation_summary("folio-active")

        for name in ("One Menerung", "The Loft", "Ken Bangsar"):
            self.assertIn(name, result)
        self.assertNotIn("Serai", result)
        self.assertIn("RM10,500/month", result)
        self.assertIn("4 bed", result)
        self.assertIn("budget and four-bedroom requirement", result)
        self.assertIn("See all 5", result)
        self.assertIn("https://www.rentee.asia/folio3/folio-active", result)
        for index in range(1, 6):
            self.assertNotIn(f"listing-internal-{index}", result)
            self.assertNotIn(f"condo-internal-{index}", result)

    @patch("app.get_current_recommendations", return_value=json.dumps({
        "current_recommendations": []
    }))
    def test_empty_folio_recommendation_summary_returns_none(self, _mocked_current):
        self.assertIsNone(
            app_module.build_whatsapp_recommendation_summary("folio-empty")
        )

    @patch("app.save_whatsapp_ai_message")
    @patch("app.create_whatsapp_ai_message", return_value="bubble-message-current")
    @patch("app.find_latest_ai_message", return_value=None)
    @patch("app.send_whatsapp_text")
    @patch("app.send_whatsapp_recommendation_batch")
    @patch("app.build_whatsapp_recommendation_summary")
    @patch("app.get_current_recommendations")
    @patch("app.run_rentee_turn")
    @patch("app.find_or_create_lead_folio", return_value=("folio-active", False))
    @patch("app.find_or_create_whatsapp_lead")
    @patch("app.send_whatsapp_typing_indicator")
    def test_recommendation_turn_uses_existing_folio_and_one_coherent_text_message(
        self, _typing, mocked_lead, mocked_folio, mocked_turn, mocked_current,
        mocked_summary, mocked_batch, mocked_send, _latest, _create, mocked_save
    ):
        lead = {"_id": "lead-1", "phone": "60123456789", "searchBriefJSON": ""}
        mocked_lead.return_value = (lead, False)
        mocked_turn.return_value = ("Model recommendation", "resp-1", True)
        listings = [{"listing_id": "listing-1", "property_name": "One Menerung"}]
        mocked_current.return_value = json.dumps({"current_recommendations": listings})
        mocked_summary.return_value = (
            "Three grounded recommendations\n\n"
            "https://www.rentee.asia/folio3/folio-active"
        )
        item = webhook_payload(text="Show me the properties")["entry"][0]["changes"][0]["value"]["messages"][0]

        app_module._process_whatsapp_message(item)

        mocked_folio.assert_called_once_with("lead-1", "live")
        mocked_current.assert_called_once_with(
            "folio-active", "live", include_media=True
        )
        mocked_summary.assert_called_once_with(
            "folio-active", "live", listings=listings
        )
        mocked_batch.assert_called_once_with("60123456789", "folio-active", listings)
        mocked_send.assert_not_called()
        mocked_save.assert_called_once_with(
            "bubble-message-current", mocked_summary.return_value, "resp-1", "live"
        )


if __name__ == "__main__":
    unittest.main()
