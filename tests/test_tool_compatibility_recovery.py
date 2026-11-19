import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from mcp import types

from agent.chat.runtime.context import AgentContext
from agent.chat.runtime.nodes import execute_tool
from agent.chat.schemas.state import AgentSession
from agent.tools.context import ToolContext
from agent.tools.dispatcher import ToolExecutor
from agent.tools.implementations.review import _best_place_insertion
from agent.tools.mcp import build_registry, private_tool_session
from agent.tools.registry import ToolRegistry
from agent.tools.schemas.errors import ErrorCode
from agent.tools.schemas.inputs import ToolSpec
from infrastructure.mcp import connect_tools


class ToolCompatibilityRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_anchor_can_be_corrected_in_same_turn(self):
        plan = {"itinerary": [{"item_id": "anchor", "day": 1}]}
        def preview(args, context):
            outcome, _ = _best_place_insertion(plan, {"operations": [{"action": "review_place",
                "payload": {"item_id": args["item_id"], "poi_id": "park"}}]}, "park")
            return outcome
        registry = ToolRegistry([ToolSpec("preview_review_changes", "Preview", {"type": "object"}, preview)])
        async with private_tool_session(registry) as bound:
            context = AgentContext(None, "offline", bound, session=AgentSession())
            state = {"calls": [{"id": "first", "name": "preview_review_changes",
                                "arguments": {"item_id": "missing"}}],
                     "messages": [], "tool_events": [], "active_tools": ["preview_review_changes"]}
            result = await execute_tool(state, SimpleNamespace(context=context))
            self.assertEqual(result["tool_events"][0]["error"]["code"], "INVALID_ARGUMENT")
            self.assertNotIn("recovery", result["tool_events"][0]["error"]["details"])
            state.update(result)
            state["calls"] = [{"id": "second", "name": "preview_review_changes",
                               "arguments": {"item_id": "anchor"},
                               "retry_of": context.session.failed_calls[0]["operation_id"]}]
            with patch("agent.tools.implementations.review.preview_review_action", return_value={"plan": {"routes": []}}), patch(
                "agent.tools.implementations.review.review_acceptance", return_value={"accepted": True}
            ):
                result = await execute_tool(state, SimpleNamespace(context=context))
            self.assertEqual(result["tool_events"][-1]["status"], "success")
            self.assertFalse(context.session.failed_calls)

    async def test_external_text_json_and_errors(self):
        for text, is_error, expected in (("pong", False, "pong"), ('{"value": 1}', False, {"value": 1}),
                                         ("denied", True, None)):
            with self.subTest(text=text):
                session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(
                    content=[types.TextContent(type="text", text=text)], isError=is_error)))
                registry = build_registry(session, [types.Tool(name="external", inputSchema={})], {})
                result = await ToolExecutor(registry).execute("external", {}, ToolContext(), allowed={"external"})
                self.assertEqual(result.data, expected)
                self.assertEqual(result.status, "error" if is_error else "success")
                if is_error:
                    self.assertEqual(result.error.message, "denied")

    async def test_same_named_external_server_uses_discovered_tools(self):
        session = SimpleNamespace(initialize=AsyncMock(), list_tools=AsyncMock(return_value=SimpleNamespace(
            tools=[types.Tool(name="external", inputSchema={})])))
        @asynccontextmanager
        async def transport(*args):
            yield (None, None)
        @asynccontextmanager
        async def client(*args):
            yield session
        with TemporaryDirectory() as folder:
            server = Path(folder) / "mcp_server.py"
            server.touch()
            with patch("infrastructure.mcp.stdio_client", transport), patch("infrastructure.mcp.ToolClientSession", client):
                async with connect_tools(server) as registry:
                    self.assertEqual(registry.names, {"external"})

    async def test_invalid_claimed_envelope_and_error_flag_are_rejected(self):
        for content, is_error in (({"status": "success"}, False),
                                  ({"status": "success", "data": {}, "error": None}, True)):
            session = SimpleNamespace(call_tool=AsyncMock(return_value=types.CallToolResult(
                content=[], structuredContent=content, isError=is_error)))
            registry = build_registry(session, [types.Tool(name="external", inputSchema={})], {})
            result = await ToolExecutor(registry).execute("external", {}, ToolContext(), allowed={"external"})
            self.assertEqual(result.error.code, ErrorCode.INTERNAL_ERROR)
