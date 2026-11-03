"""Amap Web Service adapter; coordinates use GCJ-02, longitude first."""
from __future__ import annotations

import base64
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from hashlib import sha256
from typing import Any
from time import perf_counter
from uuid import uuid4
from service.utils.request_log import write_event, log_error, body_value

from service.config import AMAP_API_KEY, AMAP_TIMEOUT_SECONDS
from service.storage.travel_db import read_map_cache, write_map_cache

logger = logging.getLogger(__name__)


def cache_key(path: str, params: dict) -> str:
    return sha256(json.dumps([path, params], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def amap_error(exc: Exception) -> dict[str, Any]:
    message = str(exc).replace(AMAP_API_KEY, "[redacted]") if AMAP_API_KEY else str(exc)
    return {"success": False, "message": f"Amap API error: {type(exc).__name__}: {message}"}


def request_amap(path: str, params: dict, image: bool = False):
    key = cache_key(path, params)
    cached = read_map_cache(key)
    if cached is not None:
        return cached
    if not AMAP_API_KEY:
        raise RuntimeError("Set AMAP_API_KEY in .env before using Amap tools.")
    url = "https://restapi.amap.com" + path + "?" + urllib.parse.urlencode({**params, "key": AMAP_API_KEY})
    details = {"call_id": uuid4().hex, "method": "GET", "url": "https://restapi.amap.com" + path,
               "direction": "outbound", "params": params}
    started = perf_counter()
    write_event("api.requests", details)
    try:
        with urllib.request.urlopen(url, timeout=AMAP_TIMEOUT_SECONDS) as response:
            body = response.read()
            mime = response.headers.get_content_type()
            write_event("api.responses", {**details, "status": getattr(response, "status", 200),
                        "headers": dict(response.headers), "body": body_value(body),
                        "duration_ms": round((perf_counter() - started) * 1000, 2)})
    except urllib.error.HTTPError as exc:
        result = {**details, "status": exc.code, "body": body_value(exc.read()),
                  "duration_ms": round((perf_counter() - started) * 1000, 2)}
        write_event("api.responses", result)
        log_error(exc, **result)
        raise RuntimeError(f"Amap HTTP {exc.code}") from None
    except Exception as exc:
        log_error(exc, **details)
        if isinstance(exc, urllib.error.URLError):
            raise RuntimeError("Amap network request failed") from None
        raise
    if image and mime in {"image/png", "image/jpeg", "image/gif"}:
        data = f"data:{mime};base64," + base64.b64encode(body).decode("ascii")
        write_map_cache(key, data, 30 * 86400)
        return data
    try:
        data = json.loads(body)
    except ValueError as exc:
        log_error(exc, **details)
        raise
    if str(data.get("status")) != "1":
        if path == "/v3/staticmap":
            logger.error("Amap static map failed: infocode=%s info=%s params=%s url_length=%d",
                         data.get("infocode"), data.get("info"), params, len(url))
        error = RuntimeError(f"{data.get('infocode')}: {data.get('info', 'Request failed')}")
        log_error(error, **details, body=data)
        raise error
    if image:
        raise RuntimeError("Amap did not return a map image")
    # Empty results may be transient; never persist failures or empty searches.
    route = data.get("route")
    nonempty = route.get("paths", route.get("transits")) if isinstance(route, dict) else data.get("pois", data.get("geocodes", [True]))
    if nonempty:
        write_map_cache(key, data, 86400 if "/direction/" in path else 30 * 86400)
    return data


def coordinates(value: str) -> str:
    lon, lat = (float(part) for part in value.split(","))
    if not (math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90):
        raise ValueError("Invalid longitude/latitude")
    return f"{lon:.6f},{lat:.6f}"


def geocode(address: str, city: str = "") -> list[dict]:
    if not address or not address.strip():
        raise ValueError("address must not be empty")
    params = {"address": address.strip()}
    if city:
        params["city"] = city
    return request_amap("/v3/geocode/geo", params).get("geocodes", [])


def resolve_location(value: str) -> dict:
    if not value or not value.strip():
        raise ValueError("Location must not be empty")
    try:
        return {"location": coordinates(value)}
    except ValueError:
        if "," in value and all(part.strip().replace(".", "").replace("-", "").isdigit() for part in value.split(",")):
            raise ValueError("Invalid longitude/latitude")
    matches = geocode(value)
    if not matches:
        matches = search_places({"keywords": value, "offset": 1, "extensions": "all"})
    if not matches:
        raise ValueError(f"No location found for {value}")
    match = matches[0]
    return {**match, "location": coordinates(match["location"])}


def build_places_payload(query, max_results, language_code, region_code,
                         location_bias_latitude, location_bias_longitude, location_bias_radius_meters):
    try:
        if not query or not query.strip():
            raise ValueError("query must not be empty")
        if isinstance(max_results, bool) or not isinstance(max_results, int) or max_results < 1:
            raise ValueError("max_results must be a positive integer")
        payload = {"keywords": query.strip(), "offset": min(max_results, 20), "extensions": "all"}
        if region_code:
            payload["city"] = region_code
        if location_bias_latitude is not None or location_bias_longitude is not None:
            if location_bias_latitude is None or location_bias_longitude is None:
                raise ValueError("latitude and longitude must be provided together")
            payload["location"] = coordinates(f"{location_bias_longitude},{location_bias_latitude}")
            if isinstance(location_bias_radius_meters, bool) or not isinstance(location_bias_radius_meters, (int, float)) or not 1 <= location_bias_radius_meters <= 50000:
                raise ValueError("radius must be between 1 and 50000 meters")
            payload["radius"] = int(location_bias_radius_meters)
        return payload, None
    except (ValueError, TypeError) as exc:
        return None, {"success": False, "message": str(exc)}


def search_places(payload: dict) -> list[dict]:
    endpoint = "around" if "location" in payload else "text"
    return request_amap(f"/v3/place/{endpoint}", payload).get("pois", [])


def place_summary(place: dict) -> dict:
    lon, lat = coordinates(place["location"]).split(",")
    business = place.get("biz_ext") or {}
    rating = business.get("rating")
    try:
        per_person = float(business.get("cost"))
        if not math.isfinite(per_person) or per_person <= 0:
            per_person = None
    except (TypeError, ValueError):
        per_person = None
    return {"place_id": place.get("id"), "name": place.get("name"),
            "address": "".join(str(place.get(key) or "") for key in ("pname", "cityname", "adname", "address")) or place.get("name"),
            "latitude": float(lat), "longitude": float(lon), "coordinate_system": "GCJ-02",
            "citycode": place.get("citycode"), "adcode": place.get("adcode"),
            "rating": float(rating) if rating and rating != "[]" else None,
            "cost_per_person": per_person,
            "user_rating_count": None, "price_level": None, "business_status": None,
            "types": (place.get("type") or "").split(";"),
            "amap_uri": "https://uri.amap.com/marker?" + urllib.parse.urlencode({"position": f"{lon},{lat}", "name": place.get("name", "")})}


def build_route_payload(origin, destination, travel_mode, language_code, units,
                        avoid_tolls, avoid_highways, avoid_ferries):
    mode, units = travel_mode.upper(), units.upper()
    try:
        if mode not in {"DRIVE", "WALK", "BICYCLE", "TWO_WHEELER", "TRANSIT"}:
            raise ValueError("Unsupported travel_mode")
        if units not in {"METRIC", "IMPERIAL"}:
            raise ValueError("units must be METRIC or IMPERIAL")
        if not origin or not origin.strip() or not destination or not destination.strip():
            raise ValueError("Route waypoint addresses must not be empty")
        if avoid_ferries:
            raise ValueError("Amap does not support avoid_ferries in this adapter")
        if mode != "DRIVE" and (avoid_tolls or avoid_highways):
            raise ValueError("Road avoidance options require DRIVE")
        return {"origin": origin.strip(), "destination": destination.strip(), "travel_mode": mode,
                "strategy": 43 if avoid_tolls and avoid_highways else 41 if avoid_tolls else 40 if avoid_highways else 32}, None, mode, units
    except ValueError as exc:
        return None, {"success": False, "message": str(exc)}, mode, units


def compute_route(payload: dict) -> list[dict]:
    origin, destination = resolve_location(payload["origin"]), resolve_location(payload["destination"])
    mode = payload["travel_mode"]
    endpoint = {"DRIVE": "driving", "WALK": "walking", "BICYCLE": "bicycling",
                "TWO_WHEELER": "electrobike", "TRANSIT": "transit/integrated"}[mode]
    params = {"origin": origin["location"], "destination": destination["location"], "show_fields": "cost,polyline"}
    if mode == "DRIVE":
        params["strategy"] = payload["strategy"]
    if mode == "TRANSIT":
        for point, city_key in ((origin, "city1"), (destination, "city2")):
            if not point.get("citycode"):
                component = request_amap("/v3/geocode/regeo", {"location": point["location"]}).get("regeocode", {}).get("addressComponent", {})
                point["citycode"] = component.get("citycode")
            if not point.get("citycode"):
                raise ValueError("Could not determine citycode for transit route")
            params[city_key] = point["citycode"]
    data = request_amap("/v5/direction/" + endpoint, params).get("route") or {}
    paths = data.get("transits" if mode == "TRANSIT" else "paths") or []
    result = []
    for path in paths:
        duration = (path.get("cost") or {}).get("duration", path.get("duration"))
        if duration in (None, "", []):
            raise ValueError("Amap route is missing duration")
        fare = (path.get("cost") or {}).get("transit_fee") if mode == "TRANSIT" else data.get("taxi_cost") if mode == "DRIVE" else None
        if fare in (None, "", []):
            fare = (path.get("cost") or {}).get("taxi_fee") if mode == "DRIVE" else None
        if fare in (None, "", []) and mode == "TRANSIT":
            segment_fares = [(segment.get("cost") or {}).get("transit_fee") for segment in path.get("segments", [])]
            if segment_fares and all(value not in (None, "", []) for value in segment_fares):
                try:
                    fare = sum(float(value) for value in segment_fares)
                except (TypeError, ValueError):
                    fare = None
        try:
            fare = float(fare)
            if not math.isfinite(fare) or fare < 0:
                fare = None
        except (TypeError, ValueError):
            fare = None
        result.append({"distanceMeters": int(float(path["distance"])),
                       "duration": f"{int(float(duration))}s", "legs": path.get("segments", path.get("steps", [])),
                       "source": "高德地图", "amap_route": path,
                       "fare": fare})
    return result


def static_map(center: str, zoom: int = 13, width: int = 600, height: int = 400,
               markers: list[str] | None = None, paths: list[str] | None = None) -> dict:
    for name, value, maximum in (("zoom", zoom, 17), ("width", width, 1024), ("height", height, 1024)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise ValueError(f"{name} must be an integer from 1 to {maximum}")
    if markers is not None and (not isinstance(markers, list) or len(markers) > 10):
        raise ValueError("markers must be a list of at most 10 locations")
    location = resolve_location(center)["location"]
    params = {"location": location, "zoom": zoom, "size": f"{width}*{height}"}
    if markers:
        params["markers"] = "|".join(f"mid,0xFF0000,{index}:" + resolve_location(item)["location"] for index, item in enumerate(markers, 1))
    if paths:
        if len(paths) > 4:
            raise ValueError("At most four map paths are supported")
        params["paths"] = "|".join("5,0x4F46E5,1,,0:" + ";".join(coordinates(p) for p in path.split(";")) for path in paths)
    return {"success": True, "center": location, "coordinate_system": "GCJ-02",
            "image_data_url": request_amap("/v3/staticmap", params, image=True),
            "image_cache_key": cache_key("/v3/staticmap", params)}
