from __future__ import annotations

from collections.abc import Iterable

from tooling.errors import ToolNotFoundError
from tooling.spec import ToolSpec


class ToolRegistry:
    """Single source of truth for tool registration and lookup."""

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._specs:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ToolNotFoundError(f"Unknown tool: {name}") from exc

    @property
    def names(self) -> set[str]:
        return set(self._specs)

    def all(self) -> tuple[ToolSpec, ...]:
        return tuple(self._specs.values())

    def for_domains(self, domains: Iterable[str]) -> tuple[ToolSpec, ...]:
        requested = set(domains)
        return tuple(spec for spec in self._specs.values() if spec.domains & requested)

    def ollama_schemas(self) -> list[dict]:
        return [{"type": "function", "function": {"name": spec.name, "description": spec.description, "parameters": spec.input_schema}} for spec in self._specs.values()]
