"""解析原生工具调用及失败操作关联元数据。"""

import json
from copy import deepcopy
from typing import Any


# ============统一工具调用格式 ============
def normalize_calls(calls: list[Any]) -> list[dict[str, Any]]:
    result = []
    for call in calls:
        function = (
            call.get("function", call)
            if isinstance(call, dict)
            else getattr(call, "function", None)
        )
        name = (
            function.get("name")
            if isinstance(function, dict)
            else getattr(function, "name", None)
        )
        args = (
            function.get("arguments", {})
            if isinstance(function, dict)
            else getattr(function, "arguments", {})
        )
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if isinstance(name, str) and isinstance(args, dict):
            arguments = dict(args)
            retry_of = arguments.pop("_retry_of", None)
            call = {"name": name, "arguments": arguments}
            if retry_of is not None:
                call["retry_of"] = retry_of
            result.append(call)
    return result


# ============生成模型工具定义 ============
def model_tools(registry, available: list[str]) -> list[dict[str, Any]]:
    """重试关系仅供 Agent 使用，不改变实际 MCP 工具参数或 handler 签名。"""
    schemas = [
        deepcopy(s)
        for s in registry.ollama_schemas()
        if s["function"]["name"] in available
    ]
    for schema in schemas:
        parameters = schema["function"]["parameters"]
        parameters.setdefault("properties", {})["_retry_of"] = {
            "type": ["string", "null"] if schema["function"]["name"] == "update_travel_profile" else "string",
            "description": "修正失败操作时，填写该失败结果的 operation_id。新操作不填。此元数据不会发给实际工具。",
        }
        if schema["function"]["name"] == "update_travel_profile":
            parameters.setdefault("required", []).append("_retry_of")
            parameters["additionalProperties"] = False
    return schemas
