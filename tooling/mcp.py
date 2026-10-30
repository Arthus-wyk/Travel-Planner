"""MCP 只负责传输适配；不再维护第二个注册表或业务执行策略。"""

from typing import Any
from tooling.registry import ToolRegistry
from tooling.results import normalize_result
from tooling.spec import ToolSpec


def build_registry(
    session: Any, definitions: list[Any], metadata: dict[str, dict[str, Any]]
) -> ToolRegistry:
    registry = ToolRegistry()
    for definition in definitions:
        name = definition.name
        info = metadata.get(name, {})

        # 默认参数固定当前名称，避免循环闭包让 handler 全调用最后一个工具。
        async def invoke(arguments, context, tool_name=name):
            result = await session.call_tool(tool_name, arguments)
            structured = getattr(result, "structuredContent", None)
            if structured is None:
                structured = getattr(result, "structured_content", None)
            content = (
                structured
                if structured is not None
                else "\n".join(
                    item.text
                    for item in getattr(result, "content", [])
                    if hasattr(item, "text")
                )
            )
            is_error = bool(
                getattr(result, "isError", False) or getattr(result, "is_error", False)
            )
            return normalize_result(tool_name, content, is_error=is_error)

        description = definition.description or f"Call {name}."
        if info.get("guidance"):
            description += " " + info["guidance"]
        registry.register(
            ToolSpec(
                name=name,
                description=description,
                input_schema=getattr(definition, "inputSchema", None)
                or getattr(definition, "input_schema", None)
                or {"type": "object"},
                handler=invoke,
                domains=frozenset(info.get("domains", ())),
                risk=info.get("risk", "low"),
                required_permission=info.get("required_permission"),
                metadata=info,
            )
        )
    return registry
