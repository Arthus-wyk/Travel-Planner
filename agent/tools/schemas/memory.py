"""Typed input and business output models for memory tools."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from agent.tools.schemas.inputs import ToolInput


class ReadTravelPreferenceProfileInput(ToolInput):
    pass


class ReadTravelPreferenceProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    profile_markdown: str
    profile: dict[str, list[str]]


class UpdateTravelPreferenceProfileInput(ToolInput):
    updates: dict[str, Any]
    note: str = ''


class UpdateTravelPreferenceProfileOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    path: str
    changed: dict[str, list[str]]
    profile_markdown: str
    profile: dict[str, list[str]]


class VerifyLastToolResultInput(ToolInput):
    tool_name: str
    arguments: dict[str, Any]
    result_text: str


class VerifyLastToolResultOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    verification: dict[str, Any]
