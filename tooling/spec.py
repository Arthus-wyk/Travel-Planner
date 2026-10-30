from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from tooling.context import ToolContext

ToolHandler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Transport-independent definition of an agent-callable tool."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    domains: frozenset[str] = frozenset()
    risk: str = "low"
    required_permission: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
