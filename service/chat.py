"""HTTP 聊天用例：读会话、调用 Agent、保存本轮产物；不包含图内部决策。"""

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from agent.context import AgentContext
from agent.prompts import is_empty_execution_promise
from agent.runner import run_turn
from agent.state import AgentCancelledError, AgentRequest, AgentResult, AgentRunError
from infrastructure.mcp import connect_tools
from service.storage.travel_db import (
    get_conversation,
    get_plan,
    list_messages,
    next_message_sequence,
    save_conversation,
    save_message,
)
from service.utils.api_telemetry import ApiTelemetryLogger
from service.utils.model_client import create_model_client
from service.utils.transcript_log import TranscriptLogger
from service.utils.runtime_log import trace_run, current_run


def save_turn_message(
    conversation_id: str, role: str, content: str, metadata: dict | None = None
) -> None:
    save_message(
        {
            "message_id": uuid.uuid4().hex,
            "conversation_id": conversation_id,
            "role": role,
            "content": content,
            "tool_name": None,
            "metadata": {**(metadata or {}), **({"run_id": current_run.get().run_id} if current_run.get() else {})},
            "sequence_no": next_message_sequence(conversation_id),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )


def persist_result(conversation: dict, result: AgentResult) -> list[dict]:
    """只保存明确的新消息，绝不从可被压缩的模型历史按下标截取。"""
    actions = []
    plan = result.plan
    if plan and plan.get("plan_id"):
        actions = [
            {
                "type": "navigate",
                "label": "查看行程详情",
                "href": f"/plans/{plan['plan_id']}",
                "plan_id": plan["plan_id"],
            }
        ]
        save_conversation(
            {
                **conversation,
                "plan_id": plan["plan_id"],
                "title": conversation.get("title")
                or f"{plan.get('destination', '行程')} · 对话",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    for message in result.new_messages:
        save_turn_message(
            conversation["conversation_id"],
            message["role"],
            message["content"],
            {"actions": actions, "tool_events": result.tool_events},
        )
    return actions


async def handle_chat(
    *, server_file: Path, model: str, conversation_id: str, user_message: str
) -> dict[str, Any]:
    if not re.fullmatch(r"[\w-]{1,128}", conversation_id):
        raise ValueError("Invalid conversation_id")
    transcript = TranscriptLogger(Path.cwd(), session_id=conversation_id)
    with trace_run(transcript, model=model, text=user_message) as run:
        result = await _handle_chat(
            server_file=server_file, model=model, conversation_id=conversation_id,
            user_message=user_message, transcript=transcript,
        )
        result["run_id"] = run.run_id
        return result


async def _handle_chat(
    *, server_file: Path, model: str, conversation_id: str, user_message: str,
    transcript: TranscriptLogger,
) -> dict[str, Any]:
    conversation, error = get_conversation(conversation_id)
    if error is not None or conversation is None:
        raise ValueError((error or {}).get("message", "Conversation not found."))
    stored, error = list_messages(conversation_id)
    if error is not None:
        raise ValueError(error.get("message", "Unable to load messages."))
    plan = None
    if conversation.get("plan_id"):
        plan, error = get_plan(conversation["plan_id"])
        if error is not None:
            raise ValueError(error.get("message", "Unable to load plan."))
    history = [
        {"role": m["role"], "content": m["content"]}
        for m in public_message_items(stored or [])
    ]
    request = AgentRequest(user_message, history=history, current_plan=plan)
    telemetry = ApiTelemetryLogger(
        session_id=transcript.session_id, project_path=str(transcript.project_path)
    )
    save_turn_message(conversation_id, "user", user_message)
    async with connect_tools(server_file) as registry:
        try:
            result = await run_turn(
                request,
                AgentContext(
                    create_model_client(),
                    model,
                    registry,
                    transcript=transcript,
                    telemetry=telemetry,
                ),
            )
        except (AgentRunError, AgentCancelledError) as exc:
            # 在 MCP/AnyIO 上下文退出前保存，避免退出时 ExceptionGroup 包装掩盖原异常。
            partial = exc.partial_result
            if not partial.new_messages:
                partial.new_messages = [
                    {
                        "role": "assistant",
                        "content": "执行中断，部分操作可能已经完成，请先检查当前行程。",
                    }
                ]
            persist_result(conversation, partial)
            raise
    actions = persist_result(conversation, result)
    # HTTP 兼容转换只在边界做；核心中的 ToolResult.data 始终是结构化对象。
    events = [
        {
            **event,
            "result": json.dumps(event["result"], ensure_ascii=False)
            if event["result"] is not None and not isinstance(event["result"], str)
            else event["result"],
        }
        for event in result.tool_events[-8:]
    ]
    return {
        "success": True,
        "conversation_id": conversation_id,
        "answer": result.answer,
        "understanding": result.understanding,
        "tool_events": events,
        "plan": result.plan,
        "actions": actions,
        "memory": result.memory,
        "messages": public_messages_from_db(conversation_id),
    }


def public_messages_from_db(conversation_id: str) -> list[dict[str, Any]]:
    messages, error = list_messages(conversation_id)
    if error is not None:
        return []
    return public_message_items(messages or [])


def public_message_items(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "message_id": item.get("message_id"),
            "role": item["role"],
            "content": item["content"],
            "tool_name": item.get("tool_name"),
            "metadata": item.get("metadata", {}),
            "created_at": item.get("created_at"),
        }
        for item in messages
        if item.get("role") in {"user", "assistant"}
        and (item.get("metadata") or {}).get("visibility") != "internal"
        and not (
            item.get("role") == "assistant"
            and (item.get("metadata") or {}).get("tool_calls")
        )
        and not (
            item.get("role") == "assistant"
            and is_empty_execution_promise(str(item.get("content") or ""))
        )
        and not is_internal_agent_control_message(str(item.get("content") or ""))
    ]


def is_internal_agent_control_message(content: str) -> bool:
    normalized = content.strip()
    return normalized.startswith(
        "The previous assistant message described future tool work"
    )
