"""Structured errors shared by all tool protocols."""
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictBool


class ErrorCode(str, Enum):
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    UPSTREAM_TIMEOUT = "UPSTREAM_TIMEOUT"
    RATE_LIMITED = "RATE_LIMITED"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: ErrorCode
    message: str
    retryable: StrictBool = False
    retry_after_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    details: dict[str, Any] = Field(default_factory=dict)


class ToolNotFoundError(LookupError):
    pass


class ToolRejected(Exception):
    """A known tool rejection whose message is safe to return to the caller."""

    def __init__(self, code: ErrorCode, message: str):
        super().__init__(message)
        self.error = ToolError(code=code, message=message)
