"""所有工具的唯一执行入口，未来专业 Agent 也可作为异步 handler 注册。"""

import asyncio
import inspect
import logging
from jsonschema import ValidationError, validators
from tooling.context import ToolContext
from tooling.registry import ToolRegistry
from tooling.results import ToolResult, normalize_result

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(
        self, name: str, arguments: dict, context: ToolContext, *, allowed: set[str]
    ) -> ToolResult:
        if not isinstance(arguments, dict):
            return ToolResult(
                name,
                False,
                message="Tool arguments must be an object",
                error_type="validation_error",
            )
        if name not in allowed or name not in self.registry.names:
            return ToolResult(
                name,
                False,
                message=f"Unknown or unavailable tool: {name}",
                error_type="unknown_tool",
            )
        spec = self.registry.get(name)
        if (
            spec.required_permission
            and spec.required_permission not in context.permissions
        ):
            return ToolResult(
                name,
                False,
                message=f"Missing permission: {spec.required_permission}",
                error_type="permission_denied",
            )
        # 保留类型、范围、必填约束；MCP 服务端仍做二次校验。
        validator = validators.validator_for(spec.input_schema)(spec.input_schema)
        try:
            validator.validate(arguments)
        except ValidationError as exc:
            path = "$" + "".join(f".{part}" if isinstance(part, str) else f"[{part}]" for part in exc.absolute_path)
            allowed = exc.schema.get("properties", {}) if isinstance(exc.schema, dict) else {}
            hint = f" Allowed fields: {', '.join(allowed)}." if allowed else ""
            return ToolResult(
                name, False, message=f"{path}: {exc.message}.{hint}", error_type="validation_error"
            )
        logger.info(
            "tool.execute",
            extra={"tool": name, "risk": spec.risk, "actor": context.actor_id},
        )
        try:
            output = spec.handler(arguments, context)
            if inspect.isawaitable(output):
                output = await output
            return normalize_result(name, output)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "tool.failed", extra={"tool": name, "error": type(exc).__name__}
            )
            return ToolResult(
                name, False, message=str(exc), error_type=type(exc).__name__
            )
