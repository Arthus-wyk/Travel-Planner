"""Expose registered tools over MCP and bind clients to those same contracts."""
import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any

from anyio import BrokenResourceError, ClosedResourceError, create_task_group, move_on_after
from mcp import ClientSession, types
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_client_server_memory_streams

from agent.tools.context import ToolContext
from agent.tools.dispatcher import ToolExecutor
from agent.tools.registry import ToolRegistry
from agent.tools.schemas.domain_results import domain_result
from agent.tools.schemas.errors import ErrorCode
from agent.tools.schemas.inputs import ToolSpec
from agent.tools.schemas.outputs import ToolResult, failure


class ToolClientSession(ClientSession):
    async def send_request(self, request, result_type, *args, **kwargs):
        # shortcut: the locked SDK allocates this ID synchronously before its first await; use a public cancellation hook when available.
        request_id = self._request_id
        try:
            return await super().send_request(request, result_type, *args, **kwargs)
        except asyncio.CancelledError:
            if isinstance(request.root, types.CallToolRequest):
                with move_on_after(0.25, shield=True):
                    try:
                        await self.send_notification(types.ClientNotification(types.CancelledNotification(
                            params=types.CancelledNotificationParams(requestId=request_id),
                        )))
                    except (BrokenResourceError, ClosedResourceError):
                        pass
            raise


def expose_tools(server: Server, registry: ToolRegistry, context: ToolContext) -> None:
    executor = ToolExecutor(registry)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=spec.name, description=spec.description, inputSchema=spec.input_schema,
                           outputSchema=_output_schema(spec))
                for spec in registry.all()]

    @server.call_tool(validate_input=False)
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        result = await executor.execute(name, arguments, context, allowed=registry.names)
        content = result.to_dict()
        return types.CallToolResult(structuredContent=content, isError=result.status == "error",
                                    content=[types.TextContent(type="text", text=json.dumps(content, ensure_ascii=False))])


def _output_schema(spec: ToolSpec) -> dict | None:
    if spec.output_model is None:
        return None
    schema = ToolResult[spec.output_model].model_json_schema()
    if not spec.allow_partial:
        schema["oneOf"] = [branch for branch in schema["oneOf"]
                           if branch["properties"]["status"]["const"] != "partial"]
        schema["properties"]["status"]["enum"] = ["success", "error"]
    return schema


def bind_registry(session: Any, registry: ToolRegistry) -> ToolRegistry:
    """The client uses the same Pydantic models and trusted policy as the server."""
    return ToolRegistry(replace(spec, handler=_mcp_handler(session, spec.name, typed=spec.output_model is not None),
                                via_mcp=True) for spec in registry.all())


@asynccontextmanager
async def private_tool_session(registry: ToolRegistry, context: ToolContext | None = None):
    """Bind review tools and their authorization to one request-local MCP server."""
    server = Server("travel-review")
    expose_tools(server, registry, context or ToolContext())
    interrupted = None
    async with create_client_server_memory_streams() as streams, create_task_group() as tasks:
        (reader, writer), (server_reader, server_writer) = streams
        tasks.start_soon(server.run, server_reader, server_writer, server.create_initialization_options())
        try:
            async with ToolClientSession(reader, writer) as session:
                await session.initialize()
                await session.list_tools()
                try:
                    yield bind_registry(session, registry)
                except (Exception, asyncio.CancelledError) as exc:
                    # Preserve the caller's error across the SDK task-group cleanup.
                    interrupted = exc
        finally:
            tasks.cancel_scope.cancel()
    if interrupted is not None:
        raise interrupted


def _mcp_handler(session, name: str, *, typed: bool):
    async def invoke(arguments, context):
        response = await session.call_tool(name, arguments)
        content = getattr(response, "structuredContent", None)
        is_error = bool(getattr(response, "isError", False))
        if not typed and content is None:
            content = "\n".join(item.text for item in response.content if isinstance(item, types.TextContent))
            if is_error:
                return failure(name, ErrorCode.INTERNAL_ERROR, content or "The MCP server rejected the tool call.")
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return domain_result(name, content, typed=False)
        if not typed and (not isinstance(content, dict) or content.get("status") not in {"success", "partial", "error"}):
            if is_error:
                return failure(name, ErrorCode.INTERNAL_ERROR, "The MCP server rejected the tool call.")
            return domain_result(name, content, typed=False)
        result = ToolResult.model_validate(content)
        if is_error != (result.status == "error"):
            return failure(name, ErrorCode.INTERNAL_ERROR, "The MCP tool response had inconsistent status.")
        return result
    return invoke


def build_registry(session: Any, definitions: list[Any], metadata: dict[str, dict[str, Any]]) -> ToolRegistry:
    """Adapt discovered schemas for callers with an externally provided MCP server."""
    registry = ToolRegistry()
    for definition in definitions:
        name = definition.name
        info = metadata.get(name, {})
        description = definition.description or f"Call {name}."
        if info.get("guidance"):
            description += " " + info["guidance"]
        registry.register(ToolSpec(
            name, description, definition.inputSchema or {"type": "object"}, _mcp_handler(session, name, typed=False),
            domains=frozenset(info.get("domains", ())), risk=info.get("risk", "low"),
            required_permission=info.get("required_permission"), metadata=info, via_mcp=True,
        ))
    return registry
