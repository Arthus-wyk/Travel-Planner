from typing import Any, TypedDict


class ItineraryResult(TypedDict, total=False):
    success: bool
    message: str
    itinerary: list[dict[str, Any]]
    affected_days: list[int]
