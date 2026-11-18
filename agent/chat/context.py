"""本轮运行资源，不进入图状态，也不从用户文字接受权限和身份。"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from tooling.context import ToolContext
from tooling.registry import ToolRegistry
from agent.chat.state import AgentSession
from agent.chat.review_execution import ReviewExecution

DEFAULT_MAX_ROUNDS = 32


@dataclass
class AgentContext:
    client: Any
    model: str
    tools: ToolRegistry
    tool_context: ToolContext = field(default_factory=ToolContext)
    # 专业 Agent 可使用独立模型、工具子集和预算，无需复制执行循环。
    allowed_tools: frozenset[str] | None = None
    max_rounds: int = DEFAULT_MAX_ROUNDS
    memory_enabled: bool = True
    fallback_model: str | None = None
    streaming: bool = False
    on_text: Callable[[str], Awaitable[None]] | None = None
    auth_recovery: Callable[[], Awaitable[None]] | None = None
    transcript: Any = None
    telemetry: Any = None
    system_prompt: str | None = None
    role: str = "chat"
    response_format: dict | None = None
    session: AgentSession | None = None
    task_id: str | None = None
    task_version: int = 1
    timeout_seconds: float = 60
    concurrency: int = 3
    execution: ReviewExecution | None = None
    enable_rewrite: bool = True
    agent_id: str | None = None
    parent_agent_id: str | None = None
