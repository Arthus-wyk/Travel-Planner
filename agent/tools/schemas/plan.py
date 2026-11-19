"""Typed input and business output models for plan tools."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from agent.tools.schemas.inputs import ToolInput
from service.validation import TravelProfileUpdates


class StoredPlan(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)

    plan_id: str


class CreateTravelPlanInput(ToolInput):
    plan_id: str
    destination: str
    start_date: str
    end_date: str
    travelers: int
    budget: float
    currency: str = 'CNY'
    departure_city: str | None = None
    travel_style: str = 'balanced'
    interests: list[str] | None = None
    constraints: list[str] | None = None
    pace: str = 'moderate'
    notes: str = ''
    title: str | None = None


class CreateTravelPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan: StoredPlan


class UpdateTravelProfileInput(ToolInput):
    plan_id: str
    updates: TravelProfileUpdates


class UpdateTravelProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan: StoredPlan


class GetTravelPlanInput(ToolInput):
    plan_id: str


class GetTravelPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plan: StoredPlan


class SummarizeTravelPlanInput(ToolInput):
    plan_id: str


class SummarizeTravelPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary: dict[str, Any]


class ListTravelPlansInput(ToolInput):
    pass


class ListTravelPlansOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    plans: list[dict[str, Any]]


class DeleteTravelPlanInput(ToolInput):
    plan_id: str


class DeleteTravelPlanOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    message: str
