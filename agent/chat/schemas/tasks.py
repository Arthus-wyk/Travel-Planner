"""专业子任务的输入、执行上下文与结果契约。"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

@dataclass(frozen=True)
class TaskSpec:
    id: str
    kind: str
    input: dict


@dataclass(frozen=True)
class TaskResult:
    status: Literal["completed", "failed", "blocked", "needs_replan"]
    data: dict = field(default_factory=dict)
    reason: str = ""


@dataclass(frozen=True)
class TaskContext:
    snapshot: dict
    read_tool: Callable[[str, dict], Awaitable[Any]]
    parent_agent_id: str | None = None


TaskHandler = Callable[[TaskSpec, dict[str, TaskResult], TaskContext], Awaitable[TaskResult]]


