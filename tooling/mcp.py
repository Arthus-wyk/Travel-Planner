"""MCP client adapters for the transport-independent agent tool interface."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from mcp import ClientSession

from tooling.context import ToolContext


class MCPTool:
    def __init__(self, mcp: ClientSession, definition: Any) -> None:
        self._mcp = mcp
        self.name = definition.name
        self.description = definition.description or f"Call the {self.name} tool."
        self.schema = (
            getattr(definition, "inputSchema", None)
            or getattr(definition, "input_schema", None)
            or {"type": "object", "properties": {}}
        )

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> str:
        try:
            result = await self._mcp.call_tool(self.name, arguments)
            structured = getattr(result, "structuredContent", None)
            if structured is None:
                structured = getattr(result, "structured_content", None)
            if structured is not None:
                output = json.dumps(structured, ensure_ascii=False)
            else:
                texts = [item.text for item in getattr(result, "content", []) if hasattr(item, "text")]
                output = "\n".join(texts) if texts else repr(result)
            return json.dumps({"status": "success", "tool": self.name, "result": output}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps(
                {"status": "error", "tool": self.name, "error_type": type(exc).__name__, "message": str(exc)},
                ensure_ascii=False,
            )


class MCPToolRegistry:
    """Runtime collection of tools advertised by an MCP ClientSession."""

    def __init__(self, tools: list[MCPTool] | None = None) -> None:
        self._tools = {tool.name: tool for tool in (tools or [])}

    @classmethod
    def from_mcp(cls, mcp: ClientSession, definitions: list[Any]) -> "MCPToolRegistry":
        return cls([MCPTool(mcp, definition) for definition in definitions])

    @classmethod
    def from_ollama_schemas(cls, mcp: ClientSession, schemas: list[dict[str, Any]]) -> "MCPToolRegistry":
        definitions = []
        for item in schemas:
            function = item.get("function", item)
            definitions.append(SimpleNamespace(
                name=function["name"],
                description=function.get("description", ""),
                inputSchema=function.get("parameters", {"type": "object", "properties": {}}),
            ))
        return cls.from_mcp(mcp, definitions)

    @property
    def names(self) -> set[str]:
        return set(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        return [{"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.schema}} for tool in self._tools.values()]

    async def execute(self, name: str, arguments: dict[str, Any], *, context: ToolContext | None = None) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return json.dumps({"status": "error", "tool": name, "error_type": "unknown_tool", "message": f"Unknown tool: {name}"}, ensure_ascii=False)
        return await tool.execute(arguments, context or ToolContext())


# Compatibility alias for third-party imports of the previous API.
ToolRegistry = MCPToolRegistry

__all__ = ["MCPTool", "MCPToolRegistry", "ToolContext", "ToolRegistry"]
