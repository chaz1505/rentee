import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module
from search_flow import empty_search_state


class FakeStream:
    def __init__(self, response, deltas=()):
        self.response = response
        self.events = [SimpleNamespace(type="response.output_text.delta", delta=value)
                       for value in deltas]

    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def __iter__(self): return iter(self.events)
    def get_final_response(self): return self.response


def function_response(response_id, name, call_id, arguments):
    call = SimpleNamespace(
        type="function_call", name=name, call_id=call_id,
        arguments=json.dumps(arguments),
    )
    return SimpleNamespace(id=response_id, output=[call], usage=None)


def text_response(response_id):
    return SimpleNamespace(id=response_id, output=[], usage=None)


class ToolOrchestrationTests(unittest.TestCase):
    def test_location_intents_select_nearby_point_to_point_and_comparison_tools(self):
        nearby = app_module.build_response_args(
            "What supermarkets are within a 10-minute walk of 9 Beringin?"
        )
        travel = app_module.build_response_args(
            "How long does it take to walk from 9 Beringin to BSC?"
        )
        comparison = app_module.build_response_args(
            "Which of these condos has better supermarket access?"
        )
        self.assertEqual(nearby["tool_choice"], {
            "type": "function", "name": "find_nearby_places",
        })
        self.assertEqual(travel["tool_choice"], {
            "type": "function", "name": "get_travel_time",
        })
        self.assertEqual(comparison["tool_choice"], "auto")
        tools = {tool.get("name"): tool for tool in nearby["tools"] if tool.get("name")}
        self.assertEqual(
            tools["get_travel_time"]["parameters"]["properties"]["mode"]["enum"],
            ["driving", "walking"],
        )
        self.assertIn("find_nearby_places", tools)
        comparison_tools = {
            tool.get("name") for tool in comparison["tools"] if tool.get("name")
        }
        self.assertIn("find_nearby_places", comparison_tools)
        self.assertIn("compare_locations", comparison_tools)

    def test_obvious_nearby_phrases_force_nearby_tool_conservatively(self):
        nearby_messages = (
            "What shops are close to this one?",
            "What supermarkets are nearby?",
            "Any cafes around here?",
            "What groceries are within walking distance?",
            "Is there a pharmacy near this property?",
            "What is around this condo?",
        )
        for message in nearby_messages:
            with self.subTest(message=message):
                self.assertTrue(app_module._requires_nearby_places_tool(message))
                self.assertEqual(
                    app_module.build_response_args(message)["tool_choice"],
                    {"type": "function", "name": "find_nearby_places"},
                )
        for message in ("What is Bangsar like?", "Tell me about this condo.",
                        "Tell me about this property"):
            with self.subTest(message=message):
                self.assertFalse(app_module._requires_nearby_places_tool(message))
                self.assertNotEqual(
                    app_module.build_response_args(message)["tool_choice"],
                    {"type": "function", "name": "find_nearby_places"},
                )

    @patch("app.find_nearby_places", return_value=json.dumps({
        "status": "ok", "origin": {
            "resolved_name": "9 Beringin", "resolution_level": "exact_property",
        }, "places": [{"name": "Village Grocer", "duration_minutes": 8}],
    }))
    def test_exact_reply_nearby_request_forces_one_grounded_call(self, nearby):
        listing = {
            "name": "9 Beringin",
            "Address": "Jalan Beringin, Damansara Heights",
            "latitude": 3.145, "longitude": 101.658,
        }
        with patch("app.bubble", return_value=listing):
            context = app_module.whatsapp_reply_listing_context("listing-9")
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(function_response(
                "nearby-call-response", "find_nearby_places", "nearby-call", {
                    "origin": "Jalan Beringin, Damansara Heights",
                    "categories": ["shopping_mall", "supermarket", "grocery_store",
                                   "convenience_store"],
                    "travel_mode": "walking",
                },
            ), ["There are several shops around the earlier search origin."]),
            FakeStream(text_response("nearby-final"), [
                "Village Grocer is an 8-minute walk from 9 Beringin."
            ]),
        ]
        with patch("app.bubble", return_value=listing), patch.object(
                app_module, "client", SimpleNamespace(responses=responses)):
            body = app_module.app.test_client().post("/chat_stream", json={
                "message": "What shops are close to this one",
                "previous_response_id": "previous-nearby-results",
                "conversation_context": context,
                "reply_listing_id": "listing-9",
            }).get_data(as_text=True)
        nearby.assert_called_once_with(
            "9 Beringin, Jalan Beringin, Damansara Heights",
            ["shopping_mall", "supermarket", "grocery_store", "convenience_store"],
            "walking", None, None,
            origin_latitude=3.145, origin_longitude=101.658,
        )
        initial_request = responses.stream.call_args_list[0].kwargs
        self.assertEqual(initial_request["tool_choice"], {
            "type": "function", "name": "find_nearby_places",
        })
        self.assertEqual(initial_request["previous_response_id"],
                         "previous-nearby-results")
        self.assertIn("Property: 9 Beringin", initial_request["instructions"])
        self.assertNotIn("earlier search origin", body)
        self.assertIn("8-minute walk from 9 Beringin", body)

    @patch("app.find_nearby_places", return_value=json.dumps({
        "status": "ok", "places": [],
    }))
    @patch("app.bubble")
    def test_explicit_different_nearby_location_is_not_overridden(self, bubble, nearby):
        call = SimpleNamespace(
            name="find_nearby_places", call_id="nearby-bsc",
            arguments=json.dumps({
                "origin": "Bangsar Shopping Centre", "categories": ["cafe"],
                "travel_mode": "walking",
            }),
        )
        app_module.execute_chat_tool(
            call, None, "live", None,
            user_message="What shops are near Bangsar Shopping Centre?",
            reply_listing_id="listing-9",
        )
        nearby.assert_called_once_with(
            "Bangsar Shopping Centre", ["cafe"], "walking", None, None,
        )
        bubble.assert_not_called()

    @patch("app.find_nearby_places", return_value=json.dumps({
        "status": "ok", "places": [],
    }))
    def test_current_property_context_preserves_exact_name_without_reply_id(self, nearby):
        call = SimpleNamespace(
            name="find_nearby_places", call_id="nearby-current-property",
            arguments=json.dumps({
                "origin": "Jalan Beringin, Damansara Heights",
                "categories": ["shop"], "travel_mode": "walking",
            }),
        )
        app_module.execute_chat_tool(
            call, None, "live", None,
            user_message="What shops are around this one?",
            conversation_context=(
                "Current listing context:\n- Property: 9 Beringin\n"
                "- Address: Jalan Beringin, Damansara Heights"
            ),
        )
        nearby.assert_called_once_with(
            "9 Beringin, Jalan Beringin, Damansara Heights",
            ["shop"], "walking", None, None,
        )

    @patch("app.find_nearby_places", return_value=json.dumps({
        "status": "ok", "places": [],
    }))
    def test_explicit_numbered_property_name_is_preserved(self, nearby):
        call = SimpleNamespace(
            name="find_nearby_places", call_id="nearby-explicit-property",
            arguments=json.dumps({
                "origin": "Jalan Beringin, Damansara Heights",
                "categories": ["shop"], "travel_mode": "walking",
            }),
        )
        app_module.execute_chat_tool(
            call, None, "live", None,
            user_message="What shops are around 9 Beringin?",
        )
        nearby.assert_called_once_with(
            "9 Beringin, Jalan Beringin, Damansara Heights",
            ["shop"], "walking", None, None,
        )

    @patch("app.get_travel_time", return_value=json.dumps({"status": "ok"}))
    @patch("app.bubble", return_value={
        "name": "9 Beringin", "Address": "Jalan Beringin, Damansara Heights",
        "latitude": 3.145, "longitude": 101.658,
    })
    def test_reply_listing_enriches_travel_origin(self, _bubble, travel):
        call = SimpleNamespace(
            name="get_travel_time", call_id="travel-listing",
            arguments=json.dumps({
                "origin": "Jalan Beringin, Damansara Heights",
                "destination": "Alice Smith School", "mode": "driving",
            }),
        )
        app_module.execute_chat_tool(
            call, None, "live", None,
            user_message="How far is this from Alice Smith?",
            reply_listing_id="listing-9",
        )
        travel.assert_called_once_with(
            "9 Beringin, Jalan Beringin, Damansara Heights",
            "Alice Smith School", "driving",
            origin_coordinates=(3.145, 101.658), destination_coordinates=None,
        )

    @patch("app.compare_locations", return_value=json.dumps({"status": "ok"}))
    @patch("app.bubble", return_value={
        "name": "9 Beringin", "Address": "Jalan Beringin, Damansara Heights",
    })
    def test_reply_listing_enriches_comparison_candidate(self, _bubble, compare):
        call = SimpleNamespace(
            name="compare_locations", call_id="compare-listing",
            arguments=json.dumps({
                "candidate_locations": ["Jalan Beringin", "One Menerung"],
                "destinations": [{"name": "Alice Smith School"}],
            }),
        )
        app_module.execute_chat_tool(
            call, None, "live", None,
            user_message="Compare this with One Menerung for Alice Smith",
            reply_listing_id="listing-9",
        )
        compare.assert_called_once_with(
            ["9 Beringin, Jalan Beringin, Damansara Heights", "One Menerung"],
            [{"name": "Alice Smith School"}],
        )

    @patch("app.find_nearby_places", return_value=json.dumps({
        "status": "ok", "places": [{"name": "Village Grocer", "duration_minutes": 8}]
    }))
    def test_short_follow_up_executes_nearby_tool_without_confirmation(self, nearby):
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(function_response(
                "nearby-call-response", "find_nearby_places", "nearby-call", {
                    "origin": "9 Beringin", "categories": [
                        "supermarket", "grocery store", "convenience store"
                    ], "travel_mode": "walking", "max_travel_minutes": 10,
                },
            ), ["I can check that..."]),
            FakeStream(text_response("nearby-final"), [
                "Village Grocer is an 8-minute walk."
            ]),
        ]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            body = app_module.app.test_client().post("/chat_stream", json={
                "message": "10 mins", "previous_response_id": "nearby-context",
            }).get_data(as_text=True)
        nearby.assert_called_once_with(
            "9 Beringin", ["supermarket", "grocery store", "convenience store"],
            "walking", 10, None,
        )
        self.assertNotIn("I can check that", body)
        self.assertIn("8-minute walk", body)

    def test_incomplete_completed_web_search_preserves_text_id_and_citations(self):
        citation = SimpleNamespace(
            type="url_citation",
            url_citation=SimpleNamespace(
                title="Groceries near 9 Beringin", url="https://example.com/groceries"
            ),
        )
        message_item = SimpleNamespace(
            type="message",
            content=[SimpleNamespace(annotations=[citation])],
        )
        incomplete_response = SimpleNamespace(
            id="web-incomplete-response", output=[message_item],
            incomplete_details=SimpleNamespace(reason="max_output_tokens"),
        )

        class IncompleteWebStream:
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self):
                return iter([
                    SimpleNamespace(type="response.created", response=SimpleNamespace(
                        id="web-incomplete-response"
                    )),
                    SimpleNamespace(type="response.web_search_call.in_progress"),
                    SimpleNamespace(type="response.web_search_call.searching"),
                    SimpleNamespace(type="response.web_search_call.completed"),
                    SimpleNamespace(type="response.output_text.delta", delta="Nearby groceries "),
                    SimpleNamespace(type="response.output_text.delta", delta="include Village Grocer."),
                    SimpleNamespace(type="response.output_text.done"),
                    SimpleNamespace(type="response.output_item.done", item=message_item),
                    SimpleNamespace(type="response.incomplete", response=incomplete_response),
                ])
            def get_final_response(self):
                raise RuntimeError("Didn't receive a `response.completed` event.")

        responses = MagicMock()
        responses.stream.return_value = IncompleteWebStream()
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)), \
                patch("builtins.print") as mocked_print:
            body = app_module.app.test_client().post(
                "/chat_stream", json={"message": "Walking distance"}
            ).get_data(as_text=True)
        self.assertIn("Nearby groceries", body)
        self.assertIn("Village Grocer", body)
        self.assertIn('"response_id": "web-incomplete-response"', body)
        self.assertIn("https://example.com/groceries", body)
        logs = "\n".join(str(call) for call in mocked_print.call_args_list)
        self.assertIn("application_function_call_started=False", logs)
        self.assertIn("web_search_started=True", logs)
        self.assertIn("web_search_completed=True", logs)
        self.assertIn("output_text_done=True", logs)
        self.assertIn("reason=max_output_tokens", logs)
        self.assertIn("action=preserve_text", logs)

    def test_inventory_and_condo_information_routing_are_separate(self):
        inventory = app_module.build_response_args(
            "What have you got in Damansara Heights - landed ok"
        )
        self.assertEqual(inventory["tool_choice"], {
            "type": "function", "name": "advance_property_search",
        })
        with patch("app.resolve_condo_mentions", return_value=["One Menerung"]):
            condo = app_module.build_response_args("Tell me about One Menerung")
            units = app_module.build_response_args("Any units available in One Menerung?")
        self.assertEqual(condo["tool_choice"], {
            "type": "function", "name": "get_condo_info",
        })
        self.assertEqual(units["tool_choice"], {
            "type": "function", "name": "advance_property_search",
        })

    @patch("app.advance_property_search")
    @patch("app.get_condo_infos", return_value=json.dumps({"condos": []}))
    def test_wrong_first_tool_can_recover_without_buffered_text_leakage(
        self, condo_info, advance
    ):
        advance.return_value = {
            "action": "ask", "text": "Grounded search answer",
            "state": {}, "lead_id": "lead-1",
        }
        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(function_response(
                "round-1", "get_condo_info", "call-1",
                {"condo_names": ["Damansara Heights"]},
            ), ["Searching for landed listings..."]),
            FakeStream(function_response(
                "round-2", "advance_property_search", "call-2",
                {"geo_names": ["Damansara Heights"], "search_listings": False},
            ), ['{"geo_names":["Damansara Heights"],"search_listings":true}']),
            FakeStream(text_response("round-3"), ["Grounded search answer"]),
        ]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post("/chat_stream", json={
                "message": "What have you got in Damansara Heights - landed ok",
                "folio_id": "folio-1",
            })
        body = response.get_data(as_text=True)
        self.assertNotIn("Searching for landed", body)
        self.assertNotIn('\\"geo_names\\"', body)
        self.assertIn("Grounded search answer", body)
        condo_info.assert_called_once()
        advance.assert_called_once()
        second_args = responses.stream.call_args_list[1].kwargs
        self.assertEqual(second_args["previous_response_id"], "round-1")
        self.assertTrue(any(tool.get("name") == "advance_property_search"
                            for tool in second_args["tools"]))

    @patch("app.get_condo_infos", return_value=json.dumps({"condos": []}))
    def test_tool_loop_stops_safely_at_maximum(self, condo_info):
        responses = MagicMock()
        responses.stream.side_effect = [FakeStream(function_response(
            f"round-{index}", "get_condo_info", f"call-{index}",
            {"condo_names": ["One Menerung"]},
        )) for index in range(1, app_module.MAX_TOOL_ROUNDS + 2)]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            body = app_module.app.test_client().post(
                "/chat_stream", json={"message": "Tell me about One Menerung"}
            ).get_data(as_text=True)
        self.assertEqual(condo_info.call_count, app_module.MAX_TOOL_ROUNDS)
        self.assertIn("complete that request safely", body)

    def test_internal_payload_guard_is_targeted(self):
        self.assertTrue(app_module.resembles_internal_orchestration_payload(
            'Searching... {"search_listings":true,"geo_names":["Bangsar"]}'
        ))
        self.assertFalse(app_module.resembles_internal_orchestration_payload(
            '{"bedrooms":4,"area":"Bangsar"}'
        ))

    def test_final_internal_payload_is_suppressed_but_normal_json_is_preserved(self):
        for text, expected, rejected in (
            ('{"search_listings":true,"geo_names":["Bangsar"]}',
             "complete that property request safely", "search_listings"),
            ('{"bedrooms":4,"area":"Bangsar"}', 'bedrooms', None),
        ):
            with self.subTest(text=text):
                responses = MagicMock()
                responses.stream.return_value = FakeStream(text_response("final"), [text])
                with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
                    body = app_module.app.test_client().post(
                        "/chat_stream", json={"message": "test"}
                    ).get_data(as_text=True)
                self.assertIn(expected, body)
                if rejected:
                    self.assertNotIn(rejected, body)

    def test_search_area_is_not_a_regular_destination(self):
        result = app_module.remove_search_areas_from_regular_destinations(
            ["Damansara Heights", "CapSquare", "Alice Smith School"],
            ["Damansara Heights"],
        )
        self.assertEqual(result, ["CapSquare", "Alice Smith School"])

    def test_active_refinement_preserves_budget_bedrooms_and_transaction(self):
        state = empty_search_state()
        state.update({
            "areas": ["Bangsar"], "area_status": "known",
            "property_types": ["rent"], "bedroom_requirement": "4",
            "budget_requirement": "15000",
        })
        updated = app_module.apply_active_search_update(state, {
            "geo_names": ["Damansara Heights"], "area_update_mode": "replace",
            "property_types": ["landed", "terrace", "semi-detached", "bungalow"],
            "search_listings": True,
        })
        self.assertEqual(updated["areas"], ["Damansara Heights"])
        self.assertEqual(updated["bedroom_requirement"], "4")
        self.assertEqual(updated["budget_requirement"], "15000")
        self.assertIn("rent", updated["property_types"])
        self.assertIn("landed", updated["property_types"])


if __name__ == "__main__":
    unittest.main()
