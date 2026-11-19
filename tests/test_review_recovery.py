import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agent.chat.review.parent import ReviewDelegations, run_parent
from agent.chat.review.children import create_review_handlers
from agent.chat.runtime.execution import ReviewExecution
from agent.chat.schemas.tasks import TaskContext, TaskResult
from service.utils.model_client import OpenAICompatibleClient, StructuredOutputError
from agent.tools.implementations.delegation import delegation_registry
from agent.tools.dispatcher import ToolExecutor
from agent.tools.context import ToolContext
from agent.tools.schemas.outputs import ToolResult, legacy_result


def delegate(task_id, *, refs=(), scope=None):
    args = {"task_id": task_id, "role": "search_places", "task": task_id,
            "extra_data": {"facts": "", "artifact_refs": list(refs)}}
    if scope is not None:
        args["search_scope"] = scope
    return args


class ReviewRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_repairs_json_and_sends_output_budget(self):
        contents = ['{"value":"好" "count":2}', '```json\n{"value":"好","count":2}\n```',
                    '{"value":"好"地点"","count":2}']
        schema = {"type": "object", "required": ["value", "count"],
                  "properties": {"value": {"type": "string"}, "count": {"type": "integer"}}}
        for content in contents:
            with self.subTest(content=content):
                def handle(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload["max_tokens"], 8192)
                    self.assertEqual(payload["response_format"]["type"], "json_schema")
                    return httpx.Response(200, json={"choices": [
                        {"finish_reason": "stop", "message": {"content": content}}]})

                client = OpenAICompatibleClient("https://example.com/v1", "key",
                                                transport=httpx.MockTransport(handle))
                result = await client.chat(model="demo", messages=[], tools=[], format=schema,
                                           options={"num_predict": 8192})
                expected = '好"地点"' if "地点" in content else "好"
                self.assertEqual(json.loads(result.message.content), {"value": expected, "count": 2})

    async def test_gateway_distinguishes_empty_truncated_and_schema_errors(self):
        schema = {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}}
        for content, finish, error in (("  ", "stop", "empty_output"),
                                       ('{"value":"half', "length", "truncated_output"),
                                       ('{"value":4}', "stop", "invalid_schema"),
                                       ("[]", "stop", "invalid_schema")):
            with self.subTest(error=error):
                def handle(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertNotIn("max_tokens", payload)
                    return httpx.Response(200, json={"choices": [
                        {"finish_reason": finish, "message": {"content": content}}]})

                client = OpenAICompatibleClient("https://example.com/v1", "key",
                                                transport=httpx.MockTransport(handle), json_object_only=True)
                with self.assertRaises(StructuredOutputError) as raised:
                    await client.chat(model="demo", messages=[], tools=[], format=schema)
                self.assertEqual(raised.exception.code, error)

    async def test_structured_stream_validates_before_exposing_repaired_text(self):
        events = [
            {"choices": [{"delta": {"content": '{"value":1 "other":'}}]},
            {"choices": [{"finish_reason": "stop", "delta": {"content": "2}"}}]},
        ]
        body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        client = OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=body)))
        schema = {"type": "object", "required": ["value", "other"]}
        stream = await client.chat(model="demo", messages=[], tools=[], stream=True, format=schema)
        chunks = [chunk async for chunk in stream]
        self.assertEqual(len(chunks), 1)
        self.assertEqual(json.loads(chunks[0].message.content), {"value": 1, "other": 2})

    async def test_snapshot_is_accepted_but_failure_never_publishes_artifact(self):
        handler = AsyncMock(return_value=TaskResult("failed", reason="搜索失败"))
        delegations = ReviewDelegations({"search_places": handler},
                                        TaskContext({"plan_version": "plan:current"}, AsyncMock()))
        result = await delegations.execute(delegate("first", refs=["plan:current"]), None)
        self.assertFalse(result.success)
        self.assertIsNone(result.data)
        self.assertIsNone(result.error.details["artifact_ref"])
        self.assertNotIn("first", delegations.results)
        self.assertEqual(delegations.progress()["failures"][0]["task"], "first")
        handler.assert_awaited_once()
        stale = await delegations.execute(delegate("second", refs=["first:v1"]), None)
        self.assertEqual(stale.error_type, "stale_artifact")
        self.assertIsNone(stale.data)
        self.assertIsNone(stale.error.details["artifact_ref"])
        handler.assert_awaited_once()

    async def test_search_scope_is_required_and_category_is_single(self):
        handler = AsyncMock()
        executor = ToolExecutor(delegation_registry(handler, ["search_places"]))
        scopes = [None, {"category": ["park", "food"], "day": 1, "selection_count": 1}]
        for scope in scopes:
            result = await executor.execute("delegate_task", delegate("search", scope=scope),
                                            ToolContext(), allowed={"delegate_task"})
            self.assertEqual(result.error_type, "validation_error")
        handler.assert_not_awaited()

    async def test_bad_final_summary_recovers_only_current_tool_places(self):
        place = {"place_id": "park-1", "name": "大安森林公园"}
        read = AsyncMock(return_value=legacy_result("search_review_places", True, {"places": [place, place]}))
        replies = iter([
            {"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "call-park", "function": {
                "name": "search_review_places", "arguments": '{"query":"公园"}',
            }}]}},
            {"finish_reason": "stop", "message": {"content": "not JSON"}},
            {"finish_reason": "length", "message": {"content": '{"status":"completed"'}},
        ])

        def handle(request):
            payload = json.loads(request.content)
            self.assertEqual(payload["max_tokens"], 8192)
            return httpx.Response(200, json={"choices": [next(replies)]})

        client = OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(handle))
        tools = [{"function": {"name": "search_review_places", "parameters": {"type": "object"}}}]
        handlers = create_review_handlers(client, "demo", tools, {}, ReviewExecution())
        delegations = ReviewDelegations(handlers, TaskContext({}, read))
        with patch("agent.chat.review.children.REVIEW_CHILD_MAX_TOKENS", 8192):
            scope = {"category": "park", "day": 1, "selection_count": 1}
            result = await delegations.execute(delegate("parks", scope=scope), None)
            self.assertTrue(result.success)
            self.assertEqual(result.data["artifact_ref"], delegations._reference("parks"))
            self.assertEqual(result.data["places"], [place])
            self.assertEqual(delegations.results["parks"].data["place_ids"], ["park-1"])
            revised = await delegations.execute(delegate("parks", scope={**scope, "day": 2}), None)
        self.assertFalse(revised.success)
        self.assertIsNone(revised.data)
        self.assertIsNone(revised.error.details["artifact_ref"])
        self.assertEqual(revised.error.details["places"], [])
        read.assert_awaited_once()

    async def test_independent_searches_share_batch_and_failure_reply_is_compact(self):
        entered = set()
        all_started = asyncio.Event()

        async def handler(task, dependencies, context):
            scope = task.input["search_scope"]
            entered.add((scope["category"], scope["selection_count"]))
            if len(entered) == 3:
                all_started.set()
            await asyncio.wait_for(all_started.wait(), timeout=2)
            if scope["category"] == "food":
                return TaskResult("failed", reason="暂时没有餐厅结果")
            return TaskResult("completed", {"places": [{"place_id": task.id}]})

        requests = []
        categories = {"park": 1, "attraction": 2, "food": 4}
        calls = [{"id": f"call-{category}", "function": {"name": "delegate_task", "arguments": delegate(
            f"search-{category}", scope={"category": category, "day": 1, "selection_count": count},
        )}} for category, count in categories.items()]

        async def model(**kwargs):
            requests.append(kwargs["messages"])
            if len(requests) == 1:
                return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=calls)), kwargs["messages"], None
            groups = [{"id": category, "label": category, "place_ids": [f"search-{category}"],
                       "selection_count": categories[category]} for category in ("park", "attraction")]
            content = json.dumps({"kind": "answer", "message": "部分候选", "question": None,
                                  "options": [], "candidate_groups": groups})
            return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[])), kwargs["messages"], None

        original = "重构第一天"
        delegations = ReviewDelegations({"search_places": handler}, TaskContext({}, AsyncMock()))
        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            result = await run_parent(client=None, model="demo", snapshot={}, delegations=delegations,
                                      request_context={"original_text": original, "rewrite": {
                                          "original_text": original, "rewritten_text": original,
                                          "error": {"type": "ValueError"}}}, max_steps=4, execution=ReviewExecution())
        self.assertEqual(entered, set(categories.items()))
        self.assertEqual(result.kind, "partial")
        self.assertCountEqual([place["place_id"] for place in result.places], ["search-park", "search-attraction"])
        user_content = next(message["content"] for message in requests[0] if message["role"] == "user")
        self.assertEqual(user_content.count(original), 1)
        self.assertNotIn("ValueError", user_content)
        replies = {message["tool_call_id"]: json.loads(message["content"])
                   for message in requests[1] if message["role"] == "tool"}
        self.assertEqual(set(replies), {"call-park", "call-attraction", "call-food"})
        self.assertNotIn("verification", replies["call-park"])
        failure = replies["call-food"]
        self.assertFalse(failure["error"]["retryable"])
        self.assertIsNone(failure["data"])
        self.assertIsNone(failure["error"]["details"]["artifact_ref"])
        self.assertEqual(json.dumps(failure, ensure_ascii=False).count("暂时没有餐厅结果"), 1)
