from __future__ import annotations

from collections.abc import Iterable

from tooling.registry import ToolRegistry


class ToolRouter:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def names_for(self, domains: Iterable[str], available: set[str] | None = None) -> set[str]:
        names = {spec.name for spec in self.registry.for_domains(domains)}
        return names if available is None else names & available
