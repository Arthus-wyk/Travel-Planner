"""Typed input and business output models for itinerary tools."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.tools.schemas.inputs import ToolInput


class ItineraryItem(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    item_id: str
    day: int
    title: str


class ItineraryRoute(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    origin_item_id: str = Field(min_length=1)
    destination_item_id: str = Field(min_length=1)


class ReconciledRoute(ItineraryRoute):
    travel_mode: str = Field(min_length=1)
    duration: str = Field(pattern=r"^\d+s$")


class RouteGap(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    origin_item_id: str = Field(min_length=1)
    destination_item_id: str = Field(min_length=1)
    http_status: int | None = None
    infocode: str | None = None


class AddItineraryItemInput(ToolInput):
    plan_id: str
    day: int
    time_of_day: str
    title: str
    location: str
    category: str
    duration_minutes: int | None = None
    estimated_cost: float = 0
    booking_required: bool = False
    notes: str = ''


class AddItineraryItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item: ItineraryItem


class UpdateItineraryItemInput(ToolInput):
    plan_id: str
    item_id: str
    updates: dict[str, Any]


class UpdateItineraryItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item: ItineraryItem


class DeleteItineraryItemInput(ToolInput):
    plan_id: str
    item_id: str


class DeleteItineraryItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    deleted_item_id: str
    affected_days: list[int]
    affected_item_ids: list[str]


class MoveItineraryItemInput(ToolInput):
    plan_id: str
    item_id: str
    target_day: int
    target_time_of_day: str


class MoveItineraryItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    item: ItineraryItem
    affected_days: list[int]
    affected_item_ids: list[str]


class ListItineraryInput(ToolInput):
    plan_id: str
    day: int | None = None


class ListItineraryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    itinerary: list[ItineraryItem]


class UpdateItineraryRoutesInput(ToolInput):
    plan_id: str
    routes: list[ItineraryRoute]


class UpdateItineraryRoutesOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    routes: list[ItineraryRoute]


class ReconcileTravelPlanInput(ToolInput):
    plan_id: str
    affected_days: list[int] | None = None
    affected_item_ids: list[str] | None = None
    travel_mode: str = 'DRIVE'


class ReconcileTravelPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan_id: str
    affected_days: list[int]
    affected_item_ids: list[str]
    routes: list[ReconciledRoute]
    route_errors: list[RouteGap]
    audit: dict[str, Any]
    suggestions: list[dict[str, Any] | str]


class AuditTravelPlanInput(ToolInput):
    plan_id: str


class AuditTravelPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan_id: str | None
    status: str
    trip_days: int
    metrics: dict[str, Any]
    issues: list[dict[str, Any]]
    budget: dict[str, Any] | None
    suggestions: list[dict[str, Any] | str]
    guide_constraints: list[dict[str, Any]]
