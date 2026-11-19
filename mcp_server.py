"""Canonical MCP transport composition root."""

import asyncio

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from infrastructure.database import check_database_on_startup
from agent.tools.catalog import register_all

mcp = Server("custom-travel-planner")
register_all(mcp)


async def _serve():
    async with stdio_server() as (reader, writer):
        await mcp.run(reader, writer, mcp.create_initialization_options())


def main() -> None:
    check_database_on_startup()
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
