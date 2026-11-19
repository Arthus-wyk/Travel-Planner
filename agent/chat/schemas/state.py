"""Agent 数据契约：HTTP、CLI、未来协调 Agent 均使用相同请求和结果。"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict
from agent.tools.retry import ToolBudget

RunStatus = Literal["completed", "partial", "failed", "limit_reached"]


@dataclass(frozen=True)
class AgentRequest:
    text: str
    history: list[dict[str, Any]] = field(default_factory=list)
    current_plan: dict[str, Any] | None = None
    extra_data: dict[str, Any] = field(default_factory=dict)
    rewrite: dict[str, Any] | None = None
    selection: dict[str, Any] | None = None


@dataclass
class AgentSession:
    """One agent's private messages, verified evidence and lifetime budget."""
    messages: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    failed_calls: list[dict[str, Any]] = field(default_factory=list)
    tool_limit: int = 8
    budget: ToolBudget = field(init=False)
    model_steps: int = 0
    operation_sequence: int = 0
    task_signature: str | None = None
    agent_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self):
        self.budget = ToolBudget(self.tool_limit)


@dataclass
class AgentResult:
    answer: str = ""
    status: RunStatus = "failed"
    rewrite: dict[str, Any] = field(default_factory=dict)
    output: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = field(default_factory=list)
    new_messages: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    plan: dict[str, Any] | None = None
    memory: dict[str, Any] | None = None
    output_error: str | None = None


class AgentState(TypedDict, total=False):
    # messages 是可压缩的模型上下文；new_messages 独立保存本轮公开产物。
    messages: list[dict[str, Any]]
    text: str
    current_plan: dict[str, Any] | None
    rewrite: dict[str, Any]
    output: dict[str, Any] | None
    output_error: str
    evidence: list[dict[str, Any]]
    active_tools: list[str]
    round_index: int
    guard_retries: int
    calls: list[dict[str, Any]]
    tool_events: list[dict[str, Any]]
    failed_calls: list[dict[str, Any]]
    answer: str
    status: RunStatus
    done: bool
    new_messages: list[dict[str, Any]]
    plan: dict[str, Any] | None


class AgentRunError(RuntimeError):
    """意外异常携带已完成产物，应用层可保存上下文而不重跑写操作。"""

    # ============初始化 ============
    def __init__(self, message: str, partial_result: AgentResult):
        super().__init__(message)
        self.partial_result = partial_result


class AgentCancelledError(asyncio.CancelledError):
    # ============初始化 ============
    def __init__(self, partial_result: AgentResult):
        super().__init__("Agent run cancelled")
        self.partial_result = partial_result
