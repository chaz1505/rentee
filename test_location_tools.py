import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import requests

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("BUBBLE_API_TOKEN", "test-token")

import app as app_module
import location_tools


def resolved(name, latitude, longitude, level="specific_place"):
    return {"status": "resolved", "input": name, "resolved_name": name,
            "latitude": latitude, "longitude": longitude,
            "place_id": f"place-{name}", "location_level": level,
            "resolution_source": "fake"}


class FakeProvider:
    name = "fake_maps"

    def __init__(self, places, matrix=None):
        self.places = places
        self.matrix = matrix or {}
        self.matrix_calls = []

    def resolve_place(self, query, known_location=None):
        if known_location:
            return resolved(known_location["resolved_name"],
                            known_location["latitude"], known_location["longitude"])
        return self.places.get(query, {"status": "error", "input": query,
                                       "error": "Location could not be resolved."})

    def route_matrix(self, origins, destinations):
        self.matrix_calls.append((origins, destinations))
        return self.matrix


class LocationToolTests(unittest.TestCase):
    def test_get_travel_time_returns_structured_grounded_result(self):
        provider = FakeProvider({
            "One Menerung": resolved("One Menerung, Bangsar", 3.13, 101.67),
            "School": resolved("School, Kuala Lumpur", 3.14, 101.69),
        }, {(0, 0): {"status": "ok", "distance_km": 6.8,
                      "duration_minutes": 14, "duration_text": "14 min",
                      "traffic_basis": "current_traffic_aware"}})
        result = location_tools.get_travel_time(
            "One Menerung", "School", provider=provider
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["origin"]["resolved_name"], "One Menerung, Bangsar")
        self.assertEqual(result["destination"]["resolved_name"], "School, Kuala Lumpur")
        self.assertEqual(result["distance_km"], 6.8)
        self.assertEqual(result["duration_minutes"], 14)

    def test_existing_coordinates_skip_geocoding(self):
        session = MagicMock()
        provider = location_tools.GoogleMapsProvider("key", session=session)
        known = {"resolved_name": "Canonical Condo", "latitude": 3.1,
                 "longitude": 101.6, "location_level": "property"}
        result = provider.resolve_place("canonical condo", known)
        self.assertEqual(result["resolution_source"], "existing_coordinates")
        self.assertEqual(result["resolved_name"], "Canonical Condo")
        session.get.assert_not_called()

    def test_ambiguous_destination_is_returned_without_routing(self):
        provider = FakeProvider({
            "Condo": resolved("Condo", 3.1, 101.6),
            "School": {"status": "ambiguous", "input": "School",
                       "alternatives": [{"resolved_name": "Primary Campus"},
                                        {"resolved_name": "Secondary Campus"}]},
        })
        result = location_tools.get_travel_time("Condo", "School", provider=provider)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["destination"]["status"], "ambiguous")
        self.assertEqual(len(result["destination"]["alternatives"]), 2)
        self.assertEqual(provider.matrix_calls, [])

    def test_provider_timeout_returns_error_instead_of_raising(self):
        session = MagicMock()
        session.get.side_effect = requests.Timeout("slow")
        provider = location_tools.GoogleMapsProvider("key", session=session)
        result = location_tools.get_travel_time("A", "B", provider=provider)
        self.assertEqual(result["status"], "error")
        self.assertIn("request failed", result["origin"]["error"])

    def test_missing_api_key_fails_gracefully(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_MAPS_API_KEY", None)
            result = location_tools.get_travel_time("A", "B")
        self.assertEqual(result["status"], "error")
        self.assertIn("GOOGLE_MAPS_API_KEY", result["origin"]["error"])

    def test_compare_locations_batches_and_preserves_associations(self):
        places = {
            "Bangsar": resolved("Bangsar, Kuala Lumpur", 1, 1, "area"),
            "Mont Kiara": resolved("Mont Kiara, Kuala Lumpur", 2, 2, "area"),
            "School": resolved("Primary School Campus", 3, 3),
            "Office": resolved("Office Tower", 4, 4),
        }
        matrix = {
            (0, 0): {"status": "ok", "duration_minutes": 10},
            (0, 1): {"status": "ok", "duration_minutes": 20},
            (1, 0): {"status": "ok", "duration_minutes": 30},
            (1, 1): {"status": "ok", "duration_minutes": 40},
        }
        provider = FakeProvider(places, matrix)
        result = location_tools.compare_locations(
            ["Bangsar", "Mont Kiara"],
            [{"name": "School", "label": "school"},
             {"name": "Office", "label": "work"}], provider=provider,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(provider.matrix_calls), 1)
        self.assertEqual(result["candidates"][0]["resolution"]["resolved_name"],
                         "Bangsar, Kuala Lumpur")
        self.assertEqual(
            [item["duration_minutes"] for item in result["candidates"][1]["destinations"]],
            [30, 40],
        )
        self.assertEqual(result["candidates"][0]["destinations"][1]["label"], "work")

    def test_compare_locations_keeps_valid_results_after_partial_resolution_failure(self):
        provider = FakeProvider({
            "Bangsar": resolved("Bangsar, Kuala Lumpur", 1, 1, "area"),
            "Office": resolved("Office Tower", 4, 4),
        }, {(0, 0): {"status": "ok", "duration_minutes": 12}})
        result = location_tools.compare_locations(
            ["Bangsar", "Unknown"], [{"name": "Office", "label": "work"}],
            provider=provider,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["candidates"][0]["destinations"][0]["duration_minutes"], 12)
        self.assertEqual(result["candidates"][1]["resolution"]["status"], "error")

    def test_tool_schemas_and_trigger_policy(self):
        benchmark = app_module.build_response_args(
            "My kids go to Alice Smith and I work at CapSquare. Where should I live?"
        )
        tools = {item.get("name"): item for item in benchmark["tools"] if item.get("name")}
        self.assertIn("get_travel_time", tools)
        self.assertIn("compare_locations", tools)
        self.assertEqual(benchmark["tool_choice"],
                         {"type": "function", "name": "compare_locations"})
        self.assertEqual(
            app_module.build_response_args("What is Bangsar like?")["tool_choice"], "auto"
        )
        self.assertIn("Never guess travel times", benchmark["instructions"])

    @patch("app.compare_locations")
    def test_chat_stream_executes_comparison_before_final_text(self, compare):
        compare.return_value = json.dumps({"status": "ok", "candidates": []})
        call = SimpleNamespace(
            type="function_call", name="compare_locations", call_id="location-call",
            arguments=json.dumps({
                "candidate_locations": ["Bangsar", "Mont Kiara"],
                "destinations": [
                    {"name": "Alice Smith", "label": "school"},
                    {"name": "CapSquare", "label": "work"},
                ],
            }),
        )
        initial = SimpleNamespace(id="initial", output=[call], usage=None)
        final = SimpleNamespace(id="final", output=[], usage=None)

        class FakeStream:
            def __init__(self, response, events=()): self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [
            FakeStream(initial, [SimpleNamespace(
                type="response.output_text.delta", delta="Internal premature recommendation"
            )]),
            FakeStream(final, [SimpleNamespace(
                type="response.output_text.delta", delta="Grounded recommendation"
            )]),
        ]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post("/chat_stream", json={
                "message": "My kids go to Alice Smith and I work at CapSquare. Where should I live?"
            })
            body = response.get_data(as_text=True)
        compare.assert_called_once()
        self.assertIn("Grounded recommendation", body)
        self.assertNotIn("Internal premature recommendation", body)
        continuation = responses.stream.call_args_list[1].kwargs
        self.assertEqual(continuation["input"][0]["call_id"], "location-call")

    @patch("app.get_travel_time", return_value=json.dumps({
        "status": "error", "error": "GOOGLE_MAPS_API_KEY is not configured."
    }))
    def test_chat_stream_survives_provider_error_result(self, travel):
        call = SimpleNamespace(
            type="function_call", name="get_travel_time", call_id="travel-call",
            arguments=json.dumps({"origin": "A", "destination": "B"}),
        )
        initial = SimpleNamespace(id="initial", output=[call], usage=None)
        final = SimpleNamespace(id="final", output=[], usage=None)

        class FakeStream:
            def __init__(self, response, events=()): self.response, self.events = response, events
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def __iter__(self): return iter(self.events)
            def get_final_response(self): return self.response

        responses = MagicMock()
        responses.stream.side_effect = [FakeStream(initial), FakeStream(final, [
            SimpleNamespace(type="response.output_text.delta", delta="Location service unavailable")
        ])]
        with patch.object(app_module, "client", SimpleNamespace(responses=responses)):
            response = app_module.app.test_client().post(
                "/chat_stream", json={"message": "How far is A from B?"}
            )
            body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Location service unavailable", body)
        travel.assert_called_once_with("A", "B", "driving")


if __name__ == "__main__":
    unittest.main()
