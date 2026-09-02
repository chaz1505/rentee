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
