"""工具结果的统一校验层，阻止模型把失败误报成成功。"""

from __future__ import annotations

import json
from typing import Any


def verify_tool_result(
    tool_name: str,
    arguments: dict[str, Any],
    result_text: Any,
) -> dict[str, Any]:
    """检查成功标志、关键 payload 和可重试性，生成内部校验结论。"""
    parsed = parse_json(result_text)
    if not isinstance(parsed, dict):
        return {
            "status": "warning",
            "tool": tool_name,
            "message": "Tool result is not JSON; inspect the raw text before claiming completion.",
            "raw_result": result_text[:1000],
        }

    if parsed.get("status") == "success" and isinstance(parsed.get("result"), str):
        nested = parse_json(parsed["result"])
        if isinstance(nested, dict):
            parsed = nested
        elif (
            parsed["result"]
            .lstrip()
            .lower()
            .startswith(("error", "failed", "validation error", "tool error"))
        ):
            # MCP 调用层可能返回“外层成功、内层执行失败”的包装。
            # 不能只看外层 status，否则参数校验错误会被误报为成功。
            return {
                "status": "failed",
                "tool": tool_name,
                "message": parsed["result"],
                "retryable": is_retryable_failure({"message": parsed["result"]}),
                "checks": [
                    "Do not claim completion.",
                    "Read the inner tool error and adjust arguments.",
                ],
            }

    if parsed.get("success") is False or parsed.get("status") == "error":
        return {
            "status": "failed",
            "tool": tool_name,
            "message": parsed.get("message")
            or parsed.get("error")
            or "Tool reported failure.",
            "retryable": is_retryable_failure(parsed),
            "checks": [
                "Do not claim completion.",
                "Adjust arguments or ask for missing information.",
            ],
        }

    checks = ["Tool returned a successful result."]
    warnings: list[str] = []

    if tool_name.startswith("amap_search") and not parsed.get("places"):
        warnings.append(
            "Search returned no places; broaden query or adjust destination."
        )
    if tool_name == "create_travel_plan" and not parsed.get("plan"):
        warnings.append("Plan creation succeeded flag is missing a plan payload.")
    if tool_name in {
        "update_travel_profile",
        "add_itinerary_item",
        "update_itinerary_item",
        "delete_itinerary_item",
        "move_itinerary_item",
        "update_itinerary_routes",
    }:
        checks.append("Confirm the changed fields match the user request.")
    if tool_name == "audit_travel_plan" and parsed.get("issues"):
        warnings.append(
            "Audit found issues; summarize them before presenting the plan as ready."
        )
    if tool_name == "reconcile_travel_plan" and parsed.get("route_errors"):
        warnings.append(
            "Some routes could not be refreshed; do not present all routes as current."
        )

    return {
        "status": "passed" if not warnings else "warning",
        "tool": tool_name,
        "message": "Result verified." if not warnings else "Result needs attention.",
        "warnings": warnings,
        "checks": checks,
    }


def parse_json(value: Any) -> Any:
    """兼容 MCP 返回的字符串 JSON、字典和列表。"""
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def is_retryable_failure(parsed: dict[str, Any]) -> bool:
    """识别临时网络/API 故障，避免对参数错误进行无意义重试。"""
    message = str(parsed.get("message") or parsed.get("error") or "").lower()
    return any(
        token in message
        for token in (
            "timeout",
            "temporarily",
            "connection",
            "rate limit",
            "api error",
        )
    )
