"""Translate domain outcomes into the tool protocol at the execution boundary."""
from typing import Any

from agent.tools.schemas.errors import ErrorCode, ToolError
from agent.tools.schemas.outputs import ToolResult, failure, legacy_result
from service.integrations.travel_amap import DAILY_CODES, QPS_CODES


def domain_result(name: str, value: Any, *, typed: bool) -> ToolResult:
    if isinstance(value, ToolResult):
        return value.for_tool(name)
    if not isinstance(value, dict):
        if not typed:
            return legacy_result(name, True, value)
        return failure(name, ErrorCode.INTERNAL_ERROR, "Tool returned an invalid result.")
    if value.get("status") in {"success", "partial", "error"} and "data" in value and "error" in value:
        return ToolResult.model_validate(value).for_tool(name)
    if value.get("success") is not False:
        return legacy_result(name, True, {key: item for key, item in value.items() if key != "success"} if typed else value)
    if name == "reconcile_travel_plan" and value.get("route_errors") and value.get("audit") is not None:
        misses = [{key: issue[key] for key in ("origin_item_id", "destination_item_id", "http_status", "infocode")
                   if key in issue} for issue in value["route_errors"]]
        return ToolResult(status="partial", data={**{k: v for k, v in value.items() if k != "success"},
                                                   "route_errors": misses},
                          error=ToolError(code=ErrorCode.PARTIAL_FAILURE, message="Some routes could not be refreshed.",
                                          details={"missing": misses})).for_tool(name)
    message = str(value.get("message") or value.get("error") or "Tool execution failed.")
    status, upstream_code = value.get("http_status"), value.get("infocode")
    reason = value.get("error_type")
    if status == 429 or upstream_code in QPS_CODES | DAILY_CODES | {"10004"}:
        return failure(name, ErrorCode.RATE_LIMITED, "Map service rate limit reached.",
                       retryable=upstream_code not in DAILY_CODES and upstream_code != "10004",
                       retry_after_seconds=value.get("retry_after"))
    if reason == "upstream_timeout" or status == 504:
        return failure(name, ErrorCode.UPSTREAM_TIMEOUT, "The upstream service timed out.", retryable=True)
    if reason == "upstream_unavailable" or status in {500, 502, 503} or upstream_code == "10016":
        return failure(name, ErrorCode.INTERNAL_ERROR, "The upstream service is temporarily unavailable.", retryable=True)
    if status in {401, 403}:
        return failure(name, ErrorCode.PERMISSION_DENIED, "The upstream service denied access.")
    if reason == "database_error" or message.startswith(("Database error.", "Amap API error:")) or upstream_code or status:
        return failure(name, ErrorCode.INTERNAL_ERROR, "The tool's upstream service failed.")
    if reason == "review_rejected":
        return legacy_result(name, False, {"preview": value["preview"]}, message, reason)
    if reason in {"validation_error", "invalid_argument"}:
        return failure(name, ErrorCode.INVALID_ARGUMENT, message, details={"reason": "validation_error"})
    if not typed:
        return legacy_result(name, False, message=message, error_type=reason or "tool_error")
    if reason == "not_found" or message in {"Travel plan not found.", "Itinerary item not found.",
                                            "Recommendation not found.", "No matching address found"} or message.startswith("No route found from "):
        return failure(name, ErrorCode.NOT_FOUND, message)
    return failure(name, ErrorCode.INVALID_ARGUMENT, message, details={key: value[key] for key in (
        "valid_categories", "conflicting_item_id",
    ) if key in value})
