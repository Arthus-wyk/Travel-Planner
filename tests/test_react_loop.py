import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent.chat.context import AgentContext
from agent.chat.runner import run_turn
from agent.chat.state import AgentCancelledError, AgentRequest, AgentSession
from tooling.registry import ToolRegistry
from tooling.spec import ToolSpec


def reply(text="", calls=()):
    return SimpleNamespace(message=SimpleNamespace(role="assistant", content=text, tool_calls=list(calls)))


def call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


class ReactLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegation_has_no_parent_total_step_timeout(self):
        async def child(arguments, context):
            await asyncio.sleep(0.03)
            return {"success": True, "evidence": "verified child result"}

        registry = ToolRegistry([ToolSpec("delegate_task", "delegate", {}, child)])
        client = SimpleNamespace(chat=AsyncMock(side_effect=[
            reply(calls=[call("delegate_task", {})]), reply("done")]))
        result = await run_turn(AgentRequest("delegate"), AgentContext(client, "mock", registry,
            memory_enabled=False, enable_rewrite=False, timeout_seconds=0.01))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.evidence[0]["result"]["evidence"], "verified child result")

    async def test_timeout_keeps_verified_plan_and_evidence(self):
        handler = AsyncMock(return_value={"success": True, "plan": {"plan_id": "saved"}})
        registry = ToolRegistry([ToolSpec("read", "read", {}, handler)])
        count = 0

        async def chat(**kwargs):
            nonlocal count
            count += 1
            if count == 1:
                return reply(calls=[call("read", {})])
            await asyncio.sleep(1)

        result = await run_turn(AgentRequest("read"), AgentContext(SimpleNamespace(chat=chat), "mock", registry,
            memory_enabled=False, enable_rewrite=False, timeout_seconds=0.01))
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.plan, {"plan_id": "saved"})
        self.assertEqual(len(result.evidence), 1)

    async def test_cancellation_keeps_completed_call_evidence(self):
        async def cancel(arguments, context):
            raise asyncio.CancelledError

        handler = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("read", "read", {}, handler), ToolSpec("cancel", "cancel", {}, cancel)])
        client = SimpleNamespace(chat=AsyncMock(return_value=reply(calls=[call("read", {}), call("cancel", {})])))
        session = AgentSession()
        with self.assertRaises(AgentCancelledError) as raised:
            await run_turn(AgentRequest("read"), AgentContext(client, "mock", registry,
                memory_enabled=False, enable_rewrite=False, session=session))
        self.assertEqual(len(raised.exception.partial_result.evidence), 1)
        tools = [message for message in session.messages if message["role"] == "tool"]
        self.assertEqual(len(tools), 2)

    async def test_failed_serial_call_skips_rest_but_counts_generated_calls(self):
        first = AsyncMock(return_value={"success": False, "message": "bad"})
        second = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("first", "first", {}, first), ToolSpec("second", "second", {}, second)])
        client = SimpleNamespace(chat=AsyncMock(side_effect=[
            reply(calls=[call("first", {}), call("second", {})]), reply("部分失败")]))
        session = AgentSession()
        result = await run_turn(AgentRequest("work"), AgentContext(client, "mock", registry,
            memory_enabled=False, enable_rewrite=False, session=session))
        second.assert_not_awaited()
        self.assertEqual(session.budget.remaining, 6)
        self.assertEqual(result.status, "failed")
        messages = [json.loads(m["content"]) for m in session.messages if m["role"] == "tool"]
        self.assertEqual(messages[-1]["error_type"], "decision_required")

    async def test_same_task_does_not_duplicate_initial_messages(self):
        client = SimpleNamespace(chat=AsyncMock(return_value=reply("done")))
        session = AgentSession()
        context = AgentContext(client, "mock", ToolRegistry([]), memory_enabled=False, enable_rewrite=False,
                               session=session, system_prompt="child", role="child", task_id="task-1")
        await run_turn(AgentRequest("same", extra_data={"day": 1}), context)
        await run_turn(AgentRequest("same", extra_data={"day": 1}), context)
        self.assertEqual(sum(m["role"] == "system" and m["content"] == "child" for m in session.messages), 1)
        self.assertEqual(sum(m["role"] == "user" for m in session.messages), 1)
        await run_turn(AgentRequest("changed", extra_data={"day": 2}), context)
        self.assertEqual(sum(m["role"] == "user" for m in session.messages), 2)

    async def test_budget_exhaustion_removes_tools_for_final_answer(self):
        handler = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("read", "read", {}, handler)])
        client = SimpleNamespace(chat=AsyncMock(side_effect=[reply(calls=[call("read", {})]), reply("done")]))
        await run_turn(AgentRequest("read"), AgentContext(client, "mock", registry,
            memory_enabled=False, enable_rewrite=False, session=AgentSession(tool_limit=1)))
        self.assertEqual(client.chat.call_args_list[-1].kwargs["tools"], [])

    async def test_role_prompt_task_data_and_private_history(self):
        seen = []
        handler = AsyncMock(return_value={"success": True, "value": 4})
        registry = ToolRegistry([ToolSpec("read", "read", {"type": "object"}, handler)])

        async def chat(**kwargs):
            seen.append(kwargs["messages"])
            return reply(calls=[call("read", {})]) if len(seen) == 1 else reply("done")

        session = AgentSession()
        context = AgentContext(SimpleNamespace(chat=chat), "mock", registry, memory_enabled=False, enable_rewrite=False,
                               system_prompt="child prompt", role="audit_route", session=session)
        result = await run_turn(AgentRequest("check day 1", extra_data={"day": 1}), context)
        self.assertEqual(result.status, "completed")
        self.assertEqual(seen[0][0]["content"], "child prompt")
        task = json.loads(seen[0][1]["content"])
        self.assertEqual(task["task"], "check day 1")
        self.assertEqual(task["extra_data"], {"day": 1})
        self.assertTrue(any(m["role"] == "tool" for m in session.messages))
        self.assertEqual(session.budget.remaining, 7)
        other = AgentSession()
        self.assertFalse(other.messages)

    async def test_native_calls_only_and_scope_does_not_depend_on_keywords(self):
        handler = AsyncMock(return_value={"success": True})
        registry = ToolRegistry([ToolSpec("read", "read", {"type": "object"}, handler)])
        client = SimpleNamespace(chat=AsyncMock(return_value=reply('```json\n{"name":"read","arguments":{}}\n```')))
        await run_turn(AgentRequest("anything"), AgentContext(client, "mock", registry, memory_enabled=False, enable_rewrite=False))
        handler.assert_not_awaited()
        self.assertEqual({t["function"]["name"] for t in client.chat.call_args.kwargs["tools"]}, {"read"})

    async def test_one_normal_call_and_three_free_retries(self):
        handler = AsyncMock(side_effect=[{"success": False, "message": "bad", "error_type": "validation_error"}] * 3 + [{"success": True}])
        registry = ToolRegistry([ToolSpec("read", "read", {"type": "object"}, handler)])
        count = 0

        async def chat(**kwargs):
            nonlocal count
            count += 1
            if count > 4:
                return reply("done")
            args = {"value": count}
            if count > 1:
                args["_retry_of"] = "op-1"
            return reply(calls=[call("read", args)])

        session = AgentSession(tool_limit=1)
        result = await run_turn(AgentRequest("read"), AgentContext(SimpleNamespace(chat=chat), "mock", registry,
            memory_enabled=False, enable_rewrite=False, session=session))
        self.assertEqual(result.status, "completed")
        self.assertEqual(handler.await_count, 4)
        self.assertEqual(session.budget.remaining, 0)

    async def test_partial_batch_is_rejected_before_any_tool_executes(self):
        handler = AsyncMock()
        registry = ToolRegistry([ToolSpec("read", "read", {}, handler)])
        client = SimpleNamespace(chat=AsyncMock(return_value=reply(calls=[call("read", {}), call("read", '{"bad":') ])))
        result = await run_turn(AgentRequest("read"), AgentContext(client, "mock", registry,
            memory_enabled=False, enable_rewrite=False, max_rounds=1))
        handler.assert_not_awaited()
        self.assertNotEqual(result.status, "completed")

