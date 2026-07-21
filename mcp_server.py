"""Canonical MCP transport composition root."""

from mcp.server.fastmcp import FastMCP

from infrastructure.database import check_database_on_startup
from tools.catalog import register_all

mcp = FastMCP("custom-travel-planner")
register_all(mcp)


def main() -> None:
    check_database_on_startup()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
