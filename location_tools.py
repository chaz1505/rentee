"""Provider-neutral grounded location resolution and driving-time tools."""

import os
import time

import requests


GOOGLE_GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
LOCATION_TIMEOUT_SECONDS = 15


class LocationProviderError(RuntimeError):
    pass


def _duration_seconds(value):
    text = str(value or "").strip()
    if text.endswith("s"):
        text = text[:-1]
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


class GoogleMapsProvider:
    name = "google_maps"

    def __init__(self, api_key=None, session=requests):
        self.api_key = str(api_key or os.getenv("GOOGLE_MAPS_API_KEY") or "").strip()
        self.session = session

    def _require_key(self):
        if not self.api_key:
            raise LocationProviderError("GOOGLE_MAPS_API_KEY is not configured.")

    def resolve_place(self, query, known_location=None):
        clean_query = " ".join(str(query or "").split())
        if not clean_query:
            return {"status": "error", "input": clean_query,
                    "error": "A location is required."}
        if known_location:
            return {
                "status": "resolved", "input": clean_query,
                "resolved_name": known_location["resolved_name"],
                "latitude": float(known_location["latitude"]),
                "longitude": float(known_location["longitude"]),
                "place_id": known_location.get("place_id"),
                "location_level": known_location.get("location_level", "property"),
                "resolution_source": "existing_coordinates",
            }
        self._require_key()
        try:
            response = self.session.get(
                GOOGLE_GEOCODING_URL,
                params={"address": clean_query, "key": self.api_key, "region": "my"},
                timeout=LOCATION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as error:
            raise LocationProviderError(
                f"Location resolution request failed: {type(error).__name__}"
            ) from error
        status = payload.get("status")
        results = payload.get("results") or []
        if status == "ZERO_RESULTS" or not results:
            return {"status": "error", "input": clean_query,
                    "error": "Location could not be resolved."}
        if status != "OK":
            raise LocationProviderError(
                f"Location provider returned {status or 'an unknown error'}."
            )
        alternatives = [
            {"resolved_name": item.get("formatted_address"),
             "place_id": item.get("place_id")}
            for item in results[:5]
        ]
        if len(results) != 1 or results[0].get("partial_match"):
            return {"status": "ambiguous", "input": clean_query,
                    "alternatives": alternatives,
                    "error": "Location is ambiguous; a more specific place is required."}
        item = results[0]
        point = ((item.get("geometry") or {}).get("location") or {})
        if point.get("lat") is None or point.get("lng") is None:
            return {"status": "error", "input": clean_query,
                    "error": "Resolved location has no coordinates."}
        types = set(item.get("types") or [])
        area_types = {"locality", "sublocality", "neighborhood", "administrative_area_level_3"}
        return {
            "status": "resolved", "input": clean_query,
            "resolved_name": item.get("formatted_address") or clean_query,
            "latitude": float(point["lat"]), "longitude": float(point["lng"]),
            "place_id": item.get("place_id"),
            "location_level": "area" if types & area_types else "specific_place",
            "resolution_source": self.name,
        }

    def route_matrix(self, origins, destinations):
        self._require_key()
        body = {
            "origins": [{"waypoint": {"location": {"latLng": {
                "latitude": item["latitude"], "longitude": item["longitude"],
            }}}} for item in origins],
            "destinations": [{"waypoint": {"location": {"latLng": {
                "latitude": item["latitude"], "longitude": item["longitude"],
            }}}} for item in destinations],
            "travelMode": "DRIVE", "routingPreference": "TRAFFIC_AWARE",
        }
        headers = {
            "X-Goog-Api-Key": self.api_key,
            "X-Goog-FieldMask": (
                "originIndex,destinationIndex,status,condition,distanceMeters,duration"
            ),
            "Content-Type": "application/json",
        }
        try:
            response = self.session.post(
                GOOGLE_ROUTES_MATRIX_URL, headers=headers, json=body,
                timeout=LOCATION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            rows = response.json()
        except requests.RequestException as error:
            raise LocationProviderError(
                f"Travel-time request failed: {type(error).__name__}"
            ) from error
        if not isinstance(rows, list):
            raise LocationProviderError("Travel-time provider returned an invalid matrix.")
        matrix = {}
        for row in rows:
            origin_index = row.get("originIndex")
            destination_index = row.get("destinationIndex")
            seconds = _duration_seconds(row.get("duration"))
            metres = row.get("distanceMeters")
            element_status = row.get("status") or {}
            status_code = (
                element_status.get("code") if isinstance(element_status, dict) else None
            )
            if origin_index is None or destination_index is None:
                continue
            if (status_code not in (None, 0)
                    or row.get("condition") not in (None, "ROUTE_EXISTS")
                    or seconds is None or metres is None):
                matrix[(origin_index, destination_index)] = {
                    "status": "error", "error": "No driving route was returned."
                }
                continue
            minutes = round(seconds / 60)
            matrix[(origin_index, destination_index)] = {
                "status": "ok", "distance_km": round(float(metres) / 1000, 1),
                "duration_minutes": minutes, "duration_text": f"{minutes} min",
                "traffic_basis": "current_traffic_aware",
            }
        return matrix


def _safe_resolve(provider, query, coordinate_resolver=None):
    known = coordinate_resolver(query) if coordinate_resolver else None
    try:
        return provider.resolve_place(query, known)
    except LocationProviderError as error:
        return {"status": "error", "input": " ".join(str(query or "").split()),
                "error": str(error)}


def get_travel_time(origin, destination, mode="driving", provider=None,
                    coordinate_resolver=None):
    started = time.perf_counter()
    provider = provider or GoogleMapsProvider()
    if mode != "driving":
        return {"status": "error", "error": "Only driving mode is supported."}
    resolved_origin = _safe_resolve(provider, origin, coordinate_resolver)
    resolved_destination = _safe_resolve(provider, destination, coordinate_resolver)
    if resolved_origin.get("status") != "resolved" or resolved_destination.get("status") != "resolved":
        return {"status": "error", "origin": resolved_origin,
                "destination": resolved_destination, "source": provider.name}
    try:
        route = provider.route_matrix([resolved_origin], [resolved_destination]).get((0, 0))
    except LocationProviderError as error:
        return {"status": "error", "origin": resolved_origin,
                "destination": resolved_destination, "error": str(error),
                "source": provider.name}
    result = {"status": "ok", "origin": resolved_origin,
              "destination": resolved_destination, "source": provider.name,
              "mode": "driving", "provider_duration_ms": round((time.perf_counter() - started) * 1000)}
    result.update(route or {"status": "error", "error": "No route was returned."})
    return result


def compare_locations(candidate_locations, destinations, provider=None,
                      coordinate_resolver=None):
    started = time.perf_counter()
    provider = provider or GoogleMapsProvider()
    destination_items = [item for item in destinations or [] if isinstance(item, dict)]
    resolved_candidates = [
        _safe_resolve(provider, name, coordinate_resolver)
        for name in candidate_locations or []
    ]
    resolved_destinations = [
        _safe_resolve(provider, item.get("name"), coordinate_resolver)
        for item in destination_items
    ]
    valid_candidates = [(index, item) for index, item in enumerate(resolved_candidates)
                        if item.get("status") == "resolved"]
    valid_destinations = [(index, item) for index, item in enumerate(resolved_destinations)
                          if item.get("status") == "resolved"]
    matrix = {}
    provider_error = None
    if valid_candidates and valid_destinations:
        try:
            matrix = provider.route_matrix(
                [item for _, item in valid_candidates],
                [item for _, item in valid_destinations],
            )
        except LocationProviderError as error:
            provider_error = str(error)
    candidate_results = []
    valid_candidate_positions = {source: pos for pos, (source, _) in enumerate(valid_candidates)}
    valid_destination_positions = {source: pos for pos, (source, _) in enumerate(valid_destinations)}
    for candidate_index, candidate in enumerate(resolved_candidates):
        entry = {"location": " ".join(str((candidate_locations or [])[candidate_index]).split()),
                 "resolution": candidate, "destinations": []}
        for destination_index, destination in enumerate(resolved_destinations):
            requested = destination_items[destination_index]
            destination_result = {
                "label": requested.get("label"), "destination": requested.get("name"),
                "resolution": destination,
            }
            key = (valid_candidate_positions.get(candidate_index),
                   valid_destination_positions.get(destination_index))
            route = matrix.get(key) if None not in key else None
            if route:
                destination_result.update(route)
            elif provider_error:
                destination_result.update({"status": "error", "error": provider_error})
            elif candidate.get("status") != "resolved" or destination.get("status") != "resolved":
                destination_result.update({"status": "error", "error": "Location was not resolved."})
            else:
                destination_result.update({"status": "error", "error": "No route was returned."})
            entry["destinations"].append(destination_result)
        candidate_results.append(entry)
    return {"status": "partial" if any(
        item["resolution"].get("status") != "resolved" for item in candidate_results
    ) or provider_error else "ok", "candidates": candidate_results,
        "source": provider.name,
        "provider_duration_ms": round((time.perf_counter() - started) * 1000)}
