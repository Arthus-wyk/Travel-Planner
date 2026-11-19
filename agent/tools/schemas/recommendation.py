"""Typed input and business output models for recommendation tools."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from agent.tools.schemas.inputs import ToolInput


class AddRecommendationInput(ToolInput):
    plan_id: str
    name: str
    category: str
    location: str
    why: str
    tags: list[str] | None = None
    estimated_cost: float = 0
    priority: str = 'medium'
    notes: str = ''


class AddRecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recommendation: dict[str, Any]


class UpdateRecommendationInput(ToolInput):
    plan_id: str
    recommendation_id: str
    updates: dict[str, Any]


class UpdateRecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    recommendation: dict[str, Any]


class RankRecommendationsInput(ToolInput):
    candidates: list[dict[str, Any]]
    category: str = ''
    max_results: int = 5
    preferences: dict[str, Any] | None = None


class RankRecommendationsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    ranked: list[dict[str, Any]]
    count: int
    scoring_notes: list[str]


class AddBudgetItemInput(ToolInput):
    plan_id: str
    category: str
    amount: float
    description: str


class AddBudgetItemOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    budget_item: dict[str, Any]
