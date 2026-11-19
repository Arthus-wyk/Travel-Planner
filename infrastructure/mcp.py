"""管理 MCP 子进程和会话生命周期，调用方只在上下文内使用注册表。"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import StdioServerParameters
from mcp.client.stdio import stdio_client
from agent.tools.mcp import ToolClientSession, bind_registry, build_registry
from agent.tools.catalog import public_registry, tool_metadata


@asynccontextmanager
async def connect_tools(server_file: Path):
    if not server_file.is_file():
        raise FileNotFoundError(f"MCP server not found: {server_file}")
    parameters = StdioServerParameters(command=sys.executable, args=[str(server_file)])
    async with stdio_client(parameters) as (reader, writer):
        async with ToolClientSession(reader, writer) as session:
            await session.initialize()
            listed = await session.list_tools()
            if server_file.resolve() == Path(__file__).resolve().parents[1] / "mcp_server.py":
                registry = public_registry()
                if {tool.name for tool in listed.tools} != registry.names:
                    raise ValueError("Public MCP tool list differs from the registered catalogue")
                yield bind_registry(session, registry)
            else:
                yield build_registry(session, listed.tools, tool_metadata())
