"""Amap search, geocoding, static maps and route operations."""
from service.mcp.server import (
    amap_compute_route, amap_search_hotels, amap_search_places, amap_geocode, amap_static_map,
)

__all__ = ["amap_compute_route", "amap_search_hotels", "amap_search_places", "amap_geocode", "amap_static_map"]
