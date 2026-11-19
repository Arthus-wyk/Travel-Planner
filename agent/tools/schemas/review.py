"""Input and business output schemas for review tools."""
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.tools.schemas.inputs import ToolInput
from agent.tools.schemas.itinerary import ItineraryItem, ItineraryRoute
from agent.tools.schemas.maps import Place

def _object(required, optional=None):
    fields = {**required, **(optional or {})}
    return {
        "type": "object",
        "properties": {name: {"type": value} for name, value in fields.items()},
        "required": list(required),
        "additionalProperties": False,
    }


_OPERATIONS = {
    "review_place": _object(
        {"item_id": "string", "poi_id": "string", "category": "string"},
        {"duration_minutes": "integer", "visit_tips": "string", "replace": "boolean"},
    ),
    "remove_activity": _object({"item_id": "string"}),
    "move_activity": _object(
        {"item_id": "string", "target_day": "integer"}, {"before_item_id": "string"},
    ),
    "update_activity": _object(
        {"activity_id": "string", "duration_minutes": "integer", "visit_tips": "string",
         "estimated_cost": "number"},
        {"ticket_required": ["boolean", "null"], "time_of_day": "string"},
    ),
    "route_mode": _object({"origin_item_id": "string", "destination_item_id": "string", "travel_mode": "string"}),
    "transport_stops": _object({"item_id": "string", "departure_location": "string", "arrival_location": "string"}),
    "day_departure": _object({"day": "integer", "time": "string"}),
    "update_budget": _object({"budget": "number"}),
    "complete_lodging": _object({"activity_id": "string"}),
    "acknowledge_purchase": _object({"reference_id": "string"}),
}

_VERSION = _object({"plan_version": "string"})
_BATCH = _object({"plan_version": "string", "operations": "array"})
_BATCH["properties"]["operations"].update(
    minItems=1,
    maxItems=1000,
    items={"oneOf": [
        {
            **_object({"action": "string", "payload": "object"}),
            "properties": {"action": {"const": name}, "payload": schema},
        }
        for name, schema in _OPERATIONS.items()
    ]},
)



class GetReviewContextInput(ToolInput):
    day: int | None = Field(default=None, ge=1)


_operations_schema = _BATCH["properties"]["operations"]
_operation_validator = Draft202012Validator(_operations_schema)


class PreviewReviewChangesInput(ToolInput):
    plan_version: str
    operations: list[dict[str, Any]] = Field(default_factory=list, json_schema_extra=_operations_schema)

    @field_validator("operations")
    @classmethod
    def validate_operations(cls, operations):
        error = next(_operation_validator.iter_errors(operations), None)
        if error:
            path = ".".join(map(str, error.absolute_path))
            raise ValueError(f"Invalid review operations at {path or 'operations'}")
        return operations


class ApplyReviewChangesInput(PreviewReviewChangesInput):
    operations: list[dict[str, Any]] = Field(json_schema_extra=_operations_schema)


class OptimizeReviewRoutesInput(ToolInput):
    plan_version: str


class SearchReviewPlacesInput(ToolInput):
    query: str


class SearchReviewHotelsInput(ToolInput):
    query: str = ""


class CompareReviewRouteInput(ToolInput):
    origin_item_id: str
    destination_item_id: str
    travel_mode: str


class GetReviewContextOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="allow")

    plan_version: str
    stage: str
    destination: str
    start_date: str | None
    end_date: str | None
    itinerary: list[ItineraryItem]
    routes: list[ItineraryRoute]
    audit: dict[str, Any]


class PreviewReviewChangesOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    accepted: bool
    plan_version: str
    stage: str | None = None
    destination: str | None = None
    start_date: str | None = None
    end_date: str | None = None
    itinerary: list[ItineraryItem] | None = None
    routes: list[ItineraryRoute] | None = None
    audit: dict[str, Any] | None = None
    draft_plan: dict[str, Any] | None = None
    operations: list[dict[str, Any]] | None = None
    blocking_issues: list[dict[str, Any]] | None = None
    optimization_message: str | None = None


class SearchReviewPlacesOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    places: list[Place]
    query: str | None = None


class SearchReviewHotelsOutput(SearchReviewPlacesOutput):
    pass


class CompareReviewRouteOutput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    origin: str
    destination: str
    travel_mode: str
    distance_meters: int | None = None
    duration: str = Field(pattern=r"^\d+s$")
    fare: float | None = None


class ApplyReviewChangesOutput(GetReviewContextOutput):
    applied: bool
    optimization_message: str | None


class OptimizeReviewRoutesOutput(ApplyReviewChangesOutput):
    pass


REVIEW_TOOL_MODELS = {
    "get_review_context": (GetReviewContextInput, GetReviewContextOutput),
    "preview_review_changes": (PreviewReviewChangesInput, PreviewReviewChangesOutput),
    "search_review_places": (SearchReviewPlacesInput, SearchReviewPlacesOutput),
    "search_review_hotels": (SearchReviewHotelsInput, SearchReviewHotelsOutput),
    "compare_review_route": (CompareReviewRouteInput, CompareReviewRouteOutput),
    "apply_review_changes": (ApplyReviewChangesInput, ApplyReviewChangesOutput),
    "optimize_review_routes": (OptimizeReviewRoutesInput, OptimizeReviewRoutesOutput),
}
