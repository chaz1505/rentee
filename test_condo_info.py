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
        self.assertIn("one call", args["instructions"])
        self.assertIn("ordinary condo knowledge", args["instructions"])
        self.assertIn("genuinely asks", tool["description"])
        self.assertIn("correction", tool["description"])

        property_tool = next(
            item for item in args["tools"]
            if item.get("name") == "get_property_details"
        )
        self.assertIn("specific current Rentee listing or unit", property_tool["description"])
        self.assertIn("general development", property_tool["description"])

    @patch("app.load_front_door_renter_context", return_value="No stored preferences yet.")
    @patch("app.get_condo_infos")
    def test_chat_stream_executes_condo_tool_and_continues_same_response(
        self, mocked_condo_infos, _mocked_summary
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
            FakeStream(initial), FakeStream(final, [final_event])
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
        self.assertIn("Checking condo information", body)
        self.assertIn("Comparison answer", body)
        continuation = responses.stream.call_args_list[1].kwargs
        self.assertEqual(continuation["previous_response_id"], "initial-response")
        self.assertEqual(
            continuation["input"][0]["call_id"], "condo-call"
        )
        self.assertEqual(
            continuation["input"][0]["output"], mocked_condo_infos.return_value
        )

    @patch("app.load_front_door_renter_context", return_value="Pets: two cats")
    @patch("app.stream_match_lead")
    @patch("app.update_preferences", return_value="Saved your cat preference.")
    def test_preference_only_update_does_not_rematch_or_leak_selection_text(
        self, _mocked_update, mocked_match, _mocked_summary
    ):
        tool_call = SimpleNamespace(
            type="function_call", name="update_preferences", call_id="update-call",
            arguments=json.dumps({
                "preference_update": "Two cats",
                "recommendations_requested": False
            })
        )
        initial = SimpleNamespace(
            id="initial-response", output=[tool_call], usage=None,
            output_text="Now calling match_lead to fetch current property matches..."
        )
        final = SimpleNamespace(id="final-response", output=[], usage=None)

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        leaked_event = SimpleNamespace(
            type="response.output_text.delta",
            delta="Now calling match_lead to fetch current property matches..."
        )
        final_event = SimpleNamespace(
            type="response.output_text.delta", delta="Saved your cat preference."
        )
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial, [leaked_event]), FakeStream(final, [final_event])
        ]

        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={"message": "We also have two cats.", "folio_id": "folio-1"}
            )
            body = response.get_data(as_text=True)
        mocked_match.assert_not_called()
        _mocked_update.assert_called_once_with(
            "folio-1", "Two cats", "live"
        )
        self.assertNotIn("Now calling match_lead", body)
        self.assertIn("Saved your cat preference.", body)
        self.assertEqual(responses.stream.call_args_list[1].kwargs["tools"], [])

    def test_update_preferences_schema_distinguishes_recommendation_request(self):
        args = app_module.build_response_args("TTDI please; recommend something now")
        tool = next(item for item in args["tools"] if item.get("name") == "update_preferences")
        self.assertIn("recommendations_requested", tool["parameters"]["properties"])
        self.assertIn("recommendations_requested", tool["parameters"]["required"])
        self.assertFalse(args["parallel_tool_calls"])

    @patch("app.load_front_door_renter_context", return_value="Complete renter brief")
    @patch("app.stream_match_lead")
    def test_current_match_answer_is_sent_directly_while_continuity_is_finalized(
        self, mocked_match, _mocked_summary
    ):
        def grounded_match(*_args):
            yield "Searching available properties..."
            return "Given what you’ve told me:\n\n- Grounded current option"

        mocked_match.side_effect = grounded_match
        tool_call = SimpleNamespace(
            type="function_call", name="match_lead", call_id="match-call",
            arguments="{}"
        )
        initial = SimpleNamespace(
            id="initial-response", output=[tool_call], usage=None, output_text=""
        )
        final = SimpleNamespace(
            id="final-response", output=[], usage=None, output_text=""
        )

        class FakeStream:
            def __init__(self, response, events=()):
                self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        rewritten_event = SimpleNamespace(
            type="response.output_text.delta",
            delta="A delayed model rewrite that must not be shown"
        )
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial), FakeStream(final, [rewritten_event])
        ]

        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={"message": "Show me my best matches", "folio_id": "folio-1"}
            )
            body = response.get_data(as_text=True)

        self.assertIn("Grounded current option", body)
        self.assertNotIn("delayed model rewrite", body)
        self.assertIn('"response_id": "final-response"', body)
        mocked_match.assert_called_once_with(
            "folio-1", "live", "Show me my best matches"
        )
        continuation = responses.stream.call_args_list[1].kwargs
        self.assertEqual(continuation["previous_response_id"], "initial-response")
        self.assertEqual(continuation["input"][0]["call_id"], "match-call")
        self.assertIn("Grounded current option", continuation["input"][0]["output"])

    def test_additive_preference_merge_preserves_old_and_new_constraints(self):
        existing = (
            "Bedrooms: 3 or 4\n"
            "Budget: maximum RM7,800\n"
            "Pets: two cats"
        )
        generated = "Area: TTDI"
        merged = app_module.merge_updated_preference_text(
            existing, generated, "Avoid a unit looking over a car park"
        )
        self.assertIn(existing, merged)
        self.assertIn("Avoid a unit looking over a car park", merged)

    def test_additive_preference_merge_removes_stale_empty_area_placeholder(self):
        merged = app_module.merge_updated_preference_text(
            "Bedrooms: 3 or 4\nAreas: No specific neighbourhoods given",
            "Preferred area: TTDI",
            "TTDI would be my favourite area if it works for school"
        )
        self.assertIn("Bedrooms: 3 or 4", merged)
        self.assertIn("TTDI would be my favourite area", merged)
        self.assertNotIn("No specific neighbourhoods given", merged)

    def test_replacement_preference_can_use_generated_profile(self):
        merged = app_module.merge_updated_preference_text(
            "Budget: RM7,800",
            "Budget: RM9,000",
            "My budget is now RM9,000"
        )
        self.assertNotIn("Budget: RM7,800", merged)
        self.assertIn("Budget: RM9,000", merged)

    @patch("app.update_lead_ai_searchtext")
    @patch("app.bubble")
    def test_additive_update_preserves_profile_and_generates_clean_summary(
        self, mocked_bubble, mocked_update_lead
    ):
        mocked_bubble.side_effect = [
            {"lead": "lead-1"},
            {
                "AIsearchtext": "Bedrooms: 3 or 4",
                "AIsearchsummary": "3 or 4 bedrooms"
            }
        ]
        fake_client = MagicMock()
        fake_client.responses.create.return_value = SimpleNamespace(
            output_text=json.dumps({
                "ai_search_summary": "Bedrooms: 3 or 4\nPets: two cats"
            })
        )
        with patch.object(app_module, "client", fake_client):
            confirmation = app_module.update_preferences(
                "folio-1", "Two cats", "development"
            )

        fake_client.responses.create.assert_called_once()
        mocked_update_lead.assert_called_once()
        updated_text = mocked_update_lead.call_args.args[1]
        updated_summary = mocked_update_lead.call_args.args[2]
        self.assertIn("Bedrooms: 3 or 4", updated_text)
        self.assertIn("Two cats", updated_text)
        self.assertEqual(updated_summary, "Bedrooms: 3 or 4\nPets: two cats")
        self.assertIn("Two cats", confirmation)

    def test_replacement_detection_keeps_model_rewrite_path_available(self):
        self.assertTrue(
            app_module.preference_update_requires_rewrite(
                "My budget is now RM9,000 instead"
            )
        )
        self.assertFalse(
            app_module.preference_update_requires_rewrite(
                "We also have two cats"
            )
        )

    @patch("app.load_front_door_renter_context", return_value="Complete renter brief")
    @patch("app.stream_match_lead")
    @patch("app.update_preferences", return_value="Saved.")
    def test_combined_update_persists_structured_preference_before_rematch(
        self, mocked_update, mocked_match, _mocked_summary
    ):
        customer_message = (
            "Please avoid units looking over a car park; show me suitable options."
        )
        tool_call = SimpleNamespace(
            type="function_call", name="update_preferences", call_id="update-call",
            arguments=json.dumps({
                "preference_update": "Avoid units overlooking a car park.",
                "recommendations_requested": True
            })
        )
        initial = SimpleNamespace(
            id="initial-response", output=[tool_call], usage=None, output_text=""
        )
        final = SimpleNamespace(id="final-response", output=[], usage=None)

        class FakeStream:
            def __init__(self, response): self.response = response
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(())
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [FakeStream(initial), FakeStream(final)]
        mocked_match.return_value = iter(())

        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post(
                "/chat_stream",
                json={"message": customer_message, "folio_id": "folio-1"}
            )
            response.get_data(as_text=True)

        mocked_update.assert_called_once_with(
            "folio-1", "Avoid units overlooking a car park.", "live"
        )
        self.assertNotIn(
            "show me suitable options",
            mocked_update.call_args.args[1].lower(),
        )
        mocked_match.assert_called_once_with(
            "folio-1", "live", customer_message
        )


if __name__ == "__main__":
    unittest.main()
