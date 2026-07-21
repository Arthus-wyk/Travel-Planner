from typing import Any, TypedDict


class MapsResult(TypedDict, total=False):
    success: bool
    message: str
    places: list[dict[str, Any]]
    routes: list[dict[str, Any]]
