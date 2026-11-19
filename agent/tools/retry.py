"""Per-agent tool budgets and correction retry policy."""
from copy import deepcopy

RETRY_DELAYS = (0.5, 1.5)

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


