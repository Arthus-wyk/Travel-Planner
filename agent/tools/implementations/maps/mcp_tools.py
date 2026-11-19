"""Register typed maps tools with their existing domain handlers."""
from agent.tools.implementations.maps import service
from agent.tools.schemas import maps as schemas
from agent.tools.schemas.inputs import ToolSpec

TOOL_NAMES = ("amap_search_places", "amap_search_hotels", "amap_compute_route", "amap_geocode", "amap_static_map")
TOOL_DOMAINS = {"amap_search_places": {"place", "restaurant", "recommendation"}, "amap_search_hotels": {"hotel"}, "amap_compute_route": {"route"}, "amap_geocode": {"place", "hotel", "route"}, "amap_static_map": {"place", "route"}}

TOOL_RISKS = {}


def specs() -> list[ToolSpec]:
    contracts = (
        ("amap_search_places", schemas.AmapSearchPlacesInput, schemas.AmapSearchPlacesOutput, True),
        ("amap_search_hotels", schemas.AmapSearchHotelsInput, schemas.AmapSearchHotelsOutput, True),
        ("amap_compute_route", schemas.AmapComputeRouteInput, schemas.AmapComputeRouteOutput, True),
        ("amap_geocode", schemas.AmapGeocodeInput, schemas.AmapGeocodeOutput, True),
        ("amap_static_map", schemas.AmapStaticMapInput, schemas.AmapStaticMapOutput, True),
    )
    return [
        ToolSpec(name, handler.__doc__ or name, inputs, handler, frozenset(TOOL_DOMAINS[name]),
                 TOOL_RISKS.get(name, "low"), output_model=outputs, replay_safe=replay_safe)
        for name, inputs, outputs, replay_safe in contracts
        for handler in [getattr(service, name)]
    ]
