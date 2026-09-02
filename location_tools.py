"""Provider-neutral grounded location resolution and driving-time tools."""

import os
import time

import requests


GOOGLE_GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_PLACES_TEXT_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
GOOGLE_PLACES_NEARBY_SEARCH_URL = "https://places.googleapis.com/v1/places:searchNearby"
GOOGLE_ROUTES_MATRIX_URL = "https://routes.googleapis.com/distanceMatrix/v2:computeRouteMatrix"
LOCATION_TIMEOUT_SECONDS = 15

NEARBY_CATEGORY_TYPES = {
    "supermarket": ("supermarket",),
    "supermarkets": ("supermarket",),
    "grocery": ("grocery_store", "supermarket"),
    "groceries": ("grocery_store", "supermarket"),
    "grocery store": ("grocery_store", "supermarket"),
    "convenience store": ("convenience_store",),
    "pharmacy": ("pharmacy",),
    "cafe": ("cafe",),
    "coffee shop": ("coffee_shop", "cafe"),
    "restaurant": ("restaurant",),
    "school": ("school",),
    "nursery": ("preschool", "child_care_agency"),
    "kindergarten": ("preschool",),
    "gym": ("gym",),
    "park": ("park",),
    "mall": ("shopping_mall",),
    "clinic": ("medical_clinic",),
    "hospital": ("hospital",),
    "public transport": ("bus_station", "train_station", "subway_station"),
    "petrol station": ("gas_station",),
    "gas station": ("gas_station",),
    "laundromat": ("laundry",),
    "specialty food shop": ("food_store",),
    "shop": ("store",),
    "shops": ("store",),
}


def _normalize_place_text(value):
    """Normalize presentation differences without enabling fuzzy matching."""
    value = str(value or "").casefold().replace("’", "'")
    return " ".join("".join(
        character if character.isalnum() else " " for character in value
    ).split())


def nearby_place_types(categories):
    """Map conservative customer category labels to supported Places API types."""
    result = []
    for category in categories or []:
        normalized = _normalize_place_text(category)
        mapped = NEARBY_CATEGORY_TYPES.get(normalized)
        if mapped is None and normalized in {
            place_type for values in NEARBY_CATEGORY_TYPES.values() for place_type in values
        }:
            mapped = (normalized,)
        for place_type in mapped or ():
            if place_type not in result:
                result.append(place_type)
    return result


def nearby_search_radius(travel_mode, max_travel_minutes):
    """Use a deliberately broad discovery circle; route time does final inclusion."""
    minutes = int(max_travel_minutes) if max_travel_minutes else None
    if travel_mode == "walking":
        return 2000 if minutes is None or minutes <= 10 else 3000 if minutes <= 20 else 5000
    return 10000 if minutes is None or minutes <= 10 else 20000 if minutes <= 20 else 30000


class LocationProviderError(RuntimeError):
    def __init__(self, message, reason="provider_error"):
        super().__init__(message)
        self.reason = reason


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

    def _place_result(self, clean_query, item, method):
        point = ((item.get("location") or (item.get("geometry") or {}).get("location")) or {})
        latitude = point.get("latitude", point.get("lat"))
        longitude = point.get("longitude", point.get("lng"))
        if latitude is None or longitude is None:
            return {"status": "error", "reason": "missing_coordinates",
                    "input": clean_query, "error": "Resolved location has no coordinates."}
        display = item.get("displayName") or {}
        display_name = display.get("text") if isinstance(display, dict) else display
        formatted = item.get("formattedAddress") or item.get("formatted_address")
        types = set(item.get("types") or [])
        area_types = {"locality", "sublocality", "neighborhood", "administrative_area_level_3"}
        level = "area" if types & area_types else "exact"
        return {
            "status": "resolved", "input": clean_query,
            "resolved_name": display_name or formatted or clean_query,
            "formatted_address": formatted,
            "latitude": float(latitude), "longitude": float(longitude),
            "place_id": item.get("id") or item.get("place_id"),
            "resolution_level": level, "location_level": level,
            "resolution_method": method, "resolution_source": method,
            "approximate": level == "area",
        }

    def search_places(self, query):
        self._require_key()
        try:
            response = self.session.post(
                GOOGLE_PLACES_TEXT_SEARCH_URL,
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "places.id,places.displayName,places.formattedAddress,"
                        "places.location,places.types"
                    ),
                    "Content-Type": "application/json",
                },
                json={"textQuery": query, "regionCode": "MY", "languageCode": "en"},
                timeout=LOCATION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise LocationProviderError("Places search timed out.", "timeout") from error
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            reason = "authentication" if status in {401, 403} else "quota" if status == 429 else "http_error"
            raise LocationProviderError(
                f"Places search failed with HTTP {status or 'unknown'}.", reason
            ) from error
        except (TypeError, ValueError) as error:
            raise LocationProviderError("Places search returned invalid JSON.", "parsing_error") from error
        places = payload.get("places") or []
        if not places:
            return {"status": "error", "reason": "zero_results", "input": query,
                    "error": "Places search returned no results."}
        if len(places) > 1:
            normalized_query = _normalize_place_text(query)
            contextual = [item for item in places if normalized_query and normalized_query in
                          _normalize_place_text(
                              f"{(item.get('displayName') or {}).get('text') or ''} "
                              f"{item.get('formattedAddress') or ''}"
                          )]
            if len(contextual) == 1:
                return self._place_result(query, contextual[0], "places_text_search")
            return {"status": "ambiguous", "input": query, "candidates": [
                {"name": ((item.get("displayName") or {}).get("text")),
                 "formatted_address": item.get("formattedAddress"),
                 "place_id": item.get("id")}
                for item in places[:5]
            ]}
        return self._place_result(query, places[0], "places_text_search")

    def geocode(self, query):
        self._require_key()
        try:
            response = self.session.get(
                GOOGLE_GEOCODING_URL,
                params={"address": query, "key": self.api_key,
                        "region": "my", "components": "country:MY"},
                timeout=LOCATION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise LocationProviderError("Geocoding timed out.", "timeout") from error
        except requests.RequestException as error:
            raise LocationProviderError(
                f"Geocoding request failed: {type(error).__name__}", "http_error"
            ) from error
        except (TypeError, ValueError) as error:
            raise LocationProviderError("Geocoding returned invalid JSON.", "parsing_error") from error
        status = payload.get("status")
        results = payload.get("results") or []
        if status == "ZERO_RESULTS" or not results:
            return {"status": "error", "reason": "zero_results", "input": query,
                    "error": "Geocoding returned no results."}
        if status != "OK":
            reason = "authentication" if status == "REQUEST_DENIED" else "quota" if status == "OVER_QUERY_LIMIT" else "provider_error"
            raise LocationProviderError(
                f"Geocoding returned {status or 'an unknown error'}: "
                f"{payload.get('error_message') or 'no details'}", reason
            )
        if len(results) != 1 or results[0].get("partial_match"):
            return {"status": "ambiguous", "input": query, "candidates": [
                {"name": item.get("formatted_address"),
                 "formatted_address": item.get("formatted_address"),
                 "place_id": item.get("place_id")}
                for item in results[:5]
            ]}
        return self._place_result(query, results[0], "geocode")

    def search_nearby(self, origin, included_types, radius_m, max_results):
        self._require_key()
        try:
            response = self.session.post(
                GOOGLE_PLACES_NEARBY_SEARCH_URL,
                headers={
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "places.id,places.displayName,places.formattedAddress,"
                        "places.location,places.primaryType,places.rating,"
                        "places.userRatingCount,places.businessStatus"
                    ),
                    "Content-Type": "application/json",
                },
                json={
                    "includedTypes": included_types,
                    "maxResultCount": max_results,
                    "rankPreference": "DISTANCE",
                    "languageCode": "en", "regionCode": "MY",
                    "locationRestriction": {"circle": {
                        "center": {"latitude": origin["latitude"],
                                   "longitude": origin["longitude"]},
                        "radius": float(radius_m),
                    }},
                },
                timeout=LOCATION_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.Timeout as error:
            raise LocationProviderError("Nearby search timed out.", "timeout") from error
        except requests.RequestException as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            reason = "authentication" if status in {401, 403} else "quota" if status == 429 else "http_error"
            raise LocationProviderError(
                f"Nearby search failed with HTTP {status or 'unknown'}.", reason
            ) from error
        except (TypeError, ValueError) as error:
            raise LocationProviderError(
                "Nearby search returned invalid JSON.", "parsing_error"
            ) from error
        return payload.get("places") or []

    def resolve_place(self, query, known_location=None):
        clean_query = " ".join(str(query or "").split())
        if not clean_query:
            return {"status": "error", "input": clean_query,
                    "error": "A location is required."}
        if (known_location and known_location.get("latitude") is not None
                and known_location.get("longitude") is not None):
            return {
                "status": "resolved", "input": clean_query,
                "resolved_name": known_location["resolved_name"],
                "formatted_address": known_location.get("formatted_address"),
                "latitude": float(known_location["latitude"]),
                "longitude": float(known_location["longitude"]),
                "place_id": known_location.get("place_id"),
                "location_level": known_location.get("resolution_level", "building"),
                "resolution_level": known_location.get("resolution_level", "building"),
                "resolution_source": "stored_coordinates",
                "resolution_method": "stored_coordinates", "approximate": False,
            }
        search_query = ((known_location or {}).get("formatted_address")
                        or (known_location or {}).get("canonical_name") or clean_query)
        places = self.search_places(search_query)
        if places.get("status") in {"resolved", "ambiguous"}:
            if known_location and places.get("status") == "resolved":
                places["resolution_method"] = "stored_address" if known_location.get("formatted_address") else "rentee_entity_match"
            return places
        return self.geocode(search_query)

    def route_matrix(self, origins, destinations, mode="driving"):
        self._require_key()
        travel_mode = {"driving": "DRIVE", "walking": "WALK"}.get(mode)
        if not travel_mode:
            raise LocationProviderError(f"Unsupported travel mode: {mode}.", "unsupported_mode")
        body = {
            "origins": [{"waypoint": {"location": {"latLng": {
                "latitude": item["latitude"], "longitude": item["longitude"],
            }}}} for item in origins],
            "destinations": [{"waypoint": {"location": {"latLng": {
                "latitude": item["latitude"], "longitude": item["longitude"],
            }}}} for item in destinations],
            "travelMode": travel_mode,
        }
        if mode == "driving":
            body["routingPreference"] = "TRAFFIC_AWARE"
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
                "status": "ok", "distance_m": int(metres),
                "distance_km": round(float(metres) / 1000, 1),
                "duration_seconds": seconds,
                "duration_minutes": minutes, "duration_text": f"{minutes} min",
                "traffic_basis": (
                    "current_traffic_aware" if mode == "driving"
                    else "walking_route_estimate"
                ),
            }
        return matrix


def _log_resolution(query, attempt, result, detail=""):
    print(
        f"[LOCATION RESOLVE] input={str(query)!r} attempt={attempt} "
        f"result={result}{(' ' + detail) if detail else ''}", flush=True,
    )


def _safe_resolve(provider, query, coordinate_resolver=None, web_resolver=None):
    attempts = []
    known = coordinate_resolver(query) if coordinate_resolver else None
    attempts.extend(["stored_coordinates", "stored_address", "rentee_entity_match"])
    has_coordinates = bool(
        known and known.get("latitude") is not None and known.get("longitude") is not None
    )
    _log_resolution(query, "stored_coordinates", "matched" if has_coordinates else "miss")
    _log_resolution(query, "stored_address",
                    "matched" if known and known.get("formatted_address") else "miss")
    _log_resolution(query, "rentee_entity_match", "matched" if known else "miss",
                    f"canonical_name={(known or {}).get('canonical_name')!r}" if known else "")
    if known and known.get("status") == "ambiguous":
        return {**known, "input": query, "attempts": attempts}
    try:
        result = provider.resolve_place(query, known)
    except LocationProviderError as error:
        _log_resolution(query, "google", "failed", f"reason={error.reason}")
        return {"status": "error", "reason": error.reason,
                "input": " ".join(str(query or "").split()),
                "error": str(error), "attempts": attempts + ["places_text_search", "geocode"]}
    method = result.get("resolution_method") or result.get("resolution_source")
    if method != "stored_coordinates":
        attempts.append("places_text_search")
        if method == "geocode" or result.get("status") == "error":
            attempts.append("geocode")
    if result.get("status") == "resolved":
        result["attempts"] = attempts
        _log_resolution(query, result.get("resolution_method", "google"), "success",
                        f"resolved_name={result.get('resolved_name')!r}")
        return result
    if result.get("status") == "ambiguous":
        result["attempts"] = attempts
        _log_resolution(query, "google", "ambiguous")
        return result

    web_identity = None
    if web_resolver:
        attempts.append("web_search")
        _log_resolution(query, "web_search", "started")
        try:
            web_identity = web_resolver(query, known)
        except Exception as error:
            _log_resolution(query, "web_search", "failed",
                            f"error={type(error).__name__}")
        if web_identity and web_identity.get("status") == "resolved":
            retry_query = web_identity.get("formatted_address") or web_identity.get("canonical_name")
            _log_resolution(query, "web_search", "address_found",
                            f"canonical={retry_query!r}")
            attempts.append("web_address_retry")
            try:
                retry = provider.resolve_place(retry_query)
            except LocationProviderError as error:
                retry = {"status": "error", "reason": error.reason, "error": str(error)}
            if retry.get("status") == "resolved":
                retry.update({
                    "input": " ".join(str(query or "").split()),
                    "resolution_method": "web_address_retry",
                    "web_identity_source_urls": web_identity.get("source_urls") or [],
                    "attempts": attempts,
                })
                _log_resolution(query, "web_address_retry", "success",
                                f"resolved_name={retry.get('resolved_name')!r}")
                return retry
        elif web_identity and web_identity.get("status") == "ambiguous":
            return {**web_identity, "input": query, "attempts": attempts}

    area = ((known or {}).get("area") or (web_identity or {}).get("area"))
    if not area:
        parts = [part.strip() for part in str(query or "").split(",") if part.strip()]
        if len(parts) >= 2:
            area = ", ".join(parts[1:])
    attempts.append("area_fallback")
    if area:
        try:
            fallback = provider.resolve_place(area)
        except LocationProviderError as error:
            fallback = {"status": "error", "reason": error.reason, "error": str(error)}
        if fallback.get("status") == "resolved":
            fallback.update({
                "input": " ".join(str(query or "").split()),
                "resolved_name": fallback.get("resolved_name") or area,
                "resolution_level": "area", "location_level": "area",
                "resolution_method": "area_fallback", "approximate": True,
                "attempts": attempts,
            })
            _log_resolution(query, "area_fallback", "success",
                            f"resolved_name={fallback.get('resolved_name')!r}")
            return fallback
    _log_resolution(query, "area_fallback", "miss")
    return {"status": "error", "reason": "location_not_resolved",
            "input": " ".join(str(query or "").split()),
            "error": result.get("error") or "Location could not be resolved.",
            "attempts": attempts}


def get_travel_time(origin, destination, mode="driving", provider=None,
                    coordinate_resolver=None, web_resolver=None):
    started = time.perf_counter()
    provider = provider or GoogleMapsProvider()
    if mode not in {"driving", "walking"}:
        return {"status": "error", "reason": "unsupported_mode",
                "error": "Only driving and walking modes are supported."}
    resolved_origin = _safe_resolve(
        provider, origin, coordinate_resolver, web_resolver
    )
    resolved_destination = _safe_resolve(
        provider, destination, coordinate_resolver, web_resolver
    )
    if resolved_origin.get("status") != "resolved" or resolved_destination.get("status") != "resolved":
        return {"status": "error", "origin": resolved_origin,
                "destination": resolved_destination, "source": provider.name}
    try:
        route = provider.route_matrix(
            [resolved_origin], [resolved_destination], mode=mode
        ).get((0, 0))
    except LocationProviderError as error:
        return {"status": "error", "origin": resolved_origin,
                "destination": resolved_destination, "error": str(error),
                "reason": error.reason,
                "source": provider.name}
    result = {"status": "ok", "origin": resolved_origin,
              "destination": resolved_destination, "source": provider.name,
              "mode": mode, "provider_duration_ms": round((time.perf_counter() - started) * 1000)}
    result.update(route or {"status": "error", "error": "No route was returned."})
    return result


def find_nearby_places(origin, categories, travel_mode="walking",
                       max_travel_minutes=None, max_results=None, provider=None,
                       coordinate_resolver=None, web_resolver=None):
    """Discover nearby amenities, batch-route them, then apply route-time filtering."""
    started = time.perf_counter()
    provider = provider or GoogleMapsProvider()
    if travel_mode not in {"walking", "driving"}:
        return {"status": "error", "reason": "unsupported_mode",
                "error": "Only walking and driving modes are supported."}
    try:
        max_minutes = int(max_travel_minutes) if max_travel_minutes is not None else None
        result_limit = int(max_results) if max_results is not None else 10
    except (TypeError, ValueError):
        return {"status": "error", "reason": "invalid_request",
                "error": "Travel minutes and result limit must be integers."}
    if max_minutes is not None and not 1 <= max_minutes <= 60:
        return {"status": "error", "reason": "invalid_request",
                "error": "Maximum travel time must be between 1 and 60 minutes."}
    if not 1 <= result_limit <= 20:
        return {"status": "error", "reason": "invalid_request",
                "error": "Maximum results must be between 1 and 20."}
    if max_minutes is None and travel_mode == "walking":
        max_minutes = 10
    place_types = nearby_place_types(categories)
    if not place_types:
        return {"status": "error", "reason": "unsupported_categories",
                "error": "No supported nearby-place category was supplied."}
    resolved_origin = _safe_resolve(
        provider, origin, coordinate_resolver, web_resolver
    )
    if resolved_origin.get("status") != "resolved":
        return {"status": "error", "reason": "origin_not_resolved",
                "origin": resolved_origin, "source": provider.name}
    radius_m = nearby_search_radius(travel_mode, max_minutes)
    discovery_limit = min(20, max(10, result_limit * 2))
    try:
        raw_places = provider.search_nearby(
            resolved_origin, place_types, radius_m, discovery_limit
        )
    except LocationProviderError as error:
        return {"status": "error", "reason": error.reason,
                "error": str(error), "origin": resolved_origin,
                "source": provider.name}
    deduplicated = []
    seen = set()
    for item in raw_places:
        display = item.get("displayName") or {}
        name = display.get("text") if isinstance(display, dict) else display
        location = item.get("location") or {}
        latitude, longitude = location.get("latitude"), location.get("longitude")
        identity = item.get("id") or (
            _normalize_place_text(name), _normalize_place_text(item.get("formattedAddress"))
        )
        if identity in seen or latitude is None or longitude is None:
            continue
        seen.add(identity)
        deduplicated.append({
            "name": name or item.get("formattedAddress") or "Unnamed place",
            "place_id": item.get("id"), "primary_type": item.get("primaryType"),
            "formatted_address": item.get("formattedAddress"),
            "latitude": float(latitude), "longitude": float(longitude),
            "rating": item.get("rating"),
            "user_rating_count": item.get("userRatingCount"),
            "business_status": item.get("businessStatus"),
        })
    if not deduplicated:
        return {"status": "ok", "origin": resolved_origin,
                "travel_mode": travel_mode, "max_travel_minutes": max_minutes,
                "categories": list(categories or []), "google_place_types": place_types,
                "places": [], "candidate_count": 0, "radius_m": radius_m,
                "source": provider.name,
                "provider_duration_ms": round((time.perf_counter() - started) * 1000)}
    try:
        matrix = provider.route_matrix(
            [resolved_origin], deduplicated, mode=travel_mode
        )
    except LocationProviderError as error:
        return {"status": "partial", "reason": "routing_failed",
                "error": str(error), "origin": resolved_origin,
                "travel_mode": travel_mode, "max_travel_minutes": max_minutes,
                "categories": list(categories or []), "google_place_types": place_types,
                "places": [], "unrouted_places": [
                    {key: value for key, value in item.items()
                     if key not in {"latitude", "longitude"}}
                    for item in deduplicated
                ], "candidate_count": len(deduplicated), "radius_m": radius_m,
                "source": provider.name}
    places, unrouted = [], []
    for index, item in enumerate(deduplicated):
        route = matrix.get((0, index))
        public_item = {key: value for key, value in item.items()
                       if key not in {"latitude", "longitude"} and value is not None}
        if not route or route.get("status") != "ok":
            unrouted.append(public_item)
            continue
        public_item.update(route)
        duration_within_limit = (
            max_minutes is None
            or (route.get("duration_seconds") is not None
                and route["duration_seconds"] <= max_minutes * 60)
            or (route.get("duration_seconds") is None
                and route["duration_minutes"] <= max_minutes)
        )
        if duration_within_limit:
            places.append(public_item)
    places.sort(key=lambda item: (
        item.get("duration_minutes", float("inf")),
        item.get("distance_m", float("inf")), item["name"].casefold(),
    ))
    return {
        "status": "ok" if places or not unrouted else "partial",
        "origin": resolved_origin, "travel_mode": travel_mode,
        "max_travel_minutes": max_minutes,
        "categories": list(categories or []), "google_place_types": place_types,
        "places": places[:result_limit], "unrouted_places": unrouted,
        "candidate_count": len(deduplicated), "routed_count": len(deduplicated) - len(unrouted),
        "within_threshold": len(places), "radius_m": radius_m,
        "source": provider.name,
        "provider_duration_ms": round((time.perf_counter() - started) * 1000),
    }


def compare_locations(candidate_locations, destinations, provider=None,
                      coordinate_resolver=None, web_resolver=None):
    started = time.perf_counter()
    provider = provider or GoogleMapsProvider()
    destination_items = [item for item in destinations or [] if isinstance(item, dict)]
    resolved_candidates = [
        _safe_resolve(provider, name, coordinate_resolver, web_resolver)
        for name in candidate_locations or []
    ]
    resolved_destinations = [
        _safe_resolve(provider, item.get("name"), coordinate_resolver, web_resolver)
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
                mode="driving",
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
