"""Pydantic result envelope shared by the MCP server and Agent runtime."""
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

from agent.tools.schemas.errors import ErrorCode, ToolError

T = TypeVar("T")


class ToolResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"oneOf": [
        {"properties": {"status": {"const": "success"}, "data": {"not": {"type": "null"}},
                        "error": {"type": "null"}}, "required": ["data"]},
        {"properties": {"status": {"const": "partial"}, "data": {"not": {"type": "null"}},
                        "error": {"type": "object", "required": ["details"], "properties": {
                            "code": {"const": "PARTIAL_FAILURE"}, "retryable": {"const": False},
                            "details": {"required": ["missing"], "properties": {
                                "missing": {"type": "array", "minItems": 1, "items": {"anyOf": [
                                    {"type": "string", "minLength": 1}, {"type": "object", "minProperties": 1},
                                ]}},
                            }},
                        }}},
         "required": ["data", "error"]},
        {"properties": {"status": {"const": "error"}, "data": {"type": "null"},
                        "error": {"type": "object", "properties": {"code": {"not": {"const": "PARTIAL_FAILURE"}}}}},
         "required": ["error"]},
    ]})

    status: Literal["success", "partial", "error"]
    data: T | None = None
    error: ToolError | None = None
    _tool: str = PrivateAttr(default="")

    @model_validator(mode="after")
    def check_status(self):
        if self.status == "success" and self.data is not None and self.error is None:
            return self
        if self.status == "partial" and self.data is not None and self.error:
            missing = self.error.details.get("missing")
            if (self.error.code == ErrorCode.PARTIAL_FAILURE and not self.error.retryable
                    and isinstance(missing, list) and missing
                    and all(isinstance(item, (str, dict)) and item for item in missing)):
                return self
        if self.status == "error" and self.data is None and self.error and self.error.code != ErrorCode.PARTIAL_FAILURE:
            return self
        raise ValueError("Data and error must match the tool result status")

    @property
    def tool(self) -> str:
        return self._tool

    @property
    def success(self) -> bool:
        return self.status == "success"

    @property
    def message(self) -> str:
        return self.error.message if self.error else ""

    @property
    def error_type(self) -> str:
        return (self.error.details.get("reason") or self.error.code.value) if self.error else ""

    def for_tool(self, name: str):
        self._tool = name
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def failure(name: str, code: ErrorCode, message: str, *, retryable: bool = False,
            retry_after_seconds: float | None = None, details: dict | None = None) -> ToolResult:
    return ToolResult(status="error", error=ToolError(code=code, message=message, retryable=retryable,
                      retry_after_seconds=retry_after_seconds, details=details or {})).for_tool(name)


def legacy_result(name: str, success: bool, data: Any = None, message: str = "", error_type: str = "") -> ToolResult:
    """Adapt existing domain outcomes and injected handlers at the boundary."""
    if success:
        return ToolResult(status="success", data=data if data is not None else {}).for_tool(name)
    code = {
        "validation_error": ErrorCode.INVALID_ARGUMENT, "ValueError": ErrorCode.INVALID_ARGUMENT,
        "PlanConflict": ErrorCode.INVALID_ARGUMENT, "review_rejected": ErrorCode.INVALID_ARGUMENT,
        "invalid_retry": ErrorCode.INVALID_ARGUMENT, "unknown_role": ErrorCode.INVALID_ARGUMENT,
        "unknown_tool": ErrorCode.NOT_FOUND, "permission_denied": ErrorCode.PERMISSION_DENIED,
        "timeout": ErrorCode.UPSTREAM_TIMEOUT,
    }.get(error_type, ErrorCode.INTERNAL_ERROR)
    return failure(name, code, message or "Tool execution failed.", details={
        **({"reason": error_type} if error_type else {}), **(data if isinstance(data, dict) else {}),
    })
