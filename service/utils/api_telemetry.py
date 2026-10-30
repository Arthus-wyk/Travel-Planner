from __future__ import annotations

from time import perf_counter
from typing import Any

from service.utils.jsonl_log import append_jsonl, utc_now_iso
from service.utils.log_paths import API_EVENTS_FILE
from service.utils.runtime_log import record_event


class ApiTelemetryLogger:
    """负责记录“第二本账”：模型 API 调用诊断事件。

    这里记录的是一次 API 请求本身，而不是完整聊天内容：
    请求用了哪个模型、传了多少 messages、是否 streaming、耗时、
    token 统计、异常类型、是否重试等。
    """

    def __init__(self, *, session_id: str, project_path: str) -> None:
        self.session_id = session_id
        self.project_path = project_path

    def start_timer(self) -> float:
        """返回高精度起点，供 success/error 计算耗时。"""

        return perf_counter()

    def record_query(
        self,
        *,
        model: str,
        message_count: int,
        tool_count: int,
        attempt: int,
        streamed: bool,
    ) -> None:
        """记录 API 请求即将发出。"""

        self._record(
            "tengu_api_query",
            {
                "model": model,
                "messageCount": message_count,
                "toolCount": tool_count,
                "attempt": attempt,
                "streamed": streamed,
            },
        )

    def record_success(
        self,
        *,
        model: str,
        message_count: int,
        tool_count: int,
        attempt: int,
        streamed: bool,
        duration_seconds: float,
        usage: dict[str, Any],
    ) -> None:
        """记录 API 请求成功返回。"""

        self._record(
            "tengu_api_success",
            {
                "model": model,
                "messageCount": message_count,
                "toolCount": tool_count,
                "attempt": attempt,
                "streamed": streamed,
                "durationMs": round(duration_seconds * 1000, 2),
                **usage,
            },
        )

    def record_error(
        self,
        *,
        model: str,
        message_count: int,
        tool_count: int,
        attempt: int,
        streamed: bool,
        duration_seconds: float,
        error: Exception,
        status_code: int | None,
        will_retry: bool,
    ) -> None:
        """记录 API 请求失败。"""

        self._record(
            "tengu_api_error",
            {
                "model": model,
                "messageCount": message_count,
                "toolCount": tool_count,
                "attempt": attempt,
                "streamed": streamed,
                "durationMs": round(duration_seconds * 1000, 2),
                "statusCode": status_code,
                "errorType": type(error).__name__,
                "message": str(error),
                "willRetry": will_retry,
            },
        )

    def _record(self, event_name: str, payload: dict[str, Any]) -> None:
        record_event(event_name, payload)
        append_jsonl(
            API_EVENTS_FILE,
            {
                "timestamp": utc_now_iso(),
                "event": event_name,
                "sessionId": self.session_id,
                "projectPath": self.project_path,
                **payload,
            },
        )
