from __future__ import annotations
from uuid import uuid4
from datetime import datetime
import re
from typing import Annotated, Any
from typing_extensions import TypedDict
from pydantic import ConfigDict, Field, with_config


@with_config(ConfigDict(extra="forbid", strict=True))
class TravelProfileUpdates(TypedDict):
    destination: str | None
    start_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    end_date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}$")] | None
    departure_city: str | None
    travelers: Annotated[int, Field(ge=1)] | None
    budget: Annotated[float, Field(ge=0)] | None
    currency: str | None
    travel_style: str | None
    interests: Annotated[list[str], Field(description="Preferred activities and themes.")] | None
    constraints: Annotated[list[str], Field(description="Dietary, mobility, accommodation, or other trip restrictions.")] | None
    pace: Annotated[str, Field(description="Requested trip tempo, for example relaxed.")] | None
    notes: Annotated[str, Field(description="Other trip-specific requests that do not fit another field.")] | None


VALID_PROFILE_FIELDS = set(TravelProfileUpdates.__annotations__)

VALID_ITINERARY_FIELDS = {
    "day",
    "time_of_day",
    "title",
    "location",
    "category",
    "duration_minutes",
    "estimated_cost",
    "booking_required",
    "notes",
}

VALID_RECOMMENDATION_FIELDS = {
    "name",
    "category",
    "location",
    "why",
    "tags",
    "estimated_cost",
    "priority",
    "notes",
}

VALID_PRIORITIES = {"low", "medium", "high"}

TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def validate_time_of_day(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value.strip()):
        return False, "time_of_day must use 24-hour HH:MM format, for example 09:30."
    return True, None


def time_minutes(value: Any) -> int | None:
    if not isinstance(value, str) or not TIME_PATTERN.fullmatch(value.strip()):
        return None
    hour, minute = value.strip().split(":")
    return int(hour) * 60 + int(minute)


def validate_date_range(start_date: str, end_date: str) -> tuple[bool, str | None]:
    start_ok, start_error = _parse_date(start_date, "start_date")
    if not start_ok:
        return False, start_error

    end_ok, end_error = _parse_date(end_date, "end_date")
    if not end_ok:
        return False, end_error

    if end_date < start_date:
        return False, "end_date must be the same as or later than start_date."

    return True, None


def validate_list(value: Any, field_name: str) -> tuple[bool, str | None]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return False, f"{field_name} must be a list of strings."
    return True, None


def validate_non_negative_number(
    value: Any,
    field_name: str,
) -> tuple[bool, str | None]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, f"{field_name} must be a number."
    if value < 0:
        return False, f"{field_name} must not be negative."
    return True, None


def next_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _parse_date(value: str, field_name: str) -> tuple[bool, str | None]:
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False, f"{field_name} must use YYYY-MM-DD format."
    return True, None
