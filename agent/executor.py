from __future__ import annotations

import inspect
import logging
from typing import Any

from agent.policy import ToolPolicy
from tooling.context import ToolContext
from tooling.errors import ToolError, ToolValidationError
from tooling.registry import ToolRegistry

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Validate, authorize, invoke, and normalize tool failures."""

    def __init__(self, registry: ToolRegistry, policy: ToolPolicy | None = None) -> None:
        self.registry = registry
        self.policy = policy or ToolPolicy()

    async def execute(self, name: str, arguments: dict[str, Any], context: ToolContext | None = None) -> Any:
        if not isinstance(arguments, dict):
            raise ToolValidationError("Tool arguments must be an object.")
        spec = self.registry.get(name)
        request_context = context or ToolContext()
        self._validate_required(spec.input_schema, arguments)
        await self.policy.authorize(spec, request_context)
        logger.info("tool.execute", extra={"tool": name, "risk": spec.risk})
        try:
            result = spec.handler(arguments, request_context)
            return await result if inspect.isawaitable(result) else result
        except ToolError:
            raise
        except Exception:
            logger.exception("tool.failed", extra={"tool": name})
            raise

    @staticmethod
    def _validate_required(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
        missing = [name for name in schema.get("required", []) if name not in arguments]
        if missing:
            raise ToolValidationError(f"Missing required arguments: {', '.join(missing)}")
