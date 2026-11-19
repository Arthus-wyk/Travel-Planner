from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from pydantic import BaseModel, ConfigDict

from agent.tools.context import ToolContext

ToolHandler = Callable[[dict[str, Any], ToolContext], Any | Awaitable[Any]]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Transport-independent definition of an agent-callable tool."""

    name: str
    description: str
    input_contract: type[ToolInput] | dict[str, Any]
    handler: ToolHandler
    domains: frozenset[str] = frozenset()
    risk: str = "low"
    required_permission: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    output_model: type[BaseModel] | None = None
    allow_partial: bool = False
    replay_safe: bool = False
    via_mcp: bool = False

    @property
    def input_schema(self) -> dict[str, Any]:
        if isinstance(self.input_contract, dict):
            return self.input_contract
        return self.input_contract.model_json_schema(by_alias=True)
