from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from service.utils.jsonl_log import append_jsonl, utc_now_iso
from service.utils.log_paths import transcript_file


class TranscriptLogger:
    """负责记录“第一本账”：本地聊天流水。

    这个类只做一件事：把用户消息、assistant 消息、工具调用、
    工具结果、系统附件等按顺序追加到当前会话的 .jsonl 文件。
    它不参与模型推理，也不影响工具执行结果。
    """

    def __init__(
        self,
        project_path: Path,
        *,
        session_id: str | None = None,
    ) -> None:
        self.project_path = project_path.resolve()
        self.session_id = session_id or uuid.uuid4().hex
        self.path = transcript_file(self.project_path, self.session_id)

    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        """写入一条带公共元数据的流水记录。"""

        append_jsonl(
            self.path,
            {
                "timestamp": utc_now_iso(),
                "sessionId": self.session_id,
                "projectPath": str(self.project_path),
                "type": event_type,
                **payload,
            },
        )

    def record_message(
        self,
        role: str,
        content: Any,
        *,
        tool_calls: Any | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        """记录一条聊天消息。

        assistant 消息如果携带 tool_calls，会同时保存在消息体里；
        usage 用来存模型 token 摘要，让 /usage 只扫描聊天流水即可统计。
        """

        message: dict[str, Any] = {
            "role": role,
            "content": content,
        }

        if tool_calls is not None:
            message["tool_calls"] = tool_calls

        if usage is not None:
            message["usage"] = usage

        self.record("message", {"message": message})

    def record_tool_use(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        round_index: int,
    ) -> None:
        """记录 assistant 发起的一次工具调用。"""

        self.record(
            "tool_use",
            {
                "round": round_index,
                "tool": name,
                "arguments": arguments,
            },
        )

    def record_tool_result(
        self,
        name: str,
        result_text: str,
        *,
        round_index: int,
    ) -> None:
        """记录工具执行后的返回结果。"""

        self.record(
            "tool_result",
            {
                "round": round_index,
                "tool": name,
                "result": result_text,
            },
        )

    def record_api_summary(self, summary: dict[str, Any]) -> None:
        """把 API 调用摘要同步写入聊天流水，供 /usage 汇总。"""

        self.record("api_summary", {"api": summary})
