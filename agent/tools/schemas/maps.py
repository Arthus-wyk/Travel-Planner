"""Typed input and business output models for maps tools."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.tools.schemas.inputs import ToolInput


class Place(BaseModel):
    model_config = ConfigDict(extra="allow")

    place_id: str | None = None
    name: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class AmapSearchPlacesInput(ToolInput):
    query: str
    max_results: int = 5
    language_code: str = 'zh-CN'
    region_code: str | None = None
    location_bias_latitude: float | None = None
    location_bias_longitude: float | None = None
    location_bias_radius_meters: float = 5000


class AmapSearchPlacesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str
    places: list[Place]


class AmapSearchHotelsInput(ToolInput):
    destination: str
    query: str = ''
    max_results: int = 5
    language_code: str = 'zh-CN'
    region_code: str | None = None


class AmapSearchHotelsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    query: str
    places: list[Place]


class AmapComputeRouteInput(ToolInput):
    origin: str
    destination: str
    travel_mode: str = 'DRIVE'
    language_code: str = 'zh-CN'
    units: str = 'METRIC'
    avoid_tolls: bool = False
    avoid_highways: bool = False
    avoid_ferries: bool = False
    refresh: bool = False


class AmapComputeRouteOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    origin: str
    destination: str
    travel_mode: str
    distance_meters: int | None = None
    duration: str = Field(pattern=r"^\d+s$")
    fare: float | None = None


class AmapGeocodeInput(ToolInput):
    address: str
    city: str = ''


class AmapGeocodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    coordinate_system: str
    geocodes: list[dict[str, Any]]
    message: str


class AmapStaticMapInput(ToolInput):
    center: str
    zoom: int = Field(default=13, ge=1, le=17)
    width: int = Field(default=600, ge=1, le=1024)
    height: int = Field(default=400, ge=1, le=1024)
    markers: list[str] | None = Field(default=None, max_length=10)


class AmapStaticMapOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    center: str
    coordinate_system: str
    image_cache_key: str
    image_url: str
