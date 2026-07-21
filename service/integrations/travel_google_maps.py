from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from service.config import (
    GOOGLE_MAPS_API_KEY,
    GOOGLE_MAPS_TIMEOUT_SECONDS,
    GOOGLE_PLACES_FIELD_MASK,
    GOOGLE_PLACES_TEXT_SEARCH_URL,
    GOOGLE_ROUTES_COMPUTE_URL,
    GOOGLE_ROUTES_FIELD_MASK,
)


def google_maps_error(exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "message": f"Google Maps API error: {type(exc).__name__}: {exc}",
    }


def build_places_payload(
    query: str,
    max_results: int,
    language_code: str,
    region_code: str | None,
    location_bias_latitude: float | None,
    location_bias_longitude: float | None,
    location_bias_radius_meters: float,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not query or not query.strip():
        return None, {
            "success": False,
            "message": "query must not be empty.",
        }

    try:
        max_result_count = clamp_result_count(max_results)
    except ValueError as exc:
        return None, {
            "success": False,
            "message": str(exc),
        }

    payload: dict[str, Any] = {
        "textQuery": query.strip(),
        "maxResultCount": max_result_count,
        "languageCode": language_code,
    }
    if region_code:
        payload["regionCode"] = region_code

    if location_bias_latitude is not None or location_bias_longitude is not None:
        if location_bias_latitude is None or location_bias_longitude is None:
            return None, {
                "success": False,
                "message": (
                    "location_bias_latitude and location_bias_longitude "
                    "must be provided together."
                ),
            }
        if isinstance(location_bias_radius_meters, bool) or not isinstance(
            location_bias_radius_meters,
            (int, float),
        ):
            return None, {
                "success": False,
                "message": "location_bias_radius_meters must be a number.",
            }
        if location_bias_radius_meters <= 0:
            return None, {
                "success": False,
                "message": "location_bias_radius_meters must be positive.",
            }
        payload["locationBias"] = {
            "circle": {
                "center": {
                    "latitude": location_bias_latitude,
                    "longitude": location_bias_longitude,
                },
                "radius": location_bias_radius_meters,
            }
        }

    return payload, None


def search_places(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = post_google_json(
        GOOGLE_PLACES_TEXT_SEARCH_URL,
        payload,
        GOOGLE_PLACES_FIELD_MASK,
    )
    return response.get("places", [])


def place_summary(place: dict[str, Any]) -> dict[str, Any]:
    display_name = place.get("displayName") or {}
    location = place.get("location") or {}
    return {
        "place_id": place.get("id"),
        "name": display_name.get("text"),
        "address": place.get("formattedAddress"),
        "latitude": location.get("latitude"),
        "longitude": location.get("longitude"),
        "rating": place.get("rating"),
        "user_rating_count": place.get("userRatingCount"),
        "price_level": place.get("priceLevel"),
        "business_status": place.get("businessStatus"),
        "types": place.get("types", []),
        "google_maps_uri": place.get("googleMapsUri"),
    }


def build_route_payload(
    origin: str,
    destination: str,
    travel_mode: str,
    language_code: str,
    units: str,
    avoid_tolls: bool,
    avoid_highways: bool,
    avoid_ferries: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str, str]:
    normalized_travel_mode = travel_mode.upper()
    normalized_units = units.upper()

    if normalized_travel_mode not in {
        "DRIVE",
        "WALK",
        "BICYCLE",
        "TWO_WHEELER",
        "TRANSIT",
    }:
        return None, {
            "success": False,
            "message": (
                "travel_mode must be DRIVE, WALK, BICYCLE, TWO_WHEELER, "
                "or TRANSIT."
            ),
        }, normalized_travel_mode, normalized_units

    if normalized_units not in {"METRIC", "IMPERIAL"}:
        return None, {
            "success": False,
            "message": "units must be METRIC or IMPERIAL.",
        }, normalized_travel_mode, normalized_units

    try:
        payload: dict[str, Any] = {
            "origin": address_waypoint(origin),
            "destination": address_waypoint(destination),
            "travelMode": normalized_travel_mode,
            "languageCode": language_code,
            "units": normalized_units,
            "computeAlternativeRoutes": False,
        }
    except ValueError as exc:
        return None, {
            "success": False,
            "message": str(exc),
        }, normalized_travel_mode, normalized_units

    if normalized_travel_mode in {"DRIVE", "TWO_WHEELER"}:
        payload["routingPreference"] = "TRAFFIC_AWARE"
        payload["routeModifiers"] = {
            "avoidTolls": avoid_tolls,
            "avoidHighways": avoid_highways,
            "avoidFerries": avoid_ferries,
        }

    return payload, None, normalized_travel_mode, normalized_units


def compute_route(payload: dict[str, Any]) -> list[dict[str, Any]]:
    response = post_google_json(
        GOOGLE_ROUTES_COMPUTE_URL,
        payload,
        GOOGLE_ROUTES_FIELD_MASK,
    )
    return response.get("routes", [])


def post_google_json(
    url: str,
    payload: dict[str, Any],
    field_mask: str,
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": require_google_maps_api_key(),
            "X-Goog-FieldMask": field_mask,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=GOOGLE_MAPS_TIMEOUT_SECONDS,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {details}") from exc


def require_google_maps_api_key() -> str:
    if not GOOGLE_MAPS_API_KEY:
        raise RuntimeError("Set GOOGLE_MAPS_API_KEY before using Google Maps tools.")
    return GOOGLE_MAPS_API_KEY


def clamp_result_count(value: int, maximum: int = 20) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("max_results must be an integer.")
    if value < 1:
        raise ValueError("max_results must be at least 1.")
    return min(value, maximum)


def address_waypoint(value: str) -> dict[str, Any]:
    if not value or not value.strip():
        raise ValueError("Route waypoint addresses must not be empty.")
    return {"address": value.strip()}
