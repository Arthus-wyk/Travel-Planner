from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from service.agent.client import (
    SYSTEM_PROMPT,
    answer_user,
    is_empty_execution_promise,
    mcp_tools_to_ollama,
)
from service.agent.memory import (
    extract_and_store,
    memory_context,
    memory_policy,
    read_mid_term,
)
from service.agent.preference_profile import read_preference_profile
from service.agent.intent_understanding import understand_user_input
from service.storage.travel_db import (
    get_conversation,
    get_plan,
    list_conversations,
    list_messages,
    list_plan_summaries,
    next_message_sequence,
    save_conversation,
    save_message,
)
from service.utils.api_telemetry import ApiTelemetryLogger
from service.utils.ollama_client import DEFAULT_MODEL, create_ollama_client
from service.utils.transcript_log import TranscriptLogger



def make_handler(server_file: Path, model: str):
    class AgentApiHandler(BaseHTTPRequestHandler):
        server_version = "TravelAgentApi/0.1"

        def do_OPTIONS(self) -> None:
            self._send_json({"ok": True})

        def do_GET(self) -> None:
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "model": model,
                        "routes": ["plans", "conversations", "messages", "chat"],
                    }
                )
                return
            if path == "/api/plans":
                plans, error = list_plan_summaries()
                self._send_result({"plans": plans}, error)
                return
            if path == "/api/conversations":
                conversations, error = list_conversations()
                self._send_result({"conversations": conversations}, error)
                return
            if path.startswith("/api/plans/") and path.endswith("/conversations"):
                plan_id = unquote(path.removeprefix("/api/plans/").removesuffix("/conversations"))
                conversations, error = list_conversations(plan_id)
                self._send_result({"conversations": conversations}, error)
                return
            if path.startswith("/api/conversations/") and path.endswith("/messages"):
                conversation_id = unquote(
                    path.removeprefix("/api/conversations/").removesuffix("/messages")
                )
                messages, error = list_messages(conversation_id)
                status = 404 if error and "not found" in error.get("message", "").lower() else 500
                self._send_result(
                    {"messages": public_message_items(messages or [])},
                    error,
                    error_status=status,
                )
                return
            if path.startswith("/api/conversations/"):
                conversation_id = unquote(path.removeprefix("/api/conversations/"))
                conversation, error = get_conversation(conversation_id)
                status = 404 if error and "not found" in error.get("message", "").lower() else 500
                self._send_result({"conversation": conversation}, error, error_status=status)
                return
            if path.startswith("/api/plans/"):
                plan_id = unquote(path.removeprefix("/api/plans/"))
                plan, error = get_plan(plan_id)
                status = 404 if error and error.get("message") == "Travel plan not found." else 500
                self._send_result({"plan": plan}, error, error_status=status)
                return
            self._send_json({"error": "Not found"}, status=404)

        def do_POST(self) -> None:
            path = urlparse(self.path).path.rstrip("/")
            if path == "/api/conversations":
                self._create_conversation()
                return
            if path.startswith("/api/conversations/") and path.endswith("/messages"):
                conversation_id = unquote(
                    path.removeprefix("/api/conversations/").removesuffix("/messages")
                )
                self._append_conversation_message(conversation_id)
                return
            if path == "/api/chat":
                self._chat()
                return
            self._send_json({"error": "Not found"}, status=404)

        def _create_conversation(self) -> None:
            try:
                payload = self._read_json()
                conversation = new_conversation(
                    conversation_id=str(payload.get("conversation_id") or uuid.uuid4().hex),
                    plan_id=payload.get("plan_id") or None,
                    title=str(payload.get("title") or "新对话"),
                )
                save_conversation(conversation)
                self._send_json({"success": True, "conversation": conversation})
            except Exception as exc:
                self._send_json(
                    {"success": False, "message": f"{type(exc).__name__}: {exc}"},
                    status=500,
                )

        def _append_conversation_message(self, conversation_id: str) -> None:
            try:
                payload = self._read_json()
                user_message = str(payload.get("content") or payload.get("message") or "").strip()
                if not user_message:
                    self._send_json(
                        {"success": False, "message": "content must not be empty."},
                        status=400,
                    )
                    return
                result = asyncio.run(
                    handle_chat(
                        server_file=server_file,
                        model=str(payload.get("model") or model),
                        conversation_id=conversation_id,
                        user_message=user_message,
                    )
                )
                self._send_json(result)
            except Exception as exc:
                traceback.print_exc()
                self._send_json(
                    {"success": False, "message": f"{type(exc).__name__}: {exc}"},
                    status=500,
                )

        def _chat(self) -> None:
            try:
                payload = self._read_json()
                user_message = str(payload.get("message", "")).strip()
                conversation_id = str(payload.get("conversation_id") or uuid.uuid4().hex)
                if not get_conversation(conversation_id)[0]:
                    save_conversation(
                        new_conversation(
                            conversation_id=conversation_id,
                            plan_id=payload.get("plan_id"),
                            title="新对话",
                        )
                    )
                if not user_message:
                    self._send_json(
                        {"success": False, "message": "message must not be empty."},
                        status=400,
                    )
                    return
                result = asyncio.run(
                    handle_chat(
                        server_file=server_file,
                        model=str(payload.get("model") or model),
                        conversation_id=conversation_id,
                        user_message=user_message,
                    )
                )
                self._send_json(result)
            except Exception as exc:
                traceback.print_exc()
                self._send_json(
                    {"success": False, "message": f"{type(exc).__name__}: {exc}"},
                    status=500,
                )

        def _send_result(
            self,
            payload: dict[str, Any],
            error: dict[str, Any] | None,
            *,
            error_status: int = 500,
        ) -> None:
            if error is not None:
                self._send_json({"success": False, **error}, status=error_status)
                return
            self._send_json({"success": True, **payload})

        def log_message(self, format: str, *args: Any) -> None:
            sys.stderr.write("[api] " + format % args + "\n")

        def _read_json(self) -> dict[str, Any]:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
            if not body:
                return {}
            return json.loads(body.decode("utf-8"))

        def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

    return AgentApiHandler


def new_conversation(
    *,
    conversation_id: str,
    plan_id: str | None,
    title: str,
) -> dict[str, Any]:
    now_value = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {
        "conversation_id": conversation_id,
        "plan_id": plan_id,
        "title": title,
        "created_at": now_value,
        "updated_at": now_value,
    }


async def handle_chat(
    *,
    server_file: Path,
    model: str,
    conversation_id: str,
    user_message: str,
) -> dict[str, Any]:
    conversation, conversation_error = get_conversation(conversation_id)
    if conversation_error is not None or conversation is None:
        raise ValueError(conversation_error.get("message", "Conversation not found."))

    stored_messages, messages_error = list_messages(conversation_id)
    if messages_error is not None:
        raise ValueError(messages_error.get("message", "Unable to load messages."))

    plan = None
    if conversation.get("plan_id"):
        plan, plan_error = get_plan(conversation["plan_id"])
        if plan_error is not None:
            raise ValueError(plan_error.get("message", "Unable to load plan."))

    messages = [{"role": "system", "content": build_system_prompt(plan)}]
    for item in public_message_items(stored_messages or []):
        message = {
            "role": item["role"],
            "content": item["content"],
        }
        messages.append(message)

    turn_start = len(messages)
    ollama = create_ollama_client()
    profile = read_preference_profile().get("profile", {})
    request_memory_policy = memory_policy(user_message)
    understanding = await understand_user_input(
        user_message,
        messages,
        ollama=ollama,
        model=model,
        memory_context=memory_context(
            user_message,
            profile,
            read_mid_term(),
        ),
        memory_policy=request_memory_policy,
    )

    transcript_logger = TranscriptLogger(Path.cwd(), session_id=conversation_id)
    api_telemetry = ApiTelemetryLogger(
        session_id=transcript_logger.session_id,
        project_path=str(transcript_logger.project_path),
    )
    transcript_logger.record_message("user", user_message)
    transcript_logger.record("intent_understanding", understanding.to_dict())

    messages.append({"role": "user", "content": understanding.format_for_model()})
    save_message(
        {
            "message_id": uuid.uuid4().hex,
            "conversation_id": conversation_id,
            "role": "user",
            "content": user_message,
            "tool_name": None,
            "metadata": {},
            "sequence_no": next_message_sequence(conversation_id),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

    server = StdioServerParameters(command=sys.executable, args=[str(server_file)])
    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as mcp:
            await mcp.initialize()
            listed = await mcp.list_tools()
            tools = mcp_tools_to_ollama(listed.tools)
            tool_names = {tool.name for tool in listed.tools}

            answer = await answer_user(
                ollama,
                mcp,
                model,
                tools,
                tool_names,
                messages,
                use_streaming=False,
                transcript_logger=transcript_logger,
                api_telemetry=api_telemetry,
            )

    memory_result = None
    if request_memory_policy["read_long_term"]:
        memory_result = await extract_and_store(
            ollama,
            model,
            user_message,
            answer,
            plan_id=conversation.get("plan_id"),
        )
        transcript_logger.record("memory_extraction", memory_result)

    latest_plan = extract_latest_plan(messages)
    # Some models emit only a plan_id after completing tool calls.  That value
    # is not a user-facing answer (and can even contain a typo), while the
    # preceding tool result already contains the authoritative plan object.
    # Replace the placeholder with a deterministic summary before persisting
    # and returning messages to the frontend.
    if latest_plan and _is_plan_id_only_answer(answer):
        answer = _build_plan_completion_message(latest_plan)
    actions = []
    if latest_plan and latest_plan.get("plan_id"):
        actions.append(
            {
                "type": "navigate",
                "label": "查看行程详情",
                "href": f"/plans/{latest_plan['plan_id']}",
                "plan_id": latest_plan["plan_id"],
            }
        )
        save_conversation(
            {
                **conversation,
                "plan_id": latest_plan["plan_id"],
                "title": conversation.get("title")
                or f"{latest_plan.get('destination', '行程')} · 对话",
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

    persist_new_model_messages(conversation_id, messages[turn_start:], actions)
    return {
        "success": True,
        "conversation_id": conversation_id,
        "answer": answer,
        "understanding": understanding.to_dict(),
        "tool_events": extract_recent_tool_events(messages),
        "plan": latest_plan,
        "actions": actions,
        "memory": memory_result,
        "messages": public_messages_from_db(conversation_id),
    }


def _is_plan_id_only_answer(answer: str) -> bool:
    """Whether the model returned only an internal plan identifier."""
    parsed = _json_or_none(answer)
    return (
        isinstance(parsed, dict)
        and isinstance(parsed.get("plan_id"), str)
        and set(parsed) <= {"plan_id"}
    ) or not answer.strip()


def _build_plan_completion_message(plan: dict[str, Any]) -> str:
    """Create a stable user-facing completion message from the saved plan."""
    plan_id = plan.get("plan_id", "")
    title = plan.get("title") or plan.get("destination") or "当前行程"
    itinerary_count = len(plan.get("itinerary") or [])
    recommendation_count = len(plan.get("recommendations") or [])
    return (
        f"已更新行程“{title}”。\n"
        f"已保存 {itinerary_count} 个行程安排和 {recommendation_count} 个推荐。\n"
        f"行程编号：{plan_id}"
    )


def build_system_prompt(plan: dict[str, Any] | None) -> str:
    if not plan:
        return SYSTEM_PROMPT
    trip_days = _trip_day_count(plan)
    summary = _current_plan_summary(plan, trip_days)
    context = json.dumps(
        {
            "plan_id": plan.get("plan_id"),
            "title": plan.get("title"),
            "destination": plan.get("destination"),
            "start_date": plan.get("start_date"),
            "end_date": plan.get("end_date"),
            "travelers": plan.get("travelers"),
            "budget": plan.get("budget"),
            "currency": plan.get("currency"),
            "interests": plan.get("interests"),
            "constraints": plan.get("constraints"),
            "itinerary": plan.get("itinerary"),
            "recommendations": plan.get("recommendations"),
            "budget_items": plan.get("budget_items"),
        },
        ensure_ascii=False,
    )
    return (
        SYSTEM_PROMPT
        + "\n\n[Current travel plan summary]\n"
        + summary
        + f"\n\nCurrent travel plan context (JSON):\n{context}\n\n"
        + "[Current-plan execution rules]\n"
        + f"- Always reuse plan_id: {plan.get('plan_id')} for edits to this trip.\n"
        + "- The summary and JSON above are authoritative. Never ask again for any "
        + "field that is already present there.\n"
        + "- If the user asks for a multi-day plan, day-by-day route, or N-day "
        + "N-night itinerary, call tools to add or update itinerary items on this "
        + "existing plan.\n"
        + "- Missing start_date/end_date does not block planning by relative days. "
        + "Use day 1, day 2, day 3, etc.\n"
        + "- Create a new plan only when the user explicitly asks for a separate "
        + "trip, a new destination workspace, or to start over."
    )


def _current_plan_summary(plan: dict[str, Any], trip_days: int | None) -> str:
    date_text = _date_summary(plan, trip_days)
    itinerary_count = len(plan.get("itinerary") or [])
    recommendation_count = len(plan.get("recommendations") or [])
    budget_item_count = len(plan.get("budget_items") or [])
    return "\n".join(
        [
            f"- plan_id: {plan.get('plan_id')}",
            f"- title: {plan.get('title') or ''}",
            f"- destination: {plan.get('destination') or ''}",
            f"- dates: {date_text}",
            f"- travelers: {plan.get('travelers') or 'unknown'}",
            f"- budget: {plan.get('budget') or 'unknown'} {plan.get('currency') or ''}".rstrip(),
            f"- interests: {', '.join(plan.get('interests') or []) or 'none'}",
            f"- constraints: {', '.join(plan.get('constraints') or []) or 'none'}",
            f"- existing itinerary items: {itinerary_count}",
            f"- existing recommendations: {recommendation_count}",
            f"- existing budget items: {budget_item_count}",
        ]
    )


def _date_summary(plan: dict[str, Any], trip_days: int | None) -> str:
    start = plan.get("start_date")
    end = plan.get("end_date")
    if start and end:
        day_text = f", {trip_days} days" if trip_days else ""
        return f"{start} to {end}{day_text}"
    return "not set; relative day planning is allowed"


def _trip_day_count(plan: dict[str, Any]) -> int | None:
    start = str(plan.get("start_date") or "")
    end = str(plan.get("end_date") or "")
    if not start or not end:
        return None
    try:
        start_date = datetime.fromisoformat(start).date()
        end_date = datetime.fromisoformat(end).date()
    except ValueError:
        return None
    days = (end_date - start_date).days + 1
    return days if days > 0 else None


def persist_new_model_messages(
    conversation_id: str,
    messages: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> None:
    for message in messages:
        if message.get("_internal"):
            continue
        role = message.get("role")
        if role != "assistant":
            continue
        metadata = {}
        if message.get("tool_calls") is not None:
            metadata["tool_calls"] = message["tool_calls"]
        if role == "assistant" and actions:
            metadata["actions"] = actions
        save_message(
            {
                "message_id": uuid.uuid4().hex,
                "conversation_id": conversation_id,
                "role": role,
                "content": str(message.get("content") or ""),
                "tool_name": message.get("tool_name"),
                "metadata": metadata,
                "sequence_no": next_message_sequence(conversation_id),
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )


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


def extract_recent_tool_events(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = []
    for message in messages:
        if message.get("role") != "tool":
            continue
        parsed = _json_or_none(message.get("content"))
        if not isinstance(parsed, dict):
            continue
        events.append(
            {
                "tool": parsed.get("tool") or message.get("tool_name"),
                "status": parsed.get("status"),
                "result": parsed.get("result"),
                "message": parsed.get("message"),
                "verification": parsed.get("verification"),
            }
        )
    return events[-8:]


def extract_latest_plan(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    latest_plan = None
    for message in messages:
        if message.get("role") != "tool":
            continue
        parsed = _json_or_none(message.get("content"))
        if not isinstance(parsed, dict):
            continue
        result_payload = _json_or_none(parsed.get("result"))
        if not isinstance(result_payload, dict):
            continue

        plan = result_payload.get("plan")
        if isinstance(plan, dict):
            latest_plan = plan
            continue

        summary = result_payload.get("summary")
        if isinstance(summary, dict) and latest_plan is None:
            latest_plan = {"summary": summary}

    return latest_plan


def _json_or_none(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if not isinstance(value, str):
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HTTP bridge for the travel MCP agent.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument(
        "--server",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "mcp" / "server.py",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    handler = make_handler(args.server.resolve(), args.model)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"Agent API listening on http://{args.host}:{args.port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Agent API.")


if __name__ == "__main__":
    main()
