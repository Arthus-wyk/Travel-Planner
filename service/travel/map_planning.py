"""Resolve selected POIs and render cached itinerary maps."""
from __future__ import annotations

from copy import deepcopy
from math import asinh, atan, degrees, isfinite, pi, radians, sinh, tan

from service.mcp.server import amap_search_places

from service.integrations.travel_amap import (
    coordinates, geocode, place_summary, request_amap, resolve_location, retry_amap_call, static_map,
)


def has_coordinates(place: dict | None) -> bool:
    if not isinstance(place, dict):
        return False
    lon, lat = place.get("longitude"), place.get("latitude")
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x) for x in (lon, lat)) and -180 <= lon <= 180 and -90 <= lat <= 90


def lookup_review_place(poi_id: str) -> dict:
    if not isinstance(poi_id, str) or not poi_id.strip() or len(poi_id) > 100:
        raise ValueError("请选择搜索结果中的地点")
    pois = request_amap("/v3/place/detail", {"id": poi_id, "extensions": "all"}).get("pois") or []
    try:
        place = place_summary(next(poi for poi in pois if poi.get("id") == poi_id))
        if has_coordinates(place) and isinstance(place["name"], str) and place["name"].strip():
            return place
    except (StopIteration, KeyError, ValueError, TypeError):
        pass
    raise ValueError("该地点没有有效的地图位置，请重新搜索")


def _place_image(place: dict) -> None:
    if place.get("image_cache_key"):
        return
    location = coordinates(f"{place['longitude']},{place['latitude']}")
    try:
        place["image_cache_key"] = static_map(location, markers=[location])["image_cache_key"]
        place.pop("image_error", None)
    except Exception:
        place["image_error"] = "地图图片暂时不可用，地点坐标已保存，可重新确认位置后重试"


def locate_activities(plan: dict) -> dict:
    """Locate parsed places once; downstream routing consumes these saved points."""
    from service.travel.place_matching import compatible_type

    updated = deepcopy(plan)
    for item in updated.get("planning", {}).get("activities", []):
        if has_coordinates(item.get("place")):
            item.setdefault("place_status", "auto_confirmed")
            if item.get("place_status") == "unresolved":
                item["place_status"] = "auto_confirmed"
            _place_image(item["place"])
            continue
        if item.get("choice") == "exclude" or item.get("source") in {"simulation", "simulated_suggestion"}:
            continue
        name = item.get("source_name") or item["name"]
        hint = item.get("location_hint") or ""
        try:
            result = retry_amap_call(amap_search_places, f"{name} {hint}".strip(),
                                     region_code=plan["destination"], max_results=5)
            if not result.get("success"):
                raise ValueError(result.get("message") or "地点搜索失败")
            city = plan["destination"].removesuffix("市")
            candidates = [p for p in result.get("places", []) if has_coordinates(p)
                          and city in (p.get("address") or "") and compatible_type(p, item.get("category"))]
            item["place_candidates"] = candidates
            if not candidates:
                item.update(place_status="failed", place_error="未找到可靠位置，请补充地址后搜索")
                continue
            place = deepcopy(candidates[0])
            _place_image(place)
            item.update(place=place, name=place["name"], location=place["address"], place_status="auto_confirmed")
            item.pop("place_error", None)
        except Exception as exc:
            item.update(place_status="failed", place_error=str(exc)[:300])
    from service.travel.planning import _sync_hotels
    return _sync_hotels(updated)


def resolve_activity_place(plan: dict, activity_id: str, poi_id: str) -> dict:
    updated = deepcopy(plan)
    activity = next((x for x in updated.get("planning", {}).get("activities", []) if x["id"] == activity_id), None)
    if activity is None:
        raise ValueError("Activity not found")
    if not isinstance(poi_id, str) or not poi_id.strip() or len(poi_id) > 100:
        raise ValueError("A POI ID is required")
    pois = request_amap("/v3/place/detail", {"id": poi_id, "extensions": "all"}).get("pois") or []
    if not pois:
        raise ValueError("POI not found; search again")
    place = place_summary(pois[0])
    # Keep the chosen POI point: address geocoding can return a building centroid.
    try:
        place["geocodes"] = geocode(place["address"], plan["destination"])
    except Exception:
        place["geocodes"] = []
    _place_image(place)
    activity.setdefault("source_name", activity["name"])
    activity.update(name=place["name"], location=place["address"], source="高德 POI", place=place, place_status="manual_confirmed")
    activity.pop("place_error", None)
    from service.travel.planning import _sync_hotels
    return _sync_hotels(updated)


def route_polylines(value):
    if isinstance(value, dict):
        if isinstance(value.get("polyline"), str) and value["polyline"]:
            yield value["polyline"]
        else:
            for key, child in value.items():
                # Only the first transit alternative is displayed.
                if key == "buslines" and isinstance(child, list):
                    child = child[:1]
                yield from route_polylines(child)
    elif isinstance(value, list):
        for child in value:
            yield from route_polylines(child)


def _sheet_basemap(points: list[dict], paths: list[str]) -> dict:
    locations = [point["location"] for point in points]
    locations.extend(location for path in paths for location in path.split(";"))
    projected = []
    for location in locations:
        try:
            lon, lat = map(float, coordinates(location).split(","))
            projected.append((lon / 360, -asinh(tan(radians(max(-85, min(85, lat))))) / (2 * pi)))
        except ValueError:
            continue
    if not projected:
        return {}
    x, y = zip(*projected)
    x_range, y_range = max(x) - min(x), max(y) - min(y)
    center = f"{180 * (min(x) + max(x)):.6f},{degrees(atan(sinh(-pi * (min(y) + max(y))))):.6f}"
    width, height = 800, 440
    zoom = next((z for z in range(17, 0, -1)
                 if 256 * 2**z * x_range <= width - 160 and 256 * 2**z * y_range <= height - 128), 1)
    image = static_map(center, zoom=zoom, width=width, height=height)
    return {"center": center, "zoom": zoom, "width": width, "height": height, "image": image["image_data_url"]}


def day_map(plan: dict, day: int, *, include_sheet: bool = False, include_basemap: bool = True) -> dict:
    from datetime import date
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= days:
        raise ValueError("Invalid day")
    items = sorted((x for x in plan["itinerary"] if x["day"] == day), key=lambda x: x["time_of_day"])
    points, warnings, groups, segments = [], [], [], []
    by_id = {x["item_id"]: x for x in items}
    outbound_id = plan.get("planning", {}).get("selected_transport", {}).get("outbound")
    for item in items:
        if item["category"] == "home_anchor" or item.get("lodging_pending"):
            continue
        try:
            place = item.get("place") or {}
            address = item.get("arrival_location", item["location"]) if item.get("reference_id") == outbound_id else item["location"]
            location = coordinates(f"{place['longitude']},{place['latitude']}") if place else resolve_location(address)["location"]
            points.append({"item_id": item["item_id"], "title": item["title"], "location": location})
        except (ValueError, RuntimeError, KeyError) as exc:
            warnings.append(f"{item['title']}：{exc}")
    previous_destination = None
    for route in plan.get("routes", []):
        if (route["origin_item_id"] not in by_id or route["destination_item_id"] not in by_id
                or route.get("intercity") or route.get("source") == "same_place"):
            previous_destination = None
            continue
        origin, destination = by_id[route["origin_item_id"]], by_id[route["destination_item_id"]]
        if (origin.get("lodging_pending") or destination.get("lodging_pending")
                or "home_anchor" in {origin["category"], destination["category"]}
                or destination.get("reference_id") == outbound_id
                or origin["category"] == "intercity_transport" and origin.get("reference_id") != outbound_id):
            previous_destination = None
            continue
        lines = list(route_polylines(route.get("legs", [])))
        if not lines:
            warnings.append("部分路段尚无地图线路，请核对路线计算提示。")
            previous_destination = None
            continue
        if previous_destination == route["origin_item_id"]:
            groups[-1].extend(lines)
        else:
            groups.append(lines)
        previous_destination = route["destination_item_id"]
        if include_sheet and route.get("source") not in {"provisional_estimate", "lodging_pending"}:
            segments.append({"origin_item_id": route["origin_item_id"], "destination_item_id": route["destination_item_id"],
                             "mode": route.get("travel_mode"), "paths": lines})
    if not points:
        raise ValueError("当天没有已定位的游玩地点，请先在地点选择中确认 POI")
    result = {"success": True, "points": points,
              "paths": [line for group in groups for line in group],
              "warnings": list(dict.fromkeys(warnings))}
    if include_sheet:
        result["segments"] = segments
    if include_sheet and include_basemap:
        try:
            result["basemap"] = _sheet_basemap(points, [path for segment in segments for path in segment["paths"]])
        except (ValueError, RuntimeError):
            result["basemap"] = None
    return result


def roadbook_map(plan: dict) -> dict:
    maps = {}
    for day in sorted({item["day"] for item in plan["itinerary"]}):
        try:
            maps[day] = day_map(plan, day, include_sheet=True, include_basemap=False)
        except ValueError:
            continue
    points = [point for data in maps.values() for point in data["points"]]
    paths = [path for data in maps.values() for segment in data["segments"] for path in segment["paths"]]
    try:
        basemap = _sheet_basemap(points, paths)
    except (ValueError, RuntimeError):
        basemap = None
    return {"days": maps, "basemap": basemap}
