"""迁移基线：只替换外部模型和 MCP，真正运行 Agent 的控制流程。"""

import json
import unittest
from ollama import ResponseError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent.context import AgentContext, DEFAULT_MAX_ROUNDS
from agent.runner import run_turn
from agent.state import AgentRequest, AgentRunError
from tooling.registry import ToolRegistry
from tooling.mcp import build_registry
from tools.catalog import tool_metadata

MAX_TOOL_ROUNDS = DEFAULT_MAX_ROUNDS


def response(text="完成", calls=None):
    return SimpleNamespace(
        message=SimpleNamespace(role="assistant", content=text, tool_calls=calls or []),
        usage={"prompt_tokens": 3},
    )


def tool_call(name="add_itinerary_item", **arguments):
    return {"function": {"name": name, "arguments": arguments}}


class ToolFlowTests(unittest.IsolatedAsyncioTestCase):
    async def run_query(self, responses, *, tools=None, names=None, tool_results=None):
        self.messages = [
            {"role": "system", "content": "旅行助手"},
            {"role": "user", "content": "安排活动"},
        ]
        self.emitted = []
        self.seen_messages = []
        self.logger = Mock()
        self.mcp = SimpleNamespace(
            call_tool=AsyncMock(side_effect=tool_results)
            if tool_results
            else AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent={"success": True, "item_id": "item-1"}
                )
            )
        )
        scripted = iter(responses)

        async def request(**kwargs):
            # 保存副本，才能检查后续轮次有没有拿到结果、校验和护栏消息。
            self.seen_messages.append(list(kwargs["messages"]))
            self.assertIsNone(kwargs["on_text"])
            item = next(scripted)
            if isinstance(item, Exception):
                raise item
            if isinstance(item, tuple):
                return item
            return item, list(kwargs["messages"]), None

        async def emit(text):
            self.emitted.append(text)

        names = names if names is not None else {"add_itinerary_item"}
        definitions = [
            SimpleNamespace(name=name, description=name, inputSchema={"type": "object"})
            for name in names
        ]
        registry = build_registry(self.mcp, definitions, tool_metadata())
        context = AgentContext(
            SimpleNamespace(),
            "test-model",
            registry,
            on_text=emit,
            transcript=self.logger,
            memory_enabled=False,
        )
        with patch("infrastructure.llm.request_model_with_retry", side_effect=request):
            try:
                self.result = await run_turn(AgentRequest("安排活动"), context)
            except AgentRunError as exc:
                self.result = exc.partial_result
                raise
            finally:
                self.messages = [*self.seen_messages[-1], *self.result.new_messages]
        return self.result.answer

    async def test_direct_answer_preserves_history_and_only_emits_final(self):
        answer = await self.run_query([response("可以按两天安排")])
        self.assertEqual(answer, "可以按两天安排")
        self.assertEqual(self.emitted, [answer])
        self.assertEqual(
            [m["role"] for m in self.messages], ["system", "user", "assistant"]
        )
        self.assertEqual(
            self.result.new_messages, [{"role": "assistant", "content": answer}]
        )
        self.mcp.call_tool.assert_not_awaited()

    async def test_tool_result_and_verification_reach_next_model_round(self):
        await self.run_query([response("", [tool_call(day=1)]), response()])
        self.mcp.call_tool.assert_awaited_once_with("add_itinerary_item", {"day": 1})
        tool_messages = [m for m in self.seen_messages[1] if m["role"] == "tool"]
        self.assertEqual(
            [m["tool_name"] for m in tool_messages],
            ["add_itinerary_item", "verify_last_tool_result"],
        )
        self.assertEqual(
            json.loads(tool_messages[1]["content"])["verification"]["status"], "passed"
        )
        self.assertEqual(self.emitted, ["完成"])

    async def test_business_failure_is_verified_before_corrected_call(self):
        await self.run_query(
            [
                response("", [tool_call(day=0)]),
                response("", [tool_call(day=1, _retry_of="r1c0")]),
                response(),
            ],
            tool_results=[
                SimpleNamespace(
                    structuredContent={
                        "success": False,
                        "message": "day must be positive",
                    }
                ),
                SimpleNamespace(structuredContent={"success": True}),
            ],
        )
        checks = [
            json.loads(m["content"])["verification"]["status"]
            for m in self.messages
            if m.get("tool_name") == "verify_last_tool_result"
        ]
        self.assertEqual(checks, ["failed", "passed"])
        self.assertEqual(self.result.status, "completed")
        self.assertEqual(
            [c.args[1] for c in self.mcp.call_tool.await_args_list],
            [{"day": 0}, {"day": 1}],
        )

    async def test_transport_failure_returns_to_model_as_failed_verification(self):
        await self.run_query(
            [response("", [tool_call()]), response("查询失败")],
            tool_results=[RuntimeError("timeout")],
        )
        result = json.loads(self.messages[-2]["content"])["verification"]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["retryable"])

    async def test_unknown_tool_does_not_execute(self):
        await self.run_query(
            [response("", [tool_call("erase_all")]), response("工具不可用")]
        )
        self.mcp.call_tool.assert_not_awaited()
        self.assertIn("unknown_tool", self.messages[3]["content"])

    async def test_max_rounds_stops_even_if_model_keeps_calling_tools(self):
        answer = await self.run_query([response("", [tool_call()])] * MAX_TOOL_ROUNDS)
        self.assertEqual(self.mcp.call_tool.await_count, MAX_TOOL_ROUNDS)
        self.assertIn("maximum number", answer)
        self.assertEqual(self.emitted, [answer])

    async def test_empty_promises_are_hidden_and_eventually_rejected(self):
        answer = await self.run_query([response("I will add this activity.")] * 3)
        self.assertIn("执行失败", answer)
        self.assertEqual(self.emitted, [answer])
        self.assertEqual(len(self.seen_messages), 3)
        self.mcp.call_tool.assert_not_awaited()

    async def test_real_tool_call_resets_promise_guard_counter(self):
        answer = await self.run_query(
            [
                response("I will add this activity."),
                response("I will add this activity."),
                response("", [tool_call()]),
                response("I will add this activity."),
                response("完成"),
            ]
        )
        self.assertEqual(answer, "完成")
        self.assertEqual(len(self.seen_messages), 5)

    async def test_retry_compression_replaces_history_without_duplication(self):
        compressed = [{"role": "system", "content": "保留摘要"}]
        await self.run_query(
            [(response("", [tool_call(day=1)]), compressed, None), response()]
        )
        self.assertEqual(self.seen_messages[1][0]["content"], "保留摘要")
        self.assertNotIn("旅行助手", [m["content"] for m in self.seen_messages[1]])
        self.assertEqual(
            self.result.new_messages, [{"role": "assistant", "content": "完成"}]
        )

    async def test_model_error_is_returned_and_logged_without_tool_execution(self):
        answer = await self.run_query(
            [(None, [{"role": "user", "content": "安排活动"}], "模型暂不可用")]
        )
        self.assertEqual(answer, "模型暂不可用")
        self.assertEqual(self.messages[-1]["content"], answer)
        self.assertEqual(self.result.status, "failed")
        self.mcp.call_tool.assert_not_awaited()

    async def test_actual_retry_wrapper_switches_to_fallback_model(self):
        # 保留真正的重试器，只替换 Ollama 网络边界和退避等待。
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    ResponseError("busy", status_code=529),
                    ResponseError("busy", status_code=529),
                    response("备用模型完成"),
                ]
            )
        )
        telemetry = Mock()
        with patch("infrastructure.llm.asyncio.sleep", new_callable=AsyncMock):
            answer = await run_turn(
                AgentRequest("规划旅行"),
                AgentContext(
                    client,
                    "primary",
                    ToolRegistry(),
                    fallback_model="backup",
                    telemetry=telemetry,
                    memory_enabled=False,
                ),
            )
        self.assertEqual(answer.answer, "备用模型完成")
        self.assertEqual(
            [c.kwargs["model"] for c in client.chat.await_args_list],
            ["primary", "primary", "backup"],
        )
        self.assertEqual(telemetry.record_error.call_count, 2)
        telemetry.record_success.assert_called_once()

    async def test_actual_context_compression_removes_old_messages(self):
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    ResponseError("context window exceeded", status_code=413),
                    response(),
                ]
            )
        )
        messages = [{"role": "system", "content": "规则"}] + [
            {"role": "user", "content": f"消息{i}"} for i in range(15)
        ]
        answer = await run_turn(
            AgentRequest("继续", history=messages),
            AgentContext(client, "test", ToolRegistry(), memory_enabled=False),
        )
        self.assertEqual(answer.answer, "完成")
        compressed = client.chat.await_args_list[-1].kwargs["messages"]
        self.assertNotIn("消息0", [m["content"] for m in compressed])
        self.assertEqual([m["content"] for m in compressed].count("消息14"), 1)
        self.assertEqual(client.chat.await_count, 2)

    async def test_streaming_failure_downgrades_without_emitting_partial_text(self):
        client = SimpleNamespace(
            chat=AsyncMock(side_effect=[RuntimeError("stream failed"), response()])
        )
        emit = AsyncMock()
        await run_turn(
            AgentRequest("介绍旅行"),
            AgentContext(
                client,
                "test",
                ToolRegistry(),
                streaming=True,
                on_text=emit,
                memory_enabled=False,
            ),
        )
        self.assertEqual(
            [c.kwargs["stream"] for c in client.chat.await_args_list], [True, False]
        )
        emit.assert_awaited_once_with("完成")

    async def test_unexpected_failure_keeps_already_executed_tools_in_history(self):
        # 意外异常仍需向上传播，但不能让调用方忘记本轮已经产生的副作用。
        with self.assertRaisesRegex(AgentRunError, "unexpected") as error:
            await self.run_query(
                [response("", [tool_call(day=1)]), RuntimeError("unexpected")]
            )
        self.mcp.call_tool.assert_awaited_once()
        self.assertEqual(
            [e["tool"] for e in error.exception.partial_result.tool_events],
            [
                "add_itinerary_item",
                "verify_last_tool_result",
            ],
        )
