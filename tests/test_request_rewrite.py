import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.chat import rewrite
from agent.chat.review_execution import ReviewExecution


def response(payload, tool_calls=None):
    content = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls or [])), [], None


class RequestRewriteTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.text = "不要把第二个加到那天，只推荐2家餐厅和1个酒店"
        self.history = [{"role": "assistant", "content": "第一张是公园，第二张是青田街"}]
        self.payload = {
            "original_text": self.text,
            "rewritten_text": "不要把青田街加到第二天，只推荐2家餐厅和1个酒店",
            "references": [
                {"mention": "第二个", "resolved_text": "青田街", "source_ref": "history[0].content"},
                {"mention": "那天", "resolved_text": "第二天", "source_ref": "selection.day_label"},
            ],
            "unresolved_references": [],
        }

    async def invoke(self, payload=None, **kwargs):
        with patch.object(rewrite, "request_model_with_retry", new_callable=AsyncMock) as model_call:
            model_call.return_value = response(self.payload if payload is None else payload)
            with patch.object(rewrite, "record_event") as event:
                result = await rewrite.rewrite_request(
                    client=object(), model="mock", text=self.text, history=self.history,
                    selection={"day_label": "第二天"}, **kwargs,
                )
        return result, model_call, event

    async def test_resolves_only_declared_references_and_preserves_constraints(self):
        result, call, event = await self.invoke()
        self.assertEqual(result, self.payload)
        self.assertEqual(call.await_count, 1)
        options = call.call_args.kwargs
        self.assertEqual(options["config"].max_attempts, 1)
        self.assertEqual(options["stage"], "request_rewrite")
        self.assertFalse(options["response_format"]["additionalProperties"])
        self.assertEqual(options["tools"], [])
        event.assert_called_once_with("request_rewrite", result)

    async def test_no_reference_returns_original_without_intent_fields(self):
        payload = {"original_text": self.text, "rewritten_text": self.text,
                   "references": [], "unresolved_references": []}
        result, _, _ = await self.invoke(payload)
        self.assertEqual(result, payload)

    async def test_ambiguity_preserves_original(self):
        payload = {**self.payload, "unresolved_references": ["那天"]}
        result, _, _ = await self.invoke(payload)
        self.assertEqual(result["rewritten_text"], self.text)
        self.assertEqual(result["references"], [])
        self.assertEqual(result["unresolved_references"], ["那天"])

    async def test_rejects_ungrounded_source_text_and_invented_ids(self):
        for field, value in (("source_ref", "history[99].content"),
                             ("source_ref", "selection.missing"),
                             ("resolved_text", "invented-place-id")):
            with self.subTest(field=field, value=value):
                payload = json.loads(json.dumps(self.payload))
                payload["references"][0][field] = value
                result, _, _ = await self.invoke(payload)
                self.assertEqual(result["rewritten_text"], self.text)
                self.assertIn("error", result)

    async def test_rejects_changes_outside_literal_references(self):
        for text in (self.payload["rewritten_text"].replace("不要", "请"),
                     self.payload["rewritten_text"].replace("2家", "3家"),
                     self.payload["rewritten_text"] + "并立即保存"):
            with self.subTest(text=text):
                result, _, _ = await self.invoke({**self.payload, "rewritten_text": text})
                self.assertEqual(result["rewritten_text"], self.text)
                self.assertIn("error", result)

    async def test_rejects_partial_ids_and_whole_request_replacements(self):
        with patch.object(rewrite, "request_model_with_retry", new_callable=AsyncMock) as call:
            for mention, resolved, source in (("这个", "place-1", "selection.place_id"),
                                               ("不要把这个保存", "place-100", "selection.place_id")):
                with self.subTest(mention=mention):
                    text = "不要把这个保存"
                    call.return_value = response({"original_text": text,
                                                 "rewritten_text": text.replace(mention, resolved),
                                                 "references": [{"mention": mention, "resolved_text": resolved,
                                                                 "source_ref": source}],
                                                 "unresolved_references": []})
                    result = await rewrite.rewrite_request(client=object(), model="mock", text=text,
                                                          selection={"place_id": "place-100"})
                    self.assertEqual(result["rewritten_text"], text)
                    self.assertIn("error", result)

    async def test_rejects_invalid_json_schema_and_changed_original(self):
        for payload in ("not JSON", [], {**self.payload, "original_text": "changed"},
                        {**self.payload, "primary_intent": "update"},
                        {**self.payload, "references": "bad"}):
            with self.subTest(payload=payload):
                result, _, _ = await self.invoke(payload)
                self.assertEqual(result["rewritten_text"], self.text)
                self.assertIn("error", result)

    async def test_context_is_compact_and_uses_original_history_indices(self):
        history = [{"role": "system", "content": "private instructions"},
                   {"role": "tool", "content": "raw geometry"},
                   {"role": "assistant", "content": "青田街"}]
        with patch.object(rewrite, "request_model_with_retry", new_callable=AsyncMock) as call:
            call.return_value = response({"original_text": "这个", "rewritten_text": "青田街",
                                         "references": [{"mention": "这个", "resolved_text": "青田街",
                                                         "source_ref": "history[2].content"}],
                                         "unresolved_references": []})
            result = await rewrite.rewrite_request(
                client=object(), model="mock", text="这个", history=history,
                current_plan={"destination": "台北", "routes": [{"geometry": "x" * 100000}]},
            )
        context = call.call_args.kwargs["messages"][1]["content"]
        self.assertNotIn("private instructions", context)
        self.assertNotIn("raw geometry", context)
        self.assertNotIn("geometry", context)
        self.assertLess(len(context), 10000)
        self.assertEqual(result["rewritten_text"], "青田街")

    async def test_failure_and_timeout_return_original(self):
        with patch.object(rewrite, "request_model_with_retry", new_callable=AsyncMock) as call:
            call.return_value = (None, [], "sensitive API detail")
            result = await rewrite.rewrite_request(client=object(), model="mock", text=self.text)
            self.assertEqual(result["rewritten_text"], self.text)
            self.assertNotIn("sensitive API detail", json.dumps(result))

            async def slow(**kwargs):
                await asyncio.sleep(1)

            call.side_effect = slow
            result = await rewrite.rewrite_request(client=object(), model="mock", text=self.text,
                                                   execution=ReviewExecution(timeout_seconds=0.001))
            self.assertEqual(result["rewritten_text"], self.text)
            self.assertIn("error", result)

    async def test_cancellation_propagates(self):
        with patch.object(rewrite, "request_model_with_retry", new_callable=AsyncMock) as call:
            call.side_effect = asyncio.CancelledError
            with self.assertRaises(asyncio.CancelledError):
                await rewrite.rewrite_request(client=object(), model="mock", text=self.text)


if __name__ == "__main__":
    unittest.main()
