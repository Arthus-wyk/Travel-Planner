import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from agent.tools.dispatcher import ToolExecutor
from agent.tools.context import ToolContext
from agent.tools.mcp import build_registry, private_tool_session
from agent.tools.schemas.errors import ErrorCode
from agent.tools.schemas.inputs import ToolInput
from agent.tools.schemas.outputs import failure
from agent.tools.registry import ToolRegistry
from agent.tools.schemas.inputs import ToolSpec
from agent.tools.catalog import tool_metadata


class ToolingTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_honors_rate_limit_and_caps_replay(self):
        limited = failure("read", ErrorCode.RATE_LIMITED, "Wait", retryable=True, retry_after_seconds=0.8)
        handler = AsyncMock(side_effect=[limited, TimeoutError(), {"value": 7}])
        spec = ToolSpec("read", "Read", {}, handler, replay_safe=True, via_mcp=True)
        with patch("agent.tools.dispatcher.asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await ToolExecutor(ToolRegistry([spec])).execute("read", {}, ToolContext(), allowed={"read"})
        self.assertEqual(result.data, {"value": 7})
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [0.8, 1.5])
        handler.reset_mock(side_effect=True)
        handler.return_value = limited
        with patch("agent.tools.dispatcher.asyncio.sleep", new_callable=AsyncMock):
            exhausted = await ToolExecutor(ToolRegistry([spec])).execute("read", {}, ToolContext(), allowed={"read"})
        self.assertEqual(handler.await_count, 3)
        self.assertFalse(exhausted.error.retryable)
        self.assertEqual(exhausted.error.details["recovery"], "RETRY_EXHAUSTED")

    async def test_long_wait_and_unknown_write_result_are_not_replayed(self):
        limited = failure("read", ErrorCode.RATE_LIMITED, "Wait", retryable=True, retry_after_seconds=200)
        for name, safe, response, reason in (
            ("read", True, limited, "DEADLINE_EXCEEDED"),
            ("write", False, TimeoutError(), "REPLAY_UNSAFE"),
        ):
            with self.subTest(tool=name):
                handler = AsyncMock(side_effect=[response])
                spec = ToolSpec(name, name, {}, handler, replay_safe=safe, via_mcp=True)
                result = await ToolExecutor(ToolRegistry([spec])).execute(name, {}, ToolContext(), allowed={name})
                self.assertFalse(result.error.retryable)
                self.assertEqual(result.error.details["recovery"], reason)
                handler.assert_awaited_once()

    async def test_private_mcp_binds_server_permissions_and_identity(self):
        from pydantic import BaseModel

        class Output(BaseModel):
            actor: str

        async def write(arguments, context):
            return {"actor": context.actor_id}

        spec = ToolSpec("write", "Write", ToolInput, write, required_permission="write", output_model=Output)
        allowed = ToolContext(actor_id="owner-a", permissions=frozenset({"write"}))
        denied = ToolContext(actor_id="owner-b")

        async def call(server_context):
            async with private_tool_session(ToolRegistry([spec]), server_context) as registry:
                return await ToolExecutor(registry).execute("write", {}, allowed, allowed={"write"})

        result_a, result_b = await asyncio.gather(call(allowed), call(denied))
        self.assertEqual(result_a.data, {"actor": "owner-a"})
        self.assertEqual(result_b.error.code, ErrorCode.PERMISSION_DENIED)

    async def test_mcp_timeout_cancels_the_matching_server_request(self):
        cancelled = asyncio.Event()
        worker_started = asyncio.Event()

        async def slow(arguments, context):
            worker_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        spec = ToolSpec("slow", "Slow", ToolInput, slow)
        async with private_tool_session(ToolRegistry([spec])) as bound:
            operation = asyncio.create_task(ToolExecutor(bound).execute(
                "slow", {}, ToolContext(), allowed={"slow"}, timeout_seconds=0.5,
            ))
            await asyncio.wait_for(worker_started.wait(), 1)
            result = await asyncio.wait_for(operation, 2)
            await asyncio.wait_for(cancelled.wait(), 1)
        self.assertEqual(result.error.code, ErrorCode.UPSTREAM_TIMEOUT)

    async def test_expired_client_deadline_does_not_restart_a_blocked_sync_read(self):
        from threading import Event
        from unittest.mock import Mock
        from agent.tools.catalog import _serve_sync

        release = Event()
        handler = Mock(side_effect=lambda: release.wait(3))
        spec = ToolSpec("read", "Read", ToolInput, _serve_sync(handler), replay_safe=True)
        try:
            with patch("agent.tools.dispatcher.RETRY_DELAYS", (0, 0)):
                async with private_tool_session(ToolRegistry([spec])) as registry:
                    result = await ToolExecutor(registry).execute("read", {}, ToolContext(),
                                                                  allowed={"read"}, timeout_seconds=1.5)
            handler.assert_called_once()
            self.assertEqual(result.error.code, ErrorCode.UPSTREAM_TIMEOUT)
            self.assertFalse(result.error.retryable)
            self.assertEqual(result.error.details["recovery"], "DEADLINE_EXCEEDED")
        finally:
            release.set()

    def test_typed_tool_envelope_keeps_partial_data(self):
        from pydantic import BaseModel, ValidationError
        from agent.tools.schemas.errors import ErrorCode, ToolError
        from agent.tools.schemas.outputs import ToolResult

        class Routes(BaseModel):
            routes: list[str]

        model = ToolResult[Routes]
        error = ToolError(code=ErrorCode.PARTIAL_FAILURE, message="One route is missing",
                          details={"missing": ["b-c"]})
        result = model(status="partial", data=Routes(routes=["a-b"]), error=error)
        self.assertEqual(result.model_dump(mode="json")["data"], {"routes": ["a-b"]})
        self.assertFalse(result.error.retryable)
        with self.assertRaises(ValidationError):
            model(status="success", error=error)
        with self.assertRaises(ValidationError):
            model(status="error", error=error)

    async def test_arguments_must_be_object_even_for_empty_schema(self):
        handler = AsyncMock()
        registry = ToolRegistry([ToolSpec("echo", "Echo", {}, handler)])
        result = await ToolExecutor(registry).execute(
            "echo", [], ToolContext(), allowed={"echo"}
        )
        self.assertEqual(result.error_type, "validation_error")
        handler.assert_not_awaited()

    def test_registry_preserves_tools_for_runtime_scope(self):
        definitions = [
            SimpleNamespace(name=n, description=n, inputSchema={})
            for n in ["create_travel_plan", "get_travel_plan", "delete_travel_plan"]
        ]
        registry = build_registry(SimpleNamespace(), definitions, tool_metadata())
        self.assertEqual(registry.names, {"create_travel_plan", "get_travel_plan", "delete_travel_plan"})

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
