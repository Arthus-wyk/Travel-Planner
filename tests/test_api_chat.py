"""HTTP 业务入口的离线基线：数据库、日志、网络均不会产生真实副作用。"""

import unittest
import anyio
from contextlib import asynccontextmanager, ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from service import chat as server
from test_agent_query import response, tool_call
from tooling.mcp import build_registry
from tools.catalog import tool_metadata


@asynccontextmanager
async def fake_stdio(_):
    yield (None, None)


class ApiChatTests(unittest.IsolatedAsyncioTestCase):
    async def run_chat(
        self,
        *,
        plan_id=None,
        error=None,
        tool_plan=False,
        compress=False,
        crash=False,
        grouped=False,
    ):
        self.saved = []
        self.saved_conversations = []
        self.model_messages = []
        self.plan = {"plan_id": "trip-1", "destination": "杭州", "travelers": 2}
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.list_tools.return_value = SimpleNamespace(tools=[])
        if tool_plan:
            session.list_tools.return_value.tools = [
                SimpleNamespace(
                    name="get_travel_plan",
                    description="读取计划",
                    inputSchema={"type": "object"},
                )
            ]
            session.call_tool.return_value = SimpleNamespace(
                structuredContent={"success": True, "plan": self.plan}
            )
        self.session = session
        responses = iter(
            [
                response("", [tool_call("get_travel_plan", plan_id="trip-1")]),
                RuntimeError("unexpected") if crash else response("建议慢慢游览"),
            ]
            if tool_plan
            else [response("建议慢慢游览")]
        )

        async def request(**kwargs):
            self.model_messages = list(kwargs["messages"])
            messages = (
                [{"role": "system", "content": "压缩后的摘要"}]
                if compress
                else list(kwargs["messages"])
            )
            item = next(responses)
            if isinstance(item, Exception):
                raise item
            return item, messages, None

        with ExitStack() as stack:
            patches = {
                "get_conversation": Mock(
                    return_value=(
                        {"conversation_id": "chat-1", "plan_id": plan_id},
                        error,
                    )
                ),
                "list_messages": Mock(
                    return_value=(
                        [{"role": "assistant", "content": "已有建议", "metadata": {}}]
                        * (20 if compress else 1),
                        None,
                    )
                ),
                "get_plan": Mock(return_value=(self.plan, None)),
                "save_conversation": Mock(side_effect=self.saved_conversations.append),
                "save_message": Mock(side_effect=lambda item: self.saved.append(item)),
                "next_message_sequence": Mock(return_value=1),
                "create_model_client": Mock(return_value=SimpleNamespace()),
                "TranscriptLogger": Mock(),
                "ApiTelemetryLogger": Mock(),
                "public_messages_from_db": Mock(return_value=[]),
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(server, name, value))

            @asynccontextmanager
            async def connection(_):
                await session.initialize()
                registry = build_registry(
                    session, session.list_tools.return_value.tools, tool_metadata()
                )
                if grouped:
                    async with anyio.create_task_group():
                        yield registry
                else:
                    yield registry

            stack.enter_context(patch.object(server, "connect_tools", connection))
            stack.enter_context(
                patch(
                    "infrastructure.llm.request_model_with_retry", side_effect=request
                )
            )
            self.memory = stack.enter_context(
                patch(
                    "agent.memory.extract_and_store",
                    new_callable=AsyncMock,
                    return_value={"success": True},
                )
            )
            text = (
                "不要参考历史，查看当前旅行计划"
                if tool_plan
                else "不要参考历史，推荐杭州景点"
            )
            return await server.handle_chat(
                server_file=Path("mcp_server.py"),
                model="test",
                conversation_id="chat-1",
                user_message=text,
            )

    async def test_chat_preserves_public_contract_and_stores_raw_user_text_once(self):
        result = await self.run_chat(plan_id="trip-1")
        self.assertEqual(result["answer"], "建议慢慢游览")
        self.assertEqual(
            set(result),
            {
                "success",
                "run_id",
                "conversation_id",
                "answer",
                "understanding",
                "tool_events",
                "plan",
                "actions",
                "memory",
                "messages",
            },
        )
        self.assertIn('"plan_id": "trip-1"', self.model_messages[0]["content"])
        self.assertEqual(self.model_messages[1]["content"], "已有建议")
        self.assertIn("[User intent understanding]", self.model_messages[2]["content"])
        users = [m for m in self.saved if m["role"] == "user"]
        self.assertRegex(result["run_id"], r"^[a-f0-9]{32}$")
        self.assertTrue(all(m["metadata"]["run_id"] == result["run_id"] for m in self.saved))
        self.assertEqual([m["content"] for m in users], ["不要参考历史，推荐杭州景点"])
        self.memory.assert_not_awaited()
        self.session.initialize.assert_awaited_once()

    async def test_unknown_conversation_fails_before_saving_or_connecting(self):
        with self.assertRaisesRegex(ValueError, "not found"):
            await self.run_chat(error={"message": "Conversation not found"})
        self.assertEqual(self.saved, [])
        self.session.initialize.assert_not_awaited()

    async def test_tool_plan_and_navigation_are_returned(self):
        result = await self.run_chat(plan_id="trip-1", tool_plan=True)
        self.assertEqual(result["plan"]["plan_id"], "trip-1")
        self.assertEqual(result["actions"][0]["href"], "/plans/trip-1")
        self.assertEqual(
            [e["tool"] for e in result["tool_events"]],
            ["get_travel_plan", "verify_last_tool_result"],
        )
        self.session.call_tool.assert_awaited_once_with(
            "get_travel_plan", {"plan_id": "trip-1"}
        )
        self.assertEqual(len([m for m in self.saved if m["role"] == "assistant"]), 1)

    async def test_compressed_history_still_persists_current_answer_once(self):
        result = await self.run_chat(plan_id="trip-1", compress=True)
        self.assertEqual(
            [m["content"] for m in self.saved if m["role"] == "assistant"],
            [result["answer"]],
        )

    async def test_mcp_task_group_does_not_hide_partial_result_persistence(self):
        with self.assertRaises(Exception):
            await self.run_chat(tool_plan=True, crash=True, grouped=True)
        assistant = [m for m in self.saved if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertIn("中断", assistant[0]["content"])
        self.assertEqual(self.saved_conversations[-1]["plan_id"], "trip-1")
