"""Discovery and registration for business-domain MCP modules."""

from importlib import import_module
from typing import Any

MODULE_NAMES = ("plan", "itinerary", "maps", "recommendation", "memory")


def modules() -> list[Any]:
    return [import_module(f"tools.{name}.mcp_tools") for name in MODULE_NAMES]


def tool_domains() -> dict[str, set[str]]:
    return {name: set(domains) for module in modules() for name, domains in module.TOOL_DOMAINS.items()}


def domain_tools() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, domains in tool_domains().items():
        for domain in domains:
            result.setdefault(domain, []).append(name)
    return result


def register_all(mcp: Any) -> None:
    for module in modules():
        module.register(mcp)
