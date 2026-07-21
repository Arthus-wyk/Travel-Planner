from tools.maps import service

TOOL_NAMES = ("google_maps_search_places", "google_maps_search_hotels", "google_maps_compute_route")
TOOL_DOMAINS = {"google_maps_search_places": {"place", "restaurant", "recommendation"}, "google_maps_search_hotels": {"hotel"}, "google_maps_compute_route": {"route"}}


def register(mcp) -> None:
    for name in TOOL_NAMES:
        mcp.tool()(getattr(service, name))
