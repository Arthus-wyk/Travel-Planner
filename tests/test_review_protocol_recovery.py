"""Keep tool protocol failures and unresolved calls out of summary recovery."""
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from agent.chat.review.children import create_review_handlers
from agent.chat.review.parent import ReviewDelegations
from agent.chat.runtime.execution import ReviewExecution
from agent.chat.schemas.tasks import TaskContext
from agent.tools.schemas.outputs import legacy_result
from service.utils.model_client import OpenAICompatibleClient, StructuredOutputError


class ProtocolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_native_calls_are_protocol_errors_in_both_modes(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                choice = {"finish_reason": "tool_calls", "message": {"content": ""}}
                if streaming:
                    body = 'data: ' + json.dumps({"choices": [{"finish_reason": "tool_calls", "delta": {}}]}) + '\n\ndata: [DONE]\n\n'
                    response = httpx.Response(200, text=body)
                else:
                    response = httpx.Response(200, json={"choices": [choice]})
                client = OpenAICompatibleClient("https://example.test/v1", "test",
                    transport=httpx.MockTransport(lambda _: response))
                with self.assertRaises(StructuredOutputError) as raised:
                    result = await client.chat(model="offline", messages=[], tools=[],
                                               stream=streaming, format={"type": "object"})
                    if streaming:
                        _ = [chunk async for chunk in result]
                self.assertEqual(raised.exception.code, "invalid_tool_response")

    async def test_forbidden_tool_or_protocol_error_preserves_poi_without_artifact(self):
        for failure in ("forbidden", "protocol", "timeout", "summary"):
            with self.subTest(failure=failure):
                def call(name, arguments):
                    return {"finish_reason": "tool_calls", "message": {"tool_calls": [{
                        "id": name, "function": {"name": name, "arguments": json.dumps(arguments)}}]}}
                replies = [call("search_review_places", {"query": "park"})]
                if failure == "forbidden":
                    replies.append(call("search_review_hotels", {}))
                if failure == "timeout":
                    replies.append(call("search_review_places", {"query": "nearby park"}))
                replies.append({"finish_reason": "tool_calls" if failure == "protocol" else "stop",
                                "message": {"content": ""}})
                iterator = iter(replies)
                client = OpenAICompatibleClient("https://example.test/v1", "test", transport=httpx.MockTransport(
                    lambda _: httpx.Response(200, json={"choices": [next(iterator)]})))
                place = {"place_id": "park", "name": "Park"}
                read = AsyncMock(return_value=legacy_result("search_review_places", True, {"places": [place]}))
                if failure == "timeout":
                    read.side_effect = [read.return_value, TimeoutError(), TimeoutError(), TimeoutError()]
                tools = [{"function": {"name": name, "parameters": {"type": "object"}}}
                         for name in ("search_review_places", "search_review_hotels")]
                manager = ReviewDelegations(create_review_handlers(client, "offline", tools, {}, ReviewExecution()),
                                            TaskContext({}, read))
                result = await manager.execute({"task_id": "park", "role": "search_places", "task": "Find park",
                    "search_scope": {"category": "park", "day": 1, "selection_count": 1},
                    "extra_data": {"facts": "", "artifact_refs": []}}, None)
                self.assertEqual(result.data["places"], [place])
                if failure == "summary":
                    self.assertTrue(result.success)
                    self.assertIsNotNone(result.data["artifact_ref"])
                else:
                    self.assertEqual(result.status, "partial")
                    self.assertIsNone(result.data["artifact_ref"])
                    self.assertNotIn("park", manager.results)
                if failure == "timeout":
                    self.assertEqual(read.await_count, 4)
                    self.assertEqual(manager.outcomes()["park"].data["tool_failures"][0]["error_type"],
                                     "UPSTREAM_TIMEOUT")
                else:
                    read.assert_awaited_once()
