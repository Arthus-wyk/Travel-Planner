from tools.maps import service

TOOL_NAMES = ("amap_search_places", "amap_search_hotels", "amap_compute_route", "amap_geocode", "amap_static_map")
TOOL_DOMAINS = {"amap_search_places": {"place", "restaurant", "recommendation"}, "amap_search_hotels": {"hotel"}, "amap_compute_route": {"route"}, "amap_geocode": {"place", "hotel", "route"}, "amap_static_map": {"place", "route"}}


def register(mcp) -> None:
    for name in TOOL_NAMES:
        mcp.tool()(getattr(service, name))
