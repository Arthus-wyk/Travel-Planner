"""管理 MCP 子进程和会话生命周期，调用方只在上下文内使用注册表。"""

import sys
from contextlib import asynccontextmanager
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from tooling.mcp import build_registry
from tools.catalog import tool_metadata


@asynccontextmanager
async def connect_tools(server_file: Path):
    if not server_file.is_file():
        raise FileNotFoundError(f"MCP server not found: {server_file}")
    parameters = StdioServerParameters(command=sys.executable, args=[str(server_file)])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            listed = await session.list_tools()
            yield build_registry(session, listed.tools, tool_metadata())
