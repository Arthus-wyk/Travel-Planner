"""路线审核单次操作的时限和进度通知。"""

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

T = TypeVar("T")
REVIEW_STEP_TIMEOUT = 60
REVIEW_TOOL_LIMIT = 8
REVIEW_TOOL_RETRIES = 3
REVIEW_MODEL_STEPS = REVIEW_TOOL_LIMIT * (REVIEW_TOOL_RETRIES + 1)


class ToolBudget:
    """每次 Agent 执行的正常调用额度及独立失败链，重试由模型发起。"""

    def __init__(self, limit: int = REVIEW_TOOL_LIMIT):
        self.remaining = limit
        self.failures: dict[str, int] = {}
        self.arguments: dict[str, dict] = {}

    def can_call(self, name: str) -> bool:
        return self.remaining > 0 or 0 < self.failures.get(name, 0) <= REVIEW_TOOL_RETRIES

    def begin(self, name: str, arguments: dict) -> tuple[bool, str]:
        failures = self.failures.get(name, 0)
        if failures and arguments == self.arguments.get(name):
            return False, "请根据上次错误调整参数，或换工具、返回已有结果。"
        is_retry = 0 < failures <= REVIEW_TOOL_RETRIES
        if not is_retry:
            if self.remaining <= 0:
                return False, "正常工具预算已耗尽，且没有可用纠错重试。"
            self.remaining -= 1
            self.failures.pop(name, None)
        self.arguments[name] = deepcopy(arguments)
        return True, ""

    def finish(self, name: str, success: bool) -> None:
        if success:
            self.failures.pop(name, None)
        else:
            self.failures[name] = self.failures.get(name, 0) + 1

    def feedback(self) -> dict:
        return {
            "remaining_tool_calls": self.remaining,
            "retry_remaining": {name: max(0, REVIEW_TOOL_RETRIES + 1 - failures)
                                for name, failures in self.failures.items()},
            "retry_exhausted": [name for name, failures in self.failures.items()
                                if failures > REVIEW_TOOL_RETRIES],
        }


class ReviewStepTimeout(ValueError):
    """一次模型或工具操作超过自己的时限。"""


@dataclass(frozen=True)
class ReviewExecution:
    """给每次调用独立计时，并把当前执行状态交给调用方。"""

    timeout_seconds: float = REVIEW_STEP_TIMEOUT
    on_progress: Callable[[str], None] | None = None

    # ============执行一次操作 ============
    async def run(self, operation: Awaitable[T], label: str) -> T:
        """在开始时发布进度，超时时指出具体步骤。"""
        if self.on_progress is not None:
            self.on_progress(f"正在{label}…")
        try:
            return await asyncio.wait_for(operation, self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise ReviewStepTimeout(f"{label}超时，请重试。") from exc
