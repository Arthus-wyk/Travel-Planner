"""Amap search, geocoding, static maps and route operations."""
from functools import wraps
from typing import Any, Callable

from service.mcp.server import (
    amap_compute_route, amap_search_hotels, amap_search_places, amap_geocode, amap_static_map,
)


def compact_map_result(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """为模型保留地图决策字段，错误详情用于参数纠错。"""
    if result.get("success") is False:
        return result
    if tool in {"amap_search_places", "amap_search_hotels", "search_review_places", "search_review_hotels"}:
        return {
            "success": result["success"], "query": result.get("query", ""),
            "places": [
                {key: place[key] for key in (
                    "place_id", "name", "address", "latitude", "longitude", "coordinate_system",
                    "types", "rating", "cost_per_person", "user_rating_count", "price_level", "business_status",
                ) if key in place and place[key] is not None} for place in result.get("places", [])
            ],
        }
    if tool in {"amap_compute_route", "compare_review_route"}:
        return {key: result[key] for key in (
            "success", "origin", "destination", "travel_mode", "distance_meters", "duration", "fare",
        ) if key in result}
    if tool == "amap_geocode":
        return {
            **{key: result[key] for key in ("success", "coordinate_system", "message") if key in result},
            "geocodes": [{key: match[key] for key in (
                "formatted_address", "location", "province", "city", "district", "level",
            ) if key in match and match[key] is not None} for match in result.get("geocodes", [])],
        }
    if tool == "amap_static_map":
        summary = {key: result[key] for key in (
            "success", "center", "coordinate_system", "image_cache_key",
        ) if key in result}
        if result.get("image_cache_key"):
            summary["image_url"] = f"/api/maps/images/{result['image_cache_key']}"
        return summary
    raise ValueError(f"Unsupported map tool: {tool}")


def _compact_tool(function: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    # FastMCP 通过原函数签名生成参数 Schema。
    @wraps(function)
    def invoke(*args, **kwargs):
        return compact_map_result(function.__name__, function(*args, **kwargs))

    return invoke


amap_search_places = _compact_tool(amap_search_places)
amap_search_hotels = _compact_tool(amap_search_hotels)
amap_compute_route = _compact_tool(amap_compute_route)
amap_geocode = _compact_tool(amap_geocode)
amap_static_map = _compact_tool(amap_static_map)
amap_static_map.__doc__ = """Fetch an Amap static image and return image_cache_key and image_url.

    image_url is relative to this application's HTTP API. Coordinates use GCJ-02.
    center and up to 10 markers accept addresses or longitude,latitude.
    zoom is 1-17; width and height are 1-1024 pixels.
    """

__all__ = ["amap_compute_route", "amap_search_hotels", "amap_search_places", "amap_geocode", "amap_static_map"]
