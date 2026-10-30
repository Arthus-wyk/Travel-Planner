"""Resolve selected POIs and render cached itinerary maps."""
from __future__ import annotations

from copy import deepcopy
from math import cos, isfinite, log2, radians

from service.mcp.server import amap_search_places

from service.integrations.travel_amap import (
    coordinates, geocode, place_summary, request_amap, resolve_location, static_map,
)


def has_coordinates(place: dict | None) -> bool:
    if not isinstance(place, dict):
        return False
    lon, lat = place.get("longitude"), place.get("latitude")
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x) for x in (lon, lat)) and -180 <= lon <= 180 and -90 <= lat <= 90


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
            result = amap_search_places(f"{name} {hint}".strip(), region_code=plan["destination"], max_results=5)
            if not result.get("success"):
                raise ValueError(result.get("message") or "地点搜索失败")
            candidates = [p for p in result.get("places", []) if has_coordinates(p)]
            city = plan["destination"].removesuffix("市")
            exact = [p for p in candidates if p.get("name") == name and city in (p.get("address") or "")]
            selected = exact[0] if len(exact) == 1 else None
            if selected is None and len(candidates) == 1 and name in (candidates[0].get("name") or "") and city in (candidates[0].get("address") or ""):
                selected = candidates[0]
            item["place_candidates"] = candidates
            if selected is None:
                item["place_status"] = "ambiguous" if candidates else "failed"
                item["place_error"] = "找到多个或名称不明确的地点，请核对具体位置" if candidates else "未找到可靠位置，请补充地址后搜索"
                continue
            place = deepcopy(selected)
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


def day_map(plan: dict, day: int) -> dict:
    from datetime import date
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= days:
        raise ValueError("Invalid day")
    items = sorted((x for x in plan["itinerary"] if x["day"] == day), key=lambda x: x["time_of_day"])
    points, warnings, groups = [], [], []
    by_id = {x["item_id"]: x for x in items}
    outbound_id = plan.get("planning", {}).get("selected_transport", {}).get("outbound")
    for item in items:
        if item["category"] == "home_anchor":
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
        if route["origin_item_id"] not in by_id or route.get("intercity"):
            previous_destination = None
            continue
        origin, destination = by_id[route["origin_item_id"]], by_id[route["destination_item_id"]]
        if ("home_anchor" in {origin["category"], destination["category"]}
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
    if not points:
        raise ValueError("当天没有已定位的游玩地点，请先在地点选择中确认 POI")
    all_coords = [tuple(map(float, p["location"].split(","))) for p in points]
    all_coords += [tuple(map(float, p.split(","))) for group in groups for line in group for p in line.split(";")]
    lons, lats = zip(*all_coords)
    lon, lat = (min(lons) + max(lons)) / 2, (min(lats) + max(lats)) / 2
    span = max((max(lons) - min(lons)) / 360, (max(lats) - min(lats)) / (360 * max(.1, cos(radians(lat)))), .00001)
    zoom = max(1, min(17, int(log2(340 / (256 * span)))))
    paths = []
    if len(groups) > 4:
        warnings.append("当天存在超过四段不连续路线，地图仅绘制前四段；完整路段见行程卡片。")
    for group in groups[:4]:
        # shortcut: sample long polylines for static-map URL limits; use an
        # interactive map if full-resolution geometry is needed.
        simplified = []
        for line in group:
            coords = line.split(";")
            stride = max(1, len(coords) // max(2, 160 // sum(map(len, groups))))
            simplified.extend(coords[::stride])
            simplified.append(coords[-1])
        paths.append(";".join(simplified))
    preview = static_map(f"{lon},{lat}", zoom, 1000, 440,
                         [p["location"] for p in points[:10]], paths or None)
    return {"success": True, "image_cache_key": preview["image_cache_key"], "points": points,
            "warnings": list(dict.fromkeys(warnings)), "marker_limit": len(points) > 10}
