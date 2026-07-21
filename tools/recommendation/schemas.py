from typing import Any, TypedDict


class RecommendationResult(TypedDict, total=False):
    success: bool
    message: str
    recommendations: list[dict[str, Any]]
