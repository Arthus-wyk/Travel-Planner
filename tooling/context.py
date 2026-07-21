from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ToolContext:
    """Request-scoped information available to policies and tools."""

    actor_id: str | None = None
    permissions: frozenset[str] = frozenset()
    confirmed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
