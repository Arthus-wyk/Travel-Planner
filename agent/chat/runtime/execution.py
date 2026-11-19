"""路线审核单次操作的时限和进度通知。"""

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar


T = TypeVar("T")
REVIEW_STEP_TIMEOUT = 120


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
