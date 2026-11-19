"""Validate and dispatch each logical tool call with a single retry budget."""
import asyncio
import inspect
import logging
from typing import Any

import httpx
from anyio import BrokenResourceError, ClosedResourceError, EndOfStream
from jsonschema import ValidationError as SchemaError, validators
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from agent.tools.context import ToolContext
from agent.tools.registry import ToolRegistry
from agent.tools.retry import RETRY_DELAYS
from agent.tools.schemas.domain_results import domain_result
from agent.tools.schemas.errors import ErrorCode, ToolRejected
from agent.tools.schemas.inputs import ToolSpec
from agent.tools.schemas.outputs import ToolResult, failure
from service.storage.planning_jobs import PlanConflict

logger = logging.getLogger(__name__)


class ToolExecutor:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def execute(self, name: str, arguments: dict, context: ToolContext, *,
                      allowed: set[str], timeout_seconds: float | None = 60) -> ToolResult:
        if name not in allowed or name not in self.registry.names:
            return failure(name, ErrorCode.NOT_FOUND, "Unknown or unavailable tool.", details={"reason": "unknown_tool"})
        spec = self.registry.get(name)
        if spec.required_permission and spec.required_permission not in context.permissions:
            return failure(name, ErrorCode.PERMISSION_DENIED, "Missing required permission.",
                           details={"reason": "permission_denied", "permission": spec.required_permission})
        try:
            arguments = validate_arguments(spec, arguments)
        except ValidationError as exc:
            issues = [{"path": list(item["loc"]), "type": item["type"]} for item in exc.errors(include_input=False)]
            return failure(name, ErrorCode.INVALID_ARGUMENT, "Arguments do not match the tool schema.",
                           details={"reason": "validation_error", "issues": issues})
        except SchemaError as exc:
            path = "$" + "".join(f".{part}" if isinstance(part, str) else f"[{part}]" for part in exc.absolute_path)
            return failure(name, ErrorCode.INVALID_ARGUMENT, f"{path}: {exc.message}",
                           details={"reason": "validation_error"})
        logger.info("tool.execute", extra={"tool": name, "risk": spec.risk, "actor": context.actor_id})
        if not spec.via_mcp:
            return _enforce_replay_policy(spec, await invoke(spec, arguments, context))
        return await self._call_mcp(spec, arguments, context, timeout_seconds)

    async def _call_mcp(self, spec: ToolSpec, arguments: dict, context: ToolContext,
                        timeout_seconds: float | None) -> ToolResult:
        clock = asyncio.get_running_loop().time
        deadline = clock() + timeout_seconds if timeout_seconds is not None else None
        for attempt in range(len(RETRY_DELAYS) + 1):
            remaining = deadline - clock() if deadline is not None else None
            try:
                result = await asyncio.wait_for(invoke(spec, arguments, context), remaining)
            except asyncio.TimeoutError:
                # A cancelled MCP request may have synchronous I/O still running.
                reason = "DEADLINE_EXCEEDED" if spec.replay_safe else "REPLAY_UNSAFE"
                return failure(spec.name, ErrorCode.UPSTREAM_TIMEOUT, "Tool call timed out.",
                               details={"recovery": reason})
            result = _enforce_replay_policy(spec, result)
            if not result.error or not result.error.retryable:
                return result
            if not spec.replay_safe:
                return _stop_retry(result, "REPLAY_UNSAFE")
            if attempt == len(RETRY_DELAYS):
                return _stop_retry(result, "RETRY_EXHAUSTED")
            delay = max(RETRY_DELAYS[attempt], result.error.retry_after_seconds or 0)
            if deadline is not None and clock() + delay >= deadline:
                return _stop_retry(result, "DEADLINE_EXCEEDED")
            await asyncio.sleep(delay)
        raise AssertionError("Unreachable retry state")


def validate_arguments(spec: ToolSpec, arguments: Any) -> dict:
    if isinstance(spec.input_contract, dict):
        if not isinstance(arguments, dict):
            raise SchemaError("Tool arguments must be an object")
        validator = validators.validator_for(spec.input_schema)(spec.input_schema)
        validator.validate(arguments)
        return arguments
    return spec.input_contract.model_validate(arguments).model_dump(exclude_unset=True, by_alias=True)


def arguments_with_defaults(spec: ToolSpec, arguments: dict) -> dict:
    """Compare the effective inputs of calls without changing what handlers receive."""
    if isinstance(spec.input_contract, dict):
        return arguments
    try:
        return spec.input_contract.model_validate(arguments).model_dump(mode="json", by_alias=True)
    except ValidationError:
        return arguments


async def invoke(spec: ToolSpec, arguments: dict, context: ToolContext) -> ToolResult:
    try:
        output = spec.handler(arguments, context)
        if inspect.isawaitable(output):
            output = await output
        result = domain_result(spec.name, output, typed=spec.output_model is not None)
        if spec.output_model is None:
            return result
        if result.status == "partial" and not spec.allow_partial:
            return failure(spec.name, ErrorCode.INTERNAL_ERROR, "This tool does not support partial results.")
        envelope = ToolResult[spec.output_model].model_validate(result.to_dict())
        # Keep validated JSON objects available to existing evidence consumers.
        return ToolResult.model_validate(envelope.model_dump(mode="json", exclude_unset=True)).for_tool(spec.name)
    except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException):
        return failure(spec.name, ErrorCode.UPSTREAM_TIMEOUT, "The upstream service timed out.", retryable=True)
    except (ConnectionError, httpx.NetworkError, BrokenResourceError, ClosedResourceError, EndOfStream):
        return failure(spec.name, ErrorCode.INTERNAL_ERROR, "The upstream connection failed.", retryable=True)
    except McpError as exc:
        if exc.error.code == 408:
            return failure(spec.name, ErrorCode.UPSTREAM_TIMEOUT, "The MCP request timed out.", retryable=True)
        logger.warning("tool.protocol_failed", extra={"tool": spec.name, "code": exc.error.code})
        return failure(spec.name, ErrorCode.INTERNAL_ERROR, "The MCP request failed.")
    except PlanConflict:
        return failure(spec.name, ErrorCode.INVALID_ARGUMENT, "行程已变化，请刷新后重试。",
                       details={"reason": "PlanConflict"})
    except ToolRejected as exc:
        return ToolResult(status="error", error=exc.error).for_tool(spec.name)
    except Exception as exc:
        logger.warning("tool.failed", extra={"tool": spec.name, "error_class": type(exc).__name__})
        return failure(spec.name, ErrorCode.INTERNAL_ERROR, "The tool could not complete this request.")


def _stop_retry(result: ToolResult, reason: str) -> ToolResult:
    error = result.error
    if error is None:
        return result
    return result.model_copy(update={"error": error.model_copy(update={"retryable": False,
        "details": {**error.details, "recovery": reason}})})


def _enforce_replay_policy(spec: ToolSpec, result: ToolResult) -> ToolResult:
    error = result.error
    if spec.replay_safe or result.status != "error" or error is None or error.details.get("recovery"):
        return result
    if (error.retryable or error.code == ErrorCode.UPSTREAM_TIMEOUT
            or (error.code == ErrorCode.INTERNAL_ERROR and not error.details.get("reason"))):
        return _stop_retry(result, "REPLAY_UNSAFE")
    return result
