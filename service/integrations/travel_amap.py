"""Amap Web Service adapter; coordinates use GCJ-02, longitude first."""
from __future__ import annotations

import base64
import json
import logging
import math
import urllib.error
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any
from datetime import datetime, timedelta, timezone
from random import uniform
from threading import Lock
from time import monotonic, perf_counter, sleep
from uuid import uuid4
from service.utils.request_log import write_event, log_error, body_value

from service.config import AMAP_API_KEY, AMAP_TIMEOUT_SECONDS
from service.storage.travel_db import read_map_cache, write_map_cache

logger = logging.getLogger(__name__)


def cache_key(path: str, params: dict) -> str:
    return sha256(json.dumps([path, params], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


QPS_CODES = {"10014", "10015", "10019", "10020", "10021"}
DAILY_CODES = {"10003", "10044", "10045"}
# shortcut: cooldown is process-local, share state if multiple workers use the account.
_cooldowns = {}
_cooldown_lock = Lock()


class AmapRequestError(RuntimeError):
    def __init__(self, code, info):
        self.code = str(code)
        self.info = str(info)
        reason = "接口请求过于频繁，请稍后重试" if self.code in QPS_CODES | {"10004"} else (
            "日调用额度已用尽" if self.code in DAILY_CODES else "请求失败")
        super().__init__(f"高德{reason}（{self.code} {self.info}）")


class AmapHttpError(RuntimeError):
    def __init__(self, status, retry_after):
        self.status = status
        self.retry_after = None
        if status == 429 and retry_after:
            try:
                self.retry_after = max(0, int(retry_after))
            except ValueError:
                try:
                    self.retry_after = max(0, (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
        super().__init__(f"高德请求失败（HTTP {status}）")


def amap_error(exc: Exception) -> dict[str, Any]:
    message = str(exc).replace(AMAP_API_KEY, "[redacted]") if AMAP_API_KEY else str(exc)
    if isinstance(exc, AmapHttpError):
        return {"success": False, "message": message, "http_status": exc.status, "retry_after": exc.retry_after}
    if isinstance(exc, AmapRequestError):
        return {"success": False, "message": message, "infocode": exc.code}
    return {"success": False, "message": f"Amap API error: {type(exc).__name__}: {message}"}


def retry_amap_call(call, *args, **kwargs):
    for attempt in range(3):
        retry_after = None
        try:
            result = call(*args, **kwargs)
        except (TimeoutError, ConnectionError):
            if attempt == 1:
                raise
        else:
            code = result.get("infocode")
            message = str(result.get("message", "")).lower()
            status = result.get("http_status")
            retry_after = result.get("retry_after")
            transient = status in {429, 500, 502, 503, 504} or code in QPS_CODES | {"10016"} or (not code and any(marker in message for marker in (
                "timeout", "timed out", "network request failed", "http 429",
                "http 500", "http 502", "http 503", "http 504",
            )))
            last_attempt = 2 if code in QPS_CODES | {"10016"} or status == 429 else 1
            if result.get("success") or not transient or attempt >= last_attempt or (retry_after or 0) > 2:
                return result
        sleep(max(retry_after or 0, (0.5, 1.5)[attempt] + uniform(0, 0.2)))


def _wait_for_amap(path):
    deadline = monotonic() + 2
    while True:
        with _cooldown_lock:
            cooldown = _cooldowns.get(path)
            if cooldown is None:
                return
            until, pace_until, error = cooldown
            now = monotonic()
            if now >= pace_until:
                _cooldowns.pop(path)
                return
            if until <= now:
                _cooldowns[path] = (now + 0.2, pace_until, error)
                return
            if until > deadline:
                raise AmapRequestError(error.code, error.info)
        sleep(max(0, until - now))


def _record_amap_limit(path, error):
    if error.code in QPS_CODES:
        delay, pacing = 0.5, 5
    elif error.code in DAILY_CODES | {"10004"}:
        now = datetime.now(timezone(timedelta(hours=8)))
        reset = (now.replace(second=0, microsecond=0) + timedelta(minutes=1)
                 if error.code == "10004" else
                 now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        delay, pacing = (reset - now).total_seconds(), 0
    else:
        return
    with _cooldown_lock:
        now = monotonic()
        if now + delay >= _cooldowns.get(path, (0,))[0]:
            _cooldowns[path] = (now + delay, now + delay + pacing, error)


def request_amap(path: str, params: dict, image: bool = False, *, keep_cached: bool = False, refresh: bool = False):
    key = cache_key(path, params)
    cached = None if refresh else read_map_cache(key, allow_expired=True) if keep_cached else read_map_cache(key)
    if cached is not None:
        return cached
    if not AMAP_API_KEY:
        raise RuntimeError("Set AMAP_API_KEY in .env before using Amap tools.")
    _wait_for_amap(path)
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
        raise AmapHttpError(exc.code, exc.headers.get("Retry-After") if exc.headers else None) from None
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
        error = AmapRequestError(data.get("infocode"), data.get("info", "Request failed"))
        _record_amap_limit(path, error)
        log_error(error, **details, body=data)
        raise error
    if image:
        raise RuntimeError("Amap did not return a map image")
    # Empty results may be transient; never persist failures or empty searches.
    route = data.get("route")
    nonempty = route.get("paths", route.get("transits")) if isinstance(route, dict) else data.get("pois", data.get("geocodes", [True]))
    if nonempty or keep_cached:
        write_map_cache(key, data, 86400 if "/direction/" in path else 30 * 86400)
    return data


_photo_locks = tuple(Lock() for _ in range(32))


def place_photo_url(poi_id: str) -> str | None:
    with _photo_locks[hash(poi_id) % len(_photo_locks)]:
        data = request_amap("/v5/place/detail", {"id": poi_id, "show_fields": "photos"}, keep_cached=True)
    for poi in data.get("pois") or []:
        if poi.get("id") != poi_id:
            continue
        for photo in poi.get("photos") or []:
            url = photo.get("url")
            if isinstance(url, str) and url.startswith("https://"):
                return url
    return None


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
            city = str(region_code).strip()
            city_name = city.removesuffix("市") or city
            if city_name and not city.isdecimal() and not payload["keywords"].startswith(city_name):
                payload["keywords"] = f"{city} {payload['keywords']}"
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
    refresh = {"refresh": True} if payload.get("refresh") else {}
    data = request_amap("/v5/direction/" + endpoint, params, **refresh).get("route") or {}
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
