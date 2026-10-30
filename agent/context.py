"""本轮运行资源，不进入图状态，也不从用户文字接受权限和身份。"""

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from tooling.context import ToolContext
from tooling.registry import ToolRegistry

DEFAULT_MAX_ROUNDS = 8


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
