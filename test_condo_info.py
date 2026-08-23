import os
import json
import unittest
from types import SimpleNamespace
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

    @patch("app.requests.get")
    def test_multi_condo_tool_output_returns_found_and_not_found_rows(self, mocked_get):
        mocked_get.return_value = self.response()

        result = json.loads(app_module.get_condo_infos([
            "Ken Bangsar", "One Menerung", "Unknown Condo"
        ]))

        self.assertEqual(len(result["condos"]), 3)
        self.assertTrue(result["condos"][0]["found"])
        self.assertEqual(result["condos"][0]["data"]["Persona"], "Family persona")
        self.assertIn("Future Column", result["condos"][0]["data"])
        self.assertTrue(result["condos"][1]["found"])
        self.assertFalse(result["condos"][2]["found"])
        mocked_get.assert_called_once()

    def test_response_tool_schema_accepts_condo_name_array(self):
        args = app_module.build_response_args("Compare Ken Bangsar and One Menerung")
        tool = next(
            item for item in args["tools"]
            if item.get("name") == "get_condo_info"
        )
        condo_names = tool["parameters"]["properties"]["condo_names"]
        self.assertEqual(condo_names["type"], "array")
        self.assertEqual(condo_names["items"], {"type": "string"})
        self.assertEqual(tool["parameters"]["required"], ["condo_names"])
        self.assertIn("Persona", args["instructions"])
        self.assertIn("one tool call", args["instructions"])
        self.assertIn("call the tool immediately", args["instructions"])
        self.assertIn(
            "Do not output any customer-facing text before making the tool call",
            args["instructions"],
        )
        self.assertIn("MUST call get_condo_info first", args["instructions"])
        self.assertIn("primary source", args["instructions"])
        self.assertIn("Do not use web search instead", args["instructions"])
        self.assertIn("MUST be called first", tool["description"])

        property_tool = next(
            item for item in args["tools"]
            if item.get("name") == "get_property_details"
        )
        self.assertIn("specific current Rentee listing or unit", property_tool["description"])
        self.assertIn("use get_condo_info instead", property_tool["description"])

        match_tool = next(
            item for item in args["tools"] if item.get("name") == "match_lead"
        )
        self.assertTrue(match_tool["strict"])
        self.assertEqual(match_tool["parameters"]["required"], [])
        self.assertFalse(match_tool["parameters"]["additionalProperties"])

    @patch("app.get_condo_infos")
    def test_chat_stream_executes_condo_tool_and_continues_same_response(
        self, mocked_condo_infos
    ):
        requested_names = ["Ken Bangsar", "One Menerung"]
        mocked_condo_infos.return_value = json.dumps({"condos": []})
        tool_call = SimpleNamespace(
            type="function_call",
            name="get_condo_info",
            call_id="condo-call",
            arguments=json.dumps({"condo_names": requested_names})
        )
        initial = SimpleNamespace(id="initial-response", output=[tool_call], usage=None)
        final = SimpleNamespace(id="final-response", output=[], usage=None)

        class FakeStream:
            def __init__(self, response, events=()):
                self.response = response
                self.events = events

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def __iter__(self):
                return iter(self.events)

            def get_final_response(self):
                return self.response

        final_event = SimpleNamespace(
            type="response.output_text.delta", delta="Comparison answer"
        )
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial),
            FakeStream(final, [final_event]),
        ]
        fake_client = SimpleNamespace(responses=responses)

        with patch.object(app_module, "client", fake_client):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={"message": "Compare them", "previous_response_id": None}
            )
            body = response.get_data(as_text=True)

        self.assertEqual(response.status_code, 200)
        mocked_condo_infos.assert_called_once_with(requested_names)
        self.assertIn("Comparison answer", body)
        self.assertIn('"response_id": "final-response"', body)
        self.assertIn('"done": true', body)
        continuation = responses.stream.call_args_list[1].kwargs
        self.assertEqual(continuation["previous_response_id"], "initial-response")
        self.assertEqual(
            continuation["input"][0]["call_id"], "condo-call"
        )
        self.assertEqual(
            continuation["input"][0]["output"], mocked_condo_infos.return_value
        )

    def test_chat_stream_emits_each_initial_delta_when_no_tool_is_called(self):
        initial = SimpleNamespace(
            id="no-tool-response", output=[], usage=None
        )

        class FakeStream:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self):
                return iter([
                    SimpleNamespace(
                        type="response.output_text.delta", delta="Hello "
                    ),
                    SimpleNamespace(
                        type="response.output_text.delta", delta="there."
                    ),
                ])
            def get_final_response(self): return initial

        responses = MagicMock()
        responses.stream.return_value = FakeStream()

        with patch.object(
            app_module, "client", SimpleNamespace(responses=responses)
        ):
            response = app_module.app.test_client().post(
                "/chat_stream", json={"message": "Hello"}
            )
            body = response.get_data(as_text=True)

        self.assertEqual(responses.stream.call_count, 1)
        self.assertIn('data: {"delta": "Hello "}\n\n', body)
        self.assertIn('data: {"delta": "there."}\n\n', body)
        self.assertNotIn('"delta": "Hello there."', body)
        self.assertIn('"response_id": "no-tool-response"', body)
        self.assertIn('"done": true', body)

    @patch("app.get_condo_infos", return_value=json.dumps({"condos": []}))
    def test_broken_previous_response_retry_suppresses_tool_selection_text(
        self, _mocked_condo_infos
    ):
        tool_call = SimpleNamespace(
            type="function_call",
            name="get_condo_info",
            call_id="retry-condo-call",
            arguments=json.dumps({"condo_names": ["One Menerung"]}),
        )
        retry_initial = SimpleNamespace(
            id="retry-initial", output=[tool_call], usage=None
        )
        final = SimpleNamespace(id="retry-final", output=[], usage=None)

        class BrokenStream:
            def __enter__(self):
                raise RuntimeError("No tool output found for function call")
            def __exit__(self, *_args): return None

        class FakeStream:
            def __init__(self, response, events):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        retry_leak = SimpleNamespace(
            type="response.output_text.delta",
            delta="I will now invoke an internal function",
        )
        final_delta = SimpleNamespace(
            type="response.output_text.delta", delta="Grounded condo answer"
        )
        responses = MagicMock()
        responses.stream.side_effect = [
            BrokenStream(),
            FakeStream(retry_initial, [retry_leak]),
            FakeStream(final, [final_delta]),
        ]

        with patch.object(
            app_module, "client", SimpleNamespace(responses=responses)
        ), patch("builtins.print") as mocked_print:
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={
                    "message": "What about One Menerung?",
                    "previous_response_id": "broken-response",
                },
            )
            body = response.get_data(as_text=True)

        self.assertEqual(responses.stream.call_count, 3)
        self.assertEqual(
            responses.stream.call_args_list[0].kwargs["previous_response_id"],
            "broken-response",
        )
        self.assertNotIn(
            "previous_response_id", responses.stream.call_args_list[1].kwargs
        )
        self.assertNotIn("invoke an internal function", body)
        self.assertTrue(any(
            "WARNING: Suppressed initial OpenAI text" in str(call)
            for call in mocked_print.call_args_list
        ))
        self.assertIn("Grounded condo answer", body)
        self.assertIn('"response_id": "retry-final"', body)

    @patch("app.execute_match_lead_silently")
    def test_match_lead_hides_internal_events_and_executes_completed_call_once(
        self, mocked_match
    ):
        mocked_match.return_value = "One matching listing"
        tool_call = SimpleNamespace(
            type="function_call", name="match_lead",
            call_id="match-call", arguments="{}",
        )
        initial = SimpleNamespace(id="match-initial", output=[tool_call], usage=None)
        final = SimpleNamespace(id="match-final", output=[], usage=None)

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        internal_events = [
            SimpleNamespace(
                type="response.output_text.delta",
                delta="Calling match_lead tool; awaiting results. # to=functions.match_lead {}",
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta", delta='{"":',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta", delta='""}',
            ),
            SimpleNamespace(type="response.reasoning.delta", delta="internal"),
        ]
        final_events = [SimpleNamespace(
            type="response.output_text.delta",
            delta="I found one suitable option at One Menerung.",
        )]
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial, internal_events),
            FakeStream(final, final_events),
        ]

        with patch.object(
            app_module, "client", SimpleNamespace(responses=responses)
        ):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={
                    "message": "Yes please", "folio_id": "folio-1",
                    "message_id": "message-1",
                },
            )
            body = response.get_data(as_text=True)

        mocked_match.assert_called_once_with(
            "folio-1", "live", "message-1"
        )
        self.assertIn("I found one suitable option at One Menerung.", body)
        self.assertIn('"response_id": "match-final"', body)
        for leaked in (
            "# to=functions", "match_lead tool was called", "function_call",
            "awaiting results", "Searching available properties", '{"": ""}',
            "response.reasoning.delta",
        ):
            self.assertNotIn(leaked, body)
        continuation = responses.stream.call_args_list[1].kwargs
        self.assertEqual(continuation["input"][0]["call_id"], "match-call")
        self.assertEqual(continuation["input"][0]["output"], "One matching listing")

    def test_match_lead_rejects_invalid_nonempty_arguments(self):
        tool_call = SimpleNamespace(
            type="function_call", name="match_lead", arguments='{"":""}'
        )
        with self.assertRaisesRegex(ValueError, "does not accept arguments"):
            app_module.parse_completed_tool_arguments(tool_call)

    def test_invalid_tool_arguments_are_logged_but_not_exposed_in_sse(self):
        tool_call = SimpleNamespace(
            type="function_call", name="match_lead", call_id="bad-call",
            arguments='{"":""}',
        )
        initial = SimpleNamespace(id="bad-initial", output=[tool_call], usage=None)

        class FakeStream:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(())
            def get_final_response(self): return initial

        responses = MagicMock()
        responses.stream.return_value = FakeStream()
        with patch.object(
            app_module, "client", SimpleNamespace(responses=responses)
        ), patch("builtins.print") as mocked_print:
            response = app_module.app.test_client().post(
                "/chat_stream", json={"message": "Yes please"}
            )
            body = response.get_data(as_text=True)

        self.assertIn("Sorry, something went wrong", body)
        self.assertNotIn("match_lead", body)
        self.assertNotIn('{\"\":\"\"}', body)
        self.assertTrue(any(
            "match_lead does not accept arguments" in str(call)
            for call in mocked_print.call_args_list
        ))


if __name__ == "__main__":
    unittest.main()
