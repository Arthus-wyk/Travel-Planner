"""Input and business output models for bounded specialist delegation."""
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from agent.tools.schemas.inputs import ToolInput


class SearchScope(ToolInput):
    category: Literal["park", "attraction", "food", "hotel", "other"]
    day: int | None = Field(ge=1)
    selection_count: int | None = Field(ge=1)


class ExtraData(ToolInput):
    facts: str = Field(max_length=12000)
    artifact_refs: list[str] = Field(max_length=32)


class DelegateTaskInput(ToolInput):
    model_config = ConfigDict(json_schema_extra={
        "if": {"properties": {"role": {"const": "search_places"}}},
        "then": {"required": ["search_scope"], "properties": {"search_scope": {"type": "object"}}},
        "else": {"not": {"required": ["search_scope"]}},
    })

    task_id: str = Field(min_length=1, max_length=128)
    role: str
    task: str = Field(min_length=1, max_length=4000)
    search_scope: SearchScope | None = None
    extra_data: ExtraData

    @model_validator(mode="after")
    def validate_search_scope(self):
        if self.role == "search_places" and self.search_scope is None:
            raise ValueError("Search tasks require a search scope")
        if self.role != "search_places" and "search_scope" in self.model_fields_set:
            raise ValueError("Only search tasks accept a search scope")
        return self


class DelegateTaskOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: str
    task_version: int
    status: Literal["completed", "failed", "blocked", "needs_replan"]
    artifact_ref: str | None
    places: list[dict[str, Any]]
    summary: str | None = None
    remaining: list[str] | None = None
    route_count: int | None = None
    proposal_kind: str | None = None
    search_scope: SearchScope | None = None


def delegation_input(roles: Iterable[str]) -> type[DelegateTaskInput]:
    allowed_roles = tuple(sorted(roles))
    return create_model("ScopedDelegateTaskInput", __base__=DelegateTaskInput,
                        role=(Literal[allowed_roles], ...))
