"""工具结果的唯一规范：外部边界解析一次，核心始终使用结构化数据。"""

from dataclasses import asdict, dataclass
import json
from typing import Any


@dataclass(frozen=True)
class ToolResult:
    tool: str
    success: bool
    data: Any = None
    message: str = ""
    error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_result(tool: str, value: Any, *, is_error: bool = False) -> ToolResult:
    """兼容文本和旧 JSON 信封；MCP isError 优先于 payload 的成功标志。"""
    if isinstance(value, ToolResult):
        return value
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            pass
    if (
        isinstance(value, dict)
        and value.get("status") == "success"
        and isinstance(value.get("result"), str)
    ):
        return normalize_result(tool, value["result"], is_error=is_error)
    failed = is_error
    message = ""
    if isinstance(value, dict):
        failed |= value.get("success") is False or value.get("status") in {
            "error",
            "failed",
        }
        message = str(value.get("message") or value.get("error") or "")
    elif isinstance(value, str):
        failed |= (
            value.lstrip()
            .lower()
            .startswith(("error", "failed", "validation error", "tool error"))
        )
        message = value if failed else ""
    error_type = value.get("error_type") if isinstance(value, dict) else None
    return ToolResult(tool, not failed, value, message, (error_type or "tool_error") if failed else "")
