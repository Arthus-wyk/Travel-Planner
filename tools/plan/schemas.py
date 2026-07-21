from typing import Any, TypedDict


class ToolResult(TypedDict, total=False):
    success: bool
    message: str
    plan: dict[str, Any]
    plans: list[dict[str, Any]]
