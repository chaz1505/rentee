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
        self.resolve_calls = []
        self.matrix_calls = []
        self.matrix_modes = []

    def resolve_place(self, query, known_location=None):
        self.resolve_calls.append((query, known_location))
        if known_location and known_location.get("latitude") is not None:
            return resolved(known_location["resolved_name"],
                            known_location["latitude"], known_location["longitude"])
        if known_location:
            query = (known_location.get("formatted_address")
                     or known_location.get("canonical_name") or query)
        return self.places.get(query, {"status": "error", "input": query,
                                       "reason": "zero_results",
                                       "error": "Location could not be resolved."})

    def route_matrix(self, origins, destinations, mode="driving"):
        self.matrix_calls.append((origins, destinations))
        self.matrix_modes.append(mode)
        return self.matrix


class NearbyFakeProvider(FakeProvider):
    def __init__(self, places, nearby, matrix=None, nearby_error=None,
                 route_error=None):
        super().__init__(places, matrix)
        self.nearby = nearby
        self.nearby_error = nearby_error
        self.route_error = route_error
        self.nearby_calls = []

    def search_nearby(self, origin, included_types, radius_m, max_results):
        self.nearby_calls.append((origin, included_types, radius_m, max_results))
        if self.nearby_error:
            raise self.nearby_error
        return self.nearby

    def route_matrix(self, origins, destinations, mode="driving"):
        if self.route_error:
            raise self.route_error
        return super().route_matrix(origins, destinations, mode)


class LocationToolTests(unittest.TestCase):
    def test_nearby_categories_map_only_to_supported_google_types(self):
        self.assertEqual(
            location_tools.nearby_place_types([
                "supermarkets", "groceries", "convenience store", "pharmacy"
            ]),
            ["supermarket", "grocery_store", "convenience_store", "pharmacy"],
        )

    def test_nearby_search_deduplicates_batch_routes_filters_and_sorts(self):
        origin = resolved("9 Beringin", 3.15, 101.66)
        nearby = [
            {"id": "slow", "displayName": {"text": "Slow Grocer"},
             "formattedAddress": "Slow Street", "primaryType": "supermarket",
             "location": {"latitude": 3.16, "longitude": 101.67}},
            {"id": "fast", "displayName": {"text": "Fast Grocer"},
             "formattedAddress": "Fast Street", "primaryType": "grocery_store",
             "rating": 4.4, "userRatingCount": 25,
             "location": {"latitude": 3.151, "longitude": 101.661}},
            {"id": "fast", "displayName": {"text": "Fast Grocer duplicate"},
             "location": {"latitude": 3.151, "longitude": 101.661}},
            {"id": "outside", "displayName": {"text": "Outside Grocer"},
             "location": {"latitude": 3.17, "longitude": 101.68}},
        ]
        matrix = {
            (0, 0): {"status": "ok", "duration_minutes": 9, "distance_m": 700},
            (0, 1): {"status": "ok", "duration_minutes": 5, "distance_m": 500},
            # Rounded minutes alone would look eligible; exact seconds must exclude it.
            (0, 2): {"status": "ok", "duration_minutes": 10,
                     "duration_seconds": 601, "distance_m": 900},
        }
        provider = NearbyFakeProvider({"9 Beringin": origin}, nearby, matrix)
        result = location_tools.find_nearby_places(
            "9 Beringin", ["supermarkets", "groceries", "convenience store"],
            travel_mode="walking", max_travel_minutes=10, max_results=10,
            provider=provider,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual([item["name"] for item in result["places"]],
                         ["Fast Grocer", "Slow Grocer"])
        self.assertEqual(result["candidate_count"], 3)
        self.assertEqual(result["within_threshold"], 2)
        self.assertEqual(provider.matrix_modes, ["walking"])
        self.assertEqual(len(provider.matrix_calls), 1)
        self.assertEqual(provider.nearby_calls[0][2], 2000)
        self.assertEqual(provider.nearby_calls[0][3], 20)

    def test_nearby_origin_ambiguity_short_circuits_discovery(self):
        provider = NearbyFakeProvider({
            "School": {"status": "ambiguous", "candidates": [{"name": "A"}, {"name": "B"}]}
        }, [])
        result = location_tools.find_nearby_places(
            "School", ["cafe"], provider=provider
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["origin"]["status"], "ambiguous")
        self.assertEqual(provider.nearby_calls, [])

    def test_nearby_zero_results_is_grounded_empty_success(self):
        provider = NearbyFakeProvider({
            "Condo": resolved("Condo", 3.1, 101.6)
        }, [])
        result = location_tools.find_nearby_places(
            "Condo", ["park"], provider=provider
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["places"], [])
        self.assertEqual(result["candidate_count"], 0)

    def test_nearby_provider_and_route_failures_are_structured(self):
        origin = {"Condo": resolved("Condo", 3.1, 101.6)}
        place = [{"id": "shop", "displayName": {"text": "Shop"},
                  "location": {"latitude": 3.11, "longitude": 101.61}}]
        search_failed = NearbyFakeProvider(
            origin, place,
            nearby_error=location_tools.LocationProviderError("quota", "quota"),
        )
        self.assertEqual(location_tools.find_nearby_places(
            "Condo", ["cafe"], provider=search_failed
        )["reason"], "quota")
        route_failed = NearbyFakeProvider(
            origin, place,
            route_error=location_tools.LocationProviderError("route down", "timeout"),
        )
        result = location_tools.find_nearby_places(
            "Condo", ["cafe"], max_travel_minutes=10, provider=route_failed
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["places"], [])
        self.assertEqual(result["reason"], "routing_failed")

    def test_google_route_matrix_uses_walk_without_traffic_preference(self):
        session = MagicMock()
        session.post.return_value.json.return_value = []
        provider = location_tools.GoogleMapsProvider("key", session=session)
        provider.route_matrix(
            [resolved("A", 1, 1)], [resolved("B", 2, 2)], mode="walking"
        )
        request_body = session.post.call_args.kwargs["json"]
        self.assertEqual(request_body["travelMode"], "WALK")
        self.assertNotIn("routingPreference", request_body)

    def test_google_nearby_search_uses_new_endpoint_and_distance_ranking(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": []}
        provider = location_tools.GoogleMapsProvider("key", session=session)
        result = provider.search_nearby(
            {"latitude": 3.15, "longitude": 101.66},
            ["supermarket", "grocery_store"], 2000, 20,
        )
        self.assertEqual(result, [])
        call = session.post.call_args
        self.assertEqual(call.args[0], location_tools.GOOGLE_PLACES_NEARBY_SEARCH_URL)
        self.assertEqual(call.kwargs["json"]["includedTypes"],
                         ["supermarket", "grocery_store"])
        self.assertEqual(call.kwargs["json"]["rankPreference"], "DISTANCE")
        self.assertEqual(
            call.kwargs["json"]["locationRestriction"]["circle"]["radius"], 2000.0
        )

    def test_get_travel_time_supports_walking(self):
        provider = FakeProvider({
            "A": resolved("A", 1, 1), "B": resolved("B", 2, 2),
        }, {(0, 0): {"status": "ok", "duration_minutes": 8}})
        result = location_tools.get_travel_time(
            "A", "B", mode="walking", provider=provider
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["mode"], "walking")
        self.assertEqual(provider.matrix_modes, ["walking"])
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
        self.assertEqual(result["resolution_source"], "stored_coordinates")
        self.assertEqual(result["resolved_name"], "Canonical Condo")
        session.post.assert_not_called()
        session.get.assert_not_called()

    def test_stored_canonical_address_is_used_before_raw_input(self):
        provider = FakeProvider({
            "1 Jalan Example, Kuala Lumpur": resolved("Canonical Condo", 3.1, 101.6)
        }, {(0, 0): {"status": "ok", "duration_minutes": 0}})
        result = location_tools.get_travel_time(
            "canonical condo", "1 Jalan Example, Kuala Lumpur", provider=provider,
            coordinate_resolver=lambda value: ({
                "canonical_name": "Canonical Condo",
                "resolved_name": "Canonical Condo",
                "formatted_address": "1 Jalan Example, Kuala Lumpur",
            } if value == "canonical condo" else None),
        )
        self.assertEqual(result["status"], "ok")

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
        session.post.side_effect = requests.Timeout("slow")
        provider = location_tools.GoogleMapsProvider("key", session=session)
        result = location_tools.get_travel_time("A", "B", provider=provider)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["origin"]["reason"], "timeout")
        self.assertIn("timed out", result["origin"]["error"])

    def test_places_text_search_resolves_poi_without_geocoding(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [{
            "id": "alice-primary", "displayName": {"text": "The Alice Smith School"},
            "formattedAddress": "Jalan Bellamy, Kuala Lumpur",
            "location": {"latitude": 3.12, "longitude": 101.69},
            "types": ["school"],
        }]}
        provider = location_tools.GoogleMapsProvider("key", session=session)
        result = provider.resolve_place("Alice Smith School")
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolution_method"], "places_text_search")
        session.get.assert_not_called()

    def test_google_rejects_area_result_for_specific_street_and_tries_full_geocode(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [{
            "id": "area", "displayName": {"text": "Damansara Heights"},
            "formattedAddress": "Kuala Lumpur",
            "location": {"latitude": 3.15, "longitude": 101.66},
            "types": ["neighborhood"],
        }]}
        session.get.return_value.json.return_value = {"status": "OK", "results": [{
            "place_id": "street", "formatted_address": "Jalan Beringin, Kuala Lumpur",
            "geometry": {"location": {"lat": 3.145, "lng": 101.658}},
            "types": ["route"],
        }]}
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "Jalan Beringin, Damansara Heights"
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["resolution_level"], "street")
        self.assertEqual(session.get.call_args.kwargs["params"]["address"],
                         "Jalan Beringin, Damansara Heights")

    def test_numbered_property_accepts_google_result_retaining_house_number(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [{
            "id": "numbered", "displayName": {"text": "9 Jalan Beringin"},
            "formattedAddress": "9 Jalan Beringin, Bukit Damansara, Kuala Lumpur",
            "location": {"latitude": 3.145, "longitude": 101.658},
            "types": ["street_address"],
        }]}
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "9 Jalan Beringin, Damansara Heights"
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["place_id"], "numbered")
        session.get.assert_not_called()

    def test_numbered_property_rejects_google_result_that_loses_house_number(self):
        session = MagicMock()
        broad = {
            "id": "street", "displayName": {"text": "Jalan Beringin"},
            "formattedAddress": "Jalan Beringin, Bukit Damansara, Kuala Lumpur",
            "location": {"latitude": 3.15, "longitude": 101.66},
            "types": ["street_address"],
        }
        session.post.return_value.json.return_value = {"places": [broad]}
        session.get.return_value.json.return_value = {
            "status": "OK", "results": [{
                "place_id": "street-geocode",
                "formatted_address": broad["formattedAddress"],
                "geometry": {"location": {"lat": 3.15, "lng": 101.66}},
                "types": ["street_address"],
            }],
        }
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "9 Jalan Beringin, Damansara Heights"
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "specificity_downgrade")
        session.get.assert_called_once()

    def test_numbered_property_rejects_unrelated_numberless_street(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [{
            "id": "damanlela", "displayName": {"text": "Jalan Damanlela"},
            "formattedAddress": "Jalan Damanlela, Damansara Heights",
            "location": {"latitude": 3.151, "longitude": 101.665},
            "types": ["route"],
        }]}
        session.get.return_value.json.return_value = {"status": "OK", "results": [{
            "place_id": "damanlela-geocode",
            "formatted_address": "Jalan Damanlela, Damansara Heights",
            "geometry": {"location": {"lat": 3.151, "lng": 101.665}},
            "types": ["route"],
        }]}
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "9 Jalan Beringin, Damansara Heights"
        )
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["reason"], "specificity_downgrade")

    def test_non_numbered_poi_still_accepts_normal_places_result(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [{
            "id": "school", "displayName": {"text": "Alice Smith School"},
            "formattedAddress": "Jalan Bellamy, Kuala Lumpur",
            "location": {"latitude": 3.12, "longitude": 101.69},
            "types": ["school"],
        }]}
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "Alice Smith School"
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["place_id"], "school")
        session.get.assert_not_called()

    def test_google_failure_reasons_are_distinguishable(self):
        cases = {}
        denied = MagicMock()
        denied.raise_for_status.side_effect = requests.HTTPError(
            response=SimpleNamespace(status_code=403)
        )
        cases["authentication"] = denied
        quota = MagicMock()
        quota.raise_for_status.side_effect = requests.HTTPError(
            response=SimpleNamespace(status_code=429)
        )
        cases["quota"] = quota
        invalid = MagicMock()
        invalid.json.side_effect = ValueError("bad json")
        cases["parsing_error"] = invalid
        for expected_reason, response in cases.items():
            with self.subTest(reason=expected_reason):
                session = MagicMock()
                session.post.return_value = response
                result = location_tools.get_travel_time(
                    "A", "B", provider=location_tools.GoogleMapsProvider("key", session)
                )
                self.assertEqual(result["origin"]["reason"], expected_reason)

    def test_zero_results_is_reported_after_places_and_geocoding(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": []}
        session.get.return_value.json.return_value = {"status": "ZERO_RESULTS", "results": []}
        result = location_tools.get_travel_time(
            "A", "B", provider=location_tools.GoogleMapsProvider("key", session)
        )
        self.assertEqual(result["origin"]["reason"], "location_not_resolved")
        self.assertIn("places_text_search", result["origin"]["attempts"])
        self.assertIn("geocode", result["origin"]["attempts"])

    def test_multiple_campuses_remain_ambiguous_without_context(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [
            {"id": "primary", "displayName": {"text": "Alice Smith Primary Campus"},
             "formattedAddress": "Jalan Bellamy", "location": {"latitude": 1, "longitude": 1}},
            {"id": "secondary", "displayName": {"text": "Alice Smith Secondary Campus"},
             "formattedAddress": "Seri Kembangan", "location": {"latitude": 2, "longitude": 2}},
        ]}
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "Alice Smith"
        )
        self.assertEqual(result["status"], "ambiguous")
        self.assertEqual(len(result["candidates"]), 2)

    def test_unique_street_context_disambiguates_places_results(self):
        session = MagicMock()
        session.post.return_value.json.return_value = {"places": [
            {"id": "primary", "displayName": {"text": "Alice Smith Primary Campus"},
             "formattedAddress": "Alice Smith Jalan Bellamy Kuala Lumpur",
             "location": {"latitude": 1, "longitude": 1}},
            {"id": "secondary", "displayName": {"text": "Alice Smith Secondary Campus"},
             "formattedAddress": "Seri Kembangan", "location": {"latitude": 2, "longitude": 2}},
        ]}
        result = location_tools.GoogleMapsProvider("key", session=session).resolve_place(
            "Alice Smith Jalan Bellamy"
        )
        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["place_id"], "primary")

    def test_web_identity_retries_google_and_ignores_web_travel_claim(self):
        canonical = "Alice Smith School, Jalan Bellamy"
        provider = FakeProvider({
            canonical: resolved(canonical, 3.1, 101.7),
            "Condo": resolved("Condo", 3.2, 101.6),
        }, {(0, 0): {"status": "ok", "duration_minutes": 17}})
        result = location_tools.get_travel_time(
            "Condo", "obscure school", provider=provider,
            web_resolver=lambda *_args: {
                "status": "resolved", "canonical_name": "Alice Smith School",
                "formatted_address": canonical, "area": "Kuala Lumpur",
                "marketing_travel_minutes": 5, "source_urls": ["https://school.example"],
            },
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["duration_minutes"], 17)
        self.assertEqual(result["destination"]["resolution_method"], "web_address_retry")

    def test_area_fallback_is_explicitly_approximate(self):
        provider = FakeProvider({
            "Bangsar, Kuala Lumpur": resolved("Bangsar", 3.13, 101.67, "area"),
            "School": resolved("School", 3.2, 101.7),
        }, {(0, 0): {"status": "ok", "duration_minutes": 20}})
        result = location_tools.get_travel_time(
            "Unknown Building, Bangsar, Kuala Lumpur", "School", provider=provider
        )
        self.assertEqual(result["origin"]["resolution_method"], "area_fallback")
        self.assertTrue(result["origin"]["approximate"])
        self.assertEqual(result["origin"]["resolution_level"], "area")

    def test_contextual_area_entity_does_not_replace_specific_street(self):
        query = "Jalan Beringin, Damansara Heights"
        provider = FakeProvider({
            query: resolved("Jalan Beringin", 3.14, 101.66, "street"),
            "School": resolved("School", 3.2, 101.7),
        }, {(0, 0): {"status": "ok", "duration_minutes": 12}})
        known_area = {
            "canonical_name": "Damansara Heights",
            "resolved_name": "Damansara Heights",
            "formatted_address": "Jalan Damanlela",
            "resolution_level": "area",
            "match_type": "contextual_component",
        }
        result = location_tools.get_travel_time(
            query, "School", provider=provider,
            coordinate_resolver=lambda value: known_area if value == query else None,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.resolve_calls[0], (query, None))
        self.assertEqual(result["origin"]["resolved_name"], "Jalan Beringin")

    def test_exact_area_input_can_use_exact_known_entity(self):
        provider = FakeProvider({
            "Jalan Damanlela": resolved("Damansara Heights", 3.15, 101.66, "area"),
            "School": resolved("School", 3.2, 101.7),
        }, {(0, 0): {"status": "ok", "duration_minutes": 12}})
        known_area = {
            "canonical_name": "Damansara Heights",
            "resolved_name": "Damansara Heights",
            "formatted_address": "Jalan Damanlela",
            "resolution_level": "area", "match_type": "exact",
        }
        result = location_tools.get_travel_time(
            "Damansara Heights", "School", provider=provider,
            coordinate_resolver=lambda value: known_area if value == "Damansara Heights" else None,
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(provider.resolve_calls[0][1], known_area)

    def test_contextual_building_coordinates_are_not_discarded(self):
        provider = FakeProvider({
            "School": resolved("School", 3.2, 101.7)
        }, {(0, 0): {"status": "ok", "duration_minutes": 8}})
        known_building = {
            "canonical_name": "One Menerung", "resolved_name": "One Menerung",
            "latitude": 3.13, "longitude": 101.67,
            "resolution_level": "building", "match_type": "contextual_component",
        }
        result = location_tools.get_travel_time(
            "One Menerung, Bangsar", "School", provider=provider,
            coordinate_resolver=lambda value: (
                known_building if value == "One Menerung, Bangsar" else None
            ),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["origin"]["resolved_name"], "One Menerung")
        self.assertEqual(provider.resolve_calls[0][1], known_building)

    def test_exact_property_failure_uses_explicit_area_fallback(self):
        query = "9 Beringin, Jalan Beringin, Damansara Heights"
        provider = FakeProvider({
            "Damansara Heights": resolved("Damansara Heights", 3.15, 101.66, "area"),
            "School": resolved("School", 3.2, 101.7),
        }, {(0, 0): {"status": "ok", "duration_minutes": 12}})
        known_area = {
            "canonical_name": "Damansara Heights", "resolved_name": "Damansara Heights",
            "formatted_address": "Jalan Damanlela", "resolution_level": "area",
            "match_type": "contextual_component",
        }
        result = location_tools.get_travel_time(
            query, "School", provider=provider,
            coordinate_resolver=lambda value: known_area if value == query else None,
        )
        self.assertEqual(provider.resolve_calls[0], (query, None))
        self.assertEqual(result["origin"]["resolution_method"], "area_fallback")
        self.assertEqual(result["origin"]["resolution_level"], "area")
        self.assertTrue(result["origin"]["approximate"])

    def test_rentee_entity_matching_normalizes_case_spacing_and_punctuation(self):
        condo = {"Condo name": "Trinity Pentamont", "Address": "Mont Kiara, Kuala Lumpur"}
        with patch("app._get_condo_lookup", return_value={"trinity pentamont": condo}):
            for value in ("trinity pentamont", " TRINITY   PENTAMONT ",
                          "Trinity-Pentamont", "Trinity ’ Pentamont"):
                result = app_module._known_location_coordinates(value)
                self.assertEqual(result["canonical_name"], "Trinity Pentamont")
                self.assertEqual(result["formatted_address"], "Mont Kiara, Kuala Lumpur")
        normalized = app_module._normalized_location_entity_name
        self.assertEqual(normalized("Mont' Kiara"), normalized("Mont’ Kiara"))
        self.assertEqual(normalized("Mont’ Kiara"), normalized("Mont Kiara"))

    def test_rentee_substring_match_is_context_not_primary_entity(self):
        rows = {
            "area": {"Condo name": "Damansara Heights", "Address": "Jalan Damanlela"},
            "condo": {"Condo name": "One Menerung", "Address": "Jalan Menerung"},
        }
        with patch("app._get_condo_lookup", return_value=rows):
            street = app_module._known_location_coordinates(
                "Jalan Beringin, Damansara Heights"
            )
            development = app_module._known_location_coordinates("One Menerung, Bangsar")
            exact_area = app_module._known_location_coordinates("Damansara Heights")
        self.assertEqual(street["match_type"], "contextual_component")
        self.assertEqual(street["resolution_level"], "area")
        self.assertEqual(development["match_type"], "contextual_component")
        self.assertEqual(development["resolution_level"], "building")
        self.assertEqual(exact_area["match_type"], "exact")

    def test_known_location_level_uses_selected_match_not_last_loop_row(self):
        rows = {
            "selected": {"Condo name": "One Menerung", "Address": "Jalan Menerung"},
            "later-nonmatch": {"Condo name": "Z Residence", "Address": "Jalan Z"},
        }
        with patch("app._get_condo_lookup", return_value=rows):
            result = app_module._known_location_coordinates("One Menerung")
        self.assertEqual(result["canonical_name"], "One Menerung")
        self.assertEqual(result["resolution_level"], "building")

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
        self.assertIn("Never ask for a unit number, floor", benchmark["instructions"])
        travel_description = tools["get_travel_time"]["description"]
        self.assertIn("trusted current listing context", travel_description)

    def test_reply_listing_address_is_available_for_this_property_tool_routing(self):
        listing = {"name": "Trinity Pentamont Mont' Kiara",
                   "Address": "Mont Kiara, Kuala Lumpur"}
        with patch("app.bubble", return_value=listing):
            context = app_module.whatsapp_reply_listing_context("listing-1")
        args = app_module.build_response_args(
            "How far is this from Alice Smith School, Jalan Bellamy, Kuala Lumpur?",
            conversation_context=context,
        )
        self.assertEqual(args["tool_choice"], {"type": "function", "name": "get_travel_time"})
        self.assertIn("Trinity Pentamont", args["instructions"])
        self.assertIn("Address: Mont Kiara, Kuala Lumpur", args["instructions"])
        self.assertIn("apartment details", args["instructions"])

    def test_reply_listing_context_preserves_coordinates_and_full_identity(self):
        listing = {
            "name": "9 Beringin", "Address": "Jalan Beringin, Damansara Heights",
            "latitude": 3.145, "longitude": 101.658,
        }
        with patch("app.bubble", return_value=listing):
            context = app_module.whatsapp_reply_listing_context("listing-9")
        self.assertIn("Property: 9 Beringin", context)
        self.assertIn("Address: Jalan Beringin, Damansara Heights", context)
        self.assertIn("Latitude: 3.145", context)
        self.assertIn("Longitude: 101.658", context)
        self.assertIn("never drop a house number", context)

    @patch("app.find_grounded_nearby_places")
    def test_nearby_wrapper_uses_trusted_listing_coordinates_directly(self, grounded):
        grounded.return_value = {"status": "ok", "origin": {}, "places": []}
        app_module.find_nearby_places(
            "9 Beringin, Jalan Beringin, Damansara Heights", ["shops"],
            origin_latitude=3.145, origin_longitude=101.658,
        )
        resolver = grounded.call_args.kwargs["coordinate_resolver"]
        known = resolver("9 Beringin, Jalan Beringin, Damansara Heights")
        self.assertEqual((known["latitude"], known["longitude"]), (3.145, 101.658))
        self.assertEqual(known["resolution_level"], "exact_property")

    def test_compare_locations_uses_the_same_web_resolution_pipeline(self):
        provider = FakeProvider({
            "Canonical Condo Address": resolved("Canonical Condo", 1, 1),
            "School": resolved("School", 2, 2),
        }, {(0, 0): {"status": "ok", "duration_minutes": 13}})
        result = location_tools.compare_locations(
            ["Obscure Condo"], [{"name": "School"}], provider=provider,
            web_resolver=lambda query, _known: ({
                "status": "resolved", "canonical_name": "Canonical Condo",
                "formatted_address": "Canonical Condo Address", "area": "Bangsar",
                "source_urls": [],
            } if query == "Obscure Condo" else None),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            result["candidates"][0]["resolution"]["resolution_method"],
            "web_address_retry",
        )

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
