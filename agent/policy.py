from __future__ import annotations

from tooling.context import ToolContext
from tooling.errors import ToolPolicyError
from tooling.spec import ToolSpec


class ToolPolicy:
    """Central confirmation, permission, and risk gate."""

    async def authorize(self, spec: ToolSpec, context: ToolContext) -> None:
        if spec.required_permission and spec.required_permission not in context.permissions:
            raise ToolPolicyError(f"Missing permission: {spec.required_permission}")
        if spec.risk in {"high", "destructive"} and not context.confirmed:
            raise ToolPolicyError(f"Tool requires explicit confirmation: {spec.name}")
