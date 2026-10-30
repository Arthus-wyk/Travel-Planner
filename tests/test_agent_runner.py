"""新核心的外部契约：不继承旧执行器测试，也不依赖内部提示词格式。"""

import unittest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.context import AgentContext
from agent.runner import run_turn
from agent.state import AgentCancelledError, AgentRequest
from tooling.registry import ToolRegistry
from tooling.spec import ToolSpec


class RunnerContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_validation_error_gets_repair_round_without_user_intervention(self):
        def reply(calls=None, text=""):
            return SimpleNamespace(message=SimpleNamespace(role="assistant", content=text, tool_calls=calls or []))

        def call(arguments):
            return {"function": {"name": "update", "arguments": arguments}}

        handler = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("update", "Update", {
            "type": "object", "properties": {"constraints": {"type": "array", "items": {"type": "string"}}},
            "required": ["constraints"], "additionalProperties": False,
        }, handler)])
        client = SimpleNamespace(chat=AsyncMock(side_effect=[
            reply([call({"dietary_preferences": "不要辣"})]),
            reply(text="工具失败，无法进行"),
            reply([call({"constraints": ["不要辣"]})]),
            reply(text="已更新"),
        ]))
        result = await run_turn(AgentRequest("不要辣"), AgentContext(client, "test", registry, max_rounds=2, memory_enabled=False))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.answer, "已更新")
        handler.assert_awaited_once()

    async def test_repeated_invalid_arguments_stop_after_three_attempts(self):
        bad_call = {"function": {"name": "update", "arguments": {"wrong": 1}}}
        def reply(calls=None, text=""):
            return SimpleNamespace(message=SimpleNamespace(role="assistant", content=text, tool_calls=calls or []))
        handler = AsyncMock()
        registry = ToolRegistry([ToolSpec("update", "Update", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False,
        }, handler)])
        client = SimpleNamespace(chat=AsyncMock(side_effect=[
            reply([bad_call]), reply([bad_call]), reply([bad_call]), reply(text="需要更多信息"),
        ]))
        result = await run_turn(AgentRequest("更新"), AgentContext(client, "test", registry, memory_enabled=False))
        self.assertEqual(result.status, "failed")
        self.assertEqual(client.chat.await_count, 4)
        handler.assert_not_awaited()

    async def test_tool_reported_validation_error_is_repaired(self):
        def reply(calls=None, text=""):
            return SimpleNamespace(message=SimpleNamespace(role="assistant", content=text, tool_calls=calls or []))
        def call(day):
            return {"function": {"name": "update", "arguments": {"day": day}}}
        handler = AsyncMock(side_effect=[
            {"success": False, "message": "day must be positive", "error_type": "validation_error"},
            {"success": True},
        ])
        registry = ToolRegistry([ToolSpec("update", "Update", {
            "type": "object", "properties": {"day": {"type": "integer"}}, "required": ["day"]
        }, handler)])
        client = SimpleNamespace(chat=AsyncMock(side_effect=[
            reply([call(0)]), reply(text="工具失败"), reply([call(1)]), reply(text="完成"),
        ]))
        result = await run_turn(AgentRequest("改成第一天"), AgentContext(client, "test", registry, memory_enabled=False))
        self.assertEqual(result.status, "completed")
        self.assertEqual(handler.await_count, 2)


    async def test_identical_new_call_does_not_implicitly_resolve_another_operation(
        self,
    ):
        call = {"function": {"name": "add", "arguments": {"title": "A"}}}
        handler = AsyncMock(side_effect=[{"success": False}, {"success": True}])
        registry = ToolRegistry([ToolSpec("add", "Add", {}, handler)])
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="", tool_calls=[call, call]
                        )
                    ),
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="完成一项", tool_calls=[]
                        )
                    ),
                ]
            )
        )
        result = await run_turn(
            AgentRequest("添加两个相同活动"),
            AgentContext(client, "test", registry, memory_enabled=False),
        )
        self.assertEqual(result.status, "failed")

    async def test_empty_model_response_is_failed(self):
        client = SimpleNamespace(
            chat=AsyncMock(
                return_value=SimpleNamespace(
                    message=SimpleNamespace(role="assistant", content="", tool_calls=[])
                )
            )
        )
        result = await run_turn(
            AgentRequest("介绍旅行"),
            AgentContext(client, "test", ToolRegistry(), memory_enabled=False),
        )
        self.assertEqual(result.status, "failed")
        self.assertIn("empty response", result.answer)

    async def test_explicit_retry_id_takes_precedence_over_matching_arguments(self):
        def reply(calls=None):
            return SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content="" if calls else "完成",
                    tool_calls=calls or [],
                )
            )

        def call(title, retry_of=None):
            args = {"title": title}
            if retry_of is not None:
                args["_retry_of"] = retry_of
            return {"function": {"name": "add", "arguments": args}}

        handler = AsyncMock(
            side_effect=[
                {"success": False},
                {"success": False},
                {"success": True},
                {"success": True},
            ]
        )
        registry = ToolRegistry([ToolSpec("add", "Add", {}, handler)])
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    reply([call("X"), call("Y")]),
                    reply([call("X", "r1c1")]),
                    reply([call("Z", "r1c0")]),
                    reply(),
                ]
            )
        )
        result = await run_turn(
            AgentRequest("添加活动"),
            AgentContext(client, "test", registry, memory_enabled=False),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(handler.await_count, 4)

    async def test_direct_answer_has_explicit_new_messages(self):
        client = SimpleNamespace(
            chat=AsyncMock(
                return_value=SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant", content="杭州适合慢游", tool_calls=[]
                    ),
                )
            )
        )
        request = AgentRequest(
            "不要参考历史，介绍杭州",
            history=[{"role": "assistant", "content": "以前的回复"}],
        )
        result = await run_turn(request, AgentContext(client, "test", ToolRegistry()))
        self.assertEqual(result.answer, "杭州适合慢游")
        self.assertEqual(result.status, "completed")
        self.assertEqual(
            result.new_messages, [{"role": "assistant", "content": "杭州适合慢游"}]
        )
        self.assertEqual(
            request.history, [{"role": "assistant", "content": "以前的回复"}]
        )

    async def test_compression_does_not_erase_new_messages(self):
        from ollama import ResponseError

        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    ResponseError("context window exceeded", status_code=413),
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="本轮回复", tool_calls=[]
                        )
                    ),
                ]
            )
        )
        history = [{"role": "user", "content": f"旧消息{i}"} for i in range(20)]
        result = await run_turn(
            AgentRequest("不要参考历史，继续", history=history),
            AgentContext(client, "test", ToolRegistry()),
        )
        self.assertEqual(result.new_messages[-1]["content"], "本轮回复")
        self.assertEqual(len(history), 20)
        self.assertEqual(client.chat.await_count, 2)

    async def test_scoped_specialist_can_be_registered_as_an_async_tool(self):
        """验证后续多 Agent 接口：子任务独立 context/预算，父节点无需继承或改写。"""
        child_client = SimpleNamespace(
            chat=AsyncMock(
                return_value=SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant", content="审计通过", tool_calls=[]
                    )
                )
            )
        )

        async def specialist(arguments, tool_context):
            child = await run_turn(
                AgentRequest(arguments["task"]),
                AgentContext(
                    child_client,
                    "specialist",
                    ToolRegistry(),
                    allowed_tools=frozenset(),
                    max_rounds=1,
                    memory_enabled=False,
                ),
            )
            return {"success": child.status == "completed", "answer": child.answer}

        registry = ToolRegistry(
            [
                ToolSpec(
                    "audit_agent",
                    "只读审计",
                    {"type": "object", "required": ["task"]},
                    specialist,
                )
            ]
        )
        responses = [
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "function": {
                                "name": "audit_agent",
                                "arguments": {"task": "检查行程"},
                            }
                        }
                    ],
                )
            ),
            SimpleNamespace(
                message=SimpleNamespace(
                    role="assistant", content="检查完成", tool_calls=[]
                )
            ),
        ]
        parent_client = SimpleNamespace(chat=AsyncMock(side_effect=responses))
        result = await run_turn(
            AgentRequest("检查行程"),
            AgentContext(parent_client, "coordinator", registry, memory_enabled=False),
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_events[0]["result"]["answer"], "审计通过")
        self.assertEqual(child_client.chat.await_args.kwargs["tools"], [])

    async def test_parallel_requests_do_not_share_messages_or_model(self):
        async def execute(text):
            async def chat(**kwargs):
                await asyncio.sleep(0)
                self.assertIn(text, kwargs["messages"][-1]["content"])
                return SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant", content=text, tool_calls=[]
                    )
                )

            return await run_turn(
                AgentRequest(text),
                AgentContext(
                    SimpleNamespace(chat=chat),
                    "test",
                    ToolRegistry(),
                    memory_enabled=False,
                ),
            )

        results = await asyncio.gather(execute("甲的请求"), execute("乙的请求"))
        self.assertEqual([r.answer for r in results], ["甲的请求", "乙的请求"])

    async def test_cancel_after_completed_tool_preserves_events_without_retry(self):
        handler = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("echo", "Echo", {}, handler)])
        first = SimpleNamespace(
            message=SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[{"function": {"name": "echo", "arguments": {}}}],
            )
        )
        client = SimpleNamespace(
            chat=AsyncMock(side_effect=[first, asyncio.CancelledError()])
        )
        with self.assertRaises(AgentCancelledError) as error:
            await run_turn(
                AgentRequest("执行任务"),
                AgentContext(client, "test", registry, memory_enabled=False),
            )
        handler.assert_awaited_once()
        self.assertEqual(
            [e["tool"] for e in error.exception.partial_result.tool_events],
            ["echo", "verify_last_tool_result"],
        )

    async def test_tool_scope_blocks_otherwise_valid_model_call(self):
        forbidden = AsyncMock()
        registry = ToolRegistry([ToolSpec("private", "Private", {}, forbidden)])
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="",
                            tool_calls=[
                                {"function": {"name": "private", "arguments": {}}}
                            ],
                        )
                    ),
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="工具不可用", tool_calls=[]
                        )
                    ),
                ]
            )
        )
        result = await run_turn(
            AgentRequest("执行任务"),
            AgentContext(
                client,
                "test",
                registry,
                allowed_tools=frozenset(),
                memory_enabled=False,
            ),
        )
        self.assertEqual(result.status, "failed")
        forbidden.assert_not_awaited()

    async def test_many_tools_are_verified_in_order(self):
        handler = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("echo", "Echo", {}, handler)])
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="",
                            tool_calls=[
                                {"function": {"name": "echo", "arguments": {"i": i}}}
                                for i in range(14)
                            ],
                        )
                    ),
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="完成", tool_calls=[]
                        )
                    ),
                ]
            )
        )
        result = await run_turn(
            AgentRequest("执行任务"),
            AgentContext(client, "test", registry, memory_enabled=False),
        )
        self.assertEqual(handler.await_count, 14)
        self.assertEqual(
            [e["tool"] for e in result.tool_events],
            ["echo", "verify_last_tool_result"] * 14,
        )

    async def test_forget_policy_does_not_read_or_write_memory(self):
        with (
            patch("service.storage.memory_store.read_preference_profile") as read,
            patch("agent.memory.extract_and_store", new_callable=AsyncMock) as write,
        ):
            client = SimpleNamespace(
                chat=AsyncMock(
                    return_value=SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="好的", tool_calls=[]
                        )
                    )
                )
            )
            await run_turn(
                AgentRequest("不要参考历史，介绍杭州"),
                AgentContext(client, "test", ToolRegistry()),
            )
            read.assert_not_called()
            write.assert_not_awaited()

    async def test_successful_second_item_does_not_erase_first_item_failure(self):
        handler = AsyncMock(
            side_effect=[{"success": False, "message": "conflict"}, {"success": True}]
        )
        registry = ToolRegistry([ToolSpec("add", "Add", {}, handler)])
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="",
                            tool_calls=[
                                {
                                    "function": {
                                        "name": "add",
                                        "arguments": {"title": "A"},
                                    }
                                },
                                {
                                    "function": {
                                        "name": "add",
                                        "arguments": {"title": "B"},
                                    }
                                },
                            ],
                        )
                    ),
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant", content="已添加 B，A 失败", tool_calls=[]
                        )
                    ),
                ]
            )
        )
        result = await run_turn(
            AgentRequest("添加活动"),
            AgentContext(client, "test", registry, memory_enabled=False),
        )
        self.assertEqual(result.status, "failed")

    async def test_plan_identifier_only_reply_is_normalized_before_persistence(self):
        plan = {"plan_id": "trip-1", "destination": "杭州", "itinerary": []}
        registry = ToolRegistry(
            [
                ToolSpec(
                    "get",
                    "Get",
                    {},
                    AsyncMock(return_value={"success": True, "plan": plan}),
                )
            ]
        )
        client = SimpleNamespace(
            chat=AsyncMock(
                side_effect=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content="",
                            tool_calls=[{"function": {"name": "get", "arguments": {}}}],
                        )
                    ),
                    SimpleNamespace(
                        message=SimpleNamespace(
                            role="assistant",
                            content='{"plan_id":"trip-1"}',
                            tool_calls=[],
                        )
                    ),
                ]
            )
        )
        result = await run_turn(
            AgentRequest("查看行程"),
            AgentContext(client, "test", registry, memory_enabled=False),
        )
        self.assertIn("已更新行程", result.answer)
        self.assertEqual(result.new_messages[0]["content"], result.answer)
