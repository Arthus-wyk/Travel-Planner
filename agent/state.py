"""Agent 数据契约：HTTP、CLI、未来协调 Agent 均使用相同请求和结果。"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

RunStatus = Literal["completed", "failed", "limit_reached"]


@dataclass(frozen=True)
class AgentRequest:
    text: str
    history: list[dict[str, Any]] = field(default_factory=list)
    current_plan: dict[str, Any] | None = None


@dataclass
class AgentResult:
    answer: str = ""
    status: RunStatus = "failed"
    understanding: dict[str, Any] = field(default_factory=dict)
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None


class AgentState(TypedDict, total=False):
    # messages 是可压缩的模型上下文；new_messages 独立保存本轮公开产物。
    messages: list[dict[str, Any]]
    text: str
    current_plan: dict[str, Any] | None
    understanding: dict[str, Any]
    memory_policy: dict[str, Any]
    memory_context: str
    active_tools: list[str]
    round_index: int
    guard_retries: int
    calls: list[dict[str, Any]]
    call_index: int
    tool_result: dict[str, Any]
    tool_retry_of: str | None
    tool_events: list[dict[str, Any]]
    failed_calls: list[dict[str, Any]]
    repair_activated: bool
    answer: str
    status: RunStatus
    done: bool
    new_messages: list[dict[str, Any]]
    plan: dict[str, Any] | None


class AgentRunError(RuntimeError):
    """意外异常携带已完成产物，应用层可保存上下文而不重跑写操作。"""

    def __init__(self, message: str, partial_result: AgentResult):
        super().__init__(message)
        self.partial_result = partial_result


class AgentCancelledError(asyncio.CancelledError):
    def __init__(self, partial_result: AgentResult):
        super().__init__("Agent run cancelled")
        self.partial_result = partial_result
