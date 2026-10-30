import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from agent.executor import ToolExecutor
from tooling.context import ToolContext
from tooling.mcp import build_registry
from tooling.registry import ToolRegistry
from tooling.spec import ToolSpec
from agent.router import select_tools
from tools.catalog import tool_metadata


class ToolingTests(unittest.IsolatedAsyncioTestCase):
    async def test_arguments_must_be_object_even_for_empty_schema(self):
        handler = AsyncMock()
        registry = ToolRegistry([ToolSpec("echo", "Echo", {}, handler)])
        result = await ToolExecutor(registry).execute(
            "echo", [], ToolContext(), allowed={"echo"}
        )
        self.assertEqual(result.error_type, "validation_error")
        handler.assert_not_awaited()

    def test_unknown_request_defaults_to_read_tools(self):
        definitions = [
            SimpleNamespace(name=n, description=n, inputSchema={})
            for n in ["create_travel_plan", "get_travel_plan", "delete_travel_plan"]
        ]
        registry = build_registry(SimpleNamespace(), definitions, tool_metadata())
        self.assertEqual(select_tools(registry, [], None), {"get_travel_plan"})

    async def test_real_executor_preserves_context_and_validates_schema(self):
        handler = AsyncMock(return_value={"success": True, "value": 7})
        registry = ToolRegistry(
            [
                ToolSpec(
                    "echo",
                    "Echo",
                    {
                        "type": "object",
                        "properties": {"value": {"type": "integer", "minimum": 1}},
                        "required": ["value"],
                    },
                    handler,
                )
            ]
        )
        executor = ToolExecutor(registry)
        context = ToolContext(actor_id="user-1")
        result = await executor.execute("echo", {"value": 7}, context, allowed={"echo"})
        self.assertTrue(result.success)
        handler.assert_awaited_once_with({"value": 7}, context)
        for arguments in ({}, {"value": "7"}, {"value": 0}):
            with self.subTest(arguments=arguments):
                failed = await executor.execute(
                    "echo", arguments, context, allowed={"echo"}
                )
                self.assertFalse(failed.success)
                self.assertEqual(failed.error_type, "validation_error")
        self.assertEqual(handler.await_count, 1)

    async def test_scope_and_permissions_are_checked_before_handler(self):
        handler = AsyncMock()
        registry = ToolRegistry(
            [ToolSpec("private", "Private", {}, handler, required_permission="write")]
        )
        executor = ToolExecutor(registry)
        for allowed, context in [
            ({"private"}, ToolContext()),
            (set(), ToolContext(permissions=frozenset({"write"}))),
        ]:
            result = await executor.execute("private", {}, context, allowed=allowed)
            self.assertFalse(result.success)
        handler.assert_not_awaited()

    async def test_mcp_is_error_overrides_success_payload_and_handler_names_are_bound(
        self,
    ):
        session = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent={"success": True}, isError=True
                )
            )
        )
        definitions = [
            SimpleNamespace(name=n, description=n, inputSchema={"type": "object"})
            for n in ["first", "second"]
        ]
        registry = build_registry(session, definitions, {})
        for name in ["first", "second"]:
            result = await ToolExecutor(registry).execute(
                name, {}, ToolContext(), allowed=registry.names
            )
            self.assertFalse(result.success)
        self.assertEqual(
            [c.args[0] for c in session.call_tool.await_args_list], ["first", "second"]
        )
