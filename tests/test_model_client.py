import json
import os
import unittest
from unittest.mock import patch

import httpx

from service.utils import model_client
from infrastructure.llm import request_model_with_retry, RetryConfig
from infrastructure.llm import is_network_error, is_prompt_too_long_error


class ModelClientTests(unittest.IsolatedAsyncioTestCase):
    def test_httpx_network_errors_are_retryable(self):
        self.assertTrue(is_network_error(httpx.ConnectError("All connection attempts failed")))
        self.assertTrue(is_network_error(httpx.ReadTimeout("")))

    def test_cli_model_selection_ignores_environment(self):
        from service import cli

        model = "Qwen/Qwen3.5-122B-A10B"
        with patch.dict(os.environ, {"LLM_MODEL": "legacy", "SILICONFLOW_MODEL": "legacy"}):
            with patch("sys.argv", ["program", "--model", model]):
                self.assertEqual(cli.parse_args().model, model)
            self.assertEqual(model_client.get_model(), "deepseek-ai/DeepSeek-V4.1-Flash")

    def test_planning_graph_propagates_model_to_nodes(self):
        from agent.planning import planning_graph

        graph = planning_graph.StateGraph(dict)
        graph.add_node("read_model", lambda _: {"model": model_client.get_model()})
        graph.add_edge(planning_graph.START, "read_model")
        graph.add_edge("read_model", planning_graph.END)
        default, chosen = model_client.AVAILABLE_MODELS[:2]
        with patch.object(planning_graph, "GRAPH", graph.compile()):
            for progress in (None, lambda *_: None):
                result = planning_graph.run_planning_action({}, "generate_from_guide", {"model": chosen}, progress)
                self.assertEqual(result["model"], chosen)
                self.assertEqual(model_client.get_model(), default)

    def test_model_catalog_and_generation_request(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from service.api import server

        client = TestClient(server.create_app(Path("mcp_server.py")))
        models = model_client.AVAILABLE_MODELS
        self.assertEqual(client.get("/api/models").json(), {"models": list(models), "default": models[0]})
        url = "/api/plans/plan/generation"
        with patch.object(server.planning_jobs, "create_job", return_value=("job", None)) as create_job:
            invalid = client.post(url, json={"text": "guide", "model": "unknown"})
            self.assertEqual(invalid.status_code, 400)
            create_job.assert_not_called()
            payload = {"text": "guide", "model": models[1]}
            self.assertEqual(client.post(url, json=payload).json()["job_id"], "job")
            create_job.assert_called_once_with("plan", payload, retry=False)

    async def test_context_error_keeps_provider_message(self):
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(400, json={"error": {"message": "maximum context length exceeded"}})))
        try:
            await client.chat(model="demo", messages=[], tools=[])
        except httpx.HTTPStatusError as exc:
            self.assertTrue(is_prompt_too_long_error(exc))
        else:
            self.fail("Expected HTTP error")

    def test_parallel_tool_replies_precede_verification(self):
        calls = [{"function": {"name": n, "arguments": {}}} for n in ("one", "two")]
        messages = [{"role": "assistant", "tool_calls": calls}, {"role": "tool", "tool_name": "one", "content": "1"}, {"role": "tool", "tool_name": "verify_last_tool_result", "content": "ok"}, {"role": "tool", "tool_name": "two", "content": "2"}]
        converted = model_client.openai_messages(messages)
        self.assertEqual([m["role"] for m in converted], ["assistant", "tool", "tool", "user"])
        self.assertEqual([c["id"] for c in converted[0]["tool_calls"]], [m["tool_call_id"] for m in converted[1:3]])

    def test_same_name_parallel_replies_preserve_native_call_ids(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "first", "function": {"name": "lookup", "arguments": {"city": "杭州"}}},
                {"id": "second", "function": {"name": "lookup", "arguments": {"city": "上海"}}},
            ]},
            {"role": "tool", "tool_name": "lookup", "tool_call_id": "second", "content": "Shanghai result"},
            {"role": "tool", "tool_name": "lookup", "tool_call_id": "first", "content": "Hangzhou result"},
        ]
        converted = model_client.openai_messages(messages)
        self.assertEqual([call["id"] for call in converted[0]["tool_calls"]], ["first", "second"])
        self.assertEqual({reply["content"]: reply["tool_call_id"] for reply in converted[1:]},
                         {"Shanghai result": "second", "Hangzhou result": "first"})

    async def test_truncated_stream_fails_instead_of_executing_partial_calls(self):
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')))
        stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
        with self.assertRaisesRegex(ConnectionError, "ended"):
            async for _ in stream:
                pass

    def test_configuration_requires_explicit_values(self):
        values = {"LLM_PROVIDER": "openai", "LLM_BASE_URL": "https://example.com/v1", "LLM_API_KEY": "key", "AGENT_MODEL": "legacy", "OLLAMA_HOST": "https://legacy.example"}
        with patch.dict(os.environ, values):
            self.assertIsInstance(model_client.create_model_client(), model_client.OpenAICompatibleClient)
            for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY"):
                with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                    with self.assertRaisesRegex(ValueError, name):
                        model_client.create_model_client()
            with patch.dict(os.environ, {"LLM_PROVIDER": "unknown"}):
                with self.assertRaisesRegex(ValueError, "LLM_PROVIDER"):
                    model_client.create_model_client()

    def test_explicit_ollama_uses_only_llm_values(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "LLM_BASE_URL": "http://localhost:11434", "LLM_API_KEY": "", "OLLAMA_API_KEY": "ignored"}):
            client = model_client.create_model_client()
            self.assertNotIn("authorization", client._client.headers)
            self.assertEqual(str(client._client.base_url), "http://localhost:11434")

    async def test_siliconflow_uses_its_own_settings(self):
        def handler(request):
            self.assertEqual(str(request.url), "https://api.siliconflow.cn/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer siliconflow-test-key")
            self.assertEqual(json.loads(request.content)["model"], model_client.AVAILABLE_MODELS[0])
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        values = {"LLM_PROVIDER": "siliconflow",
                  "SILICONFLOW_API_KEY": "siliconflow-test-key", "SILICONFLOW_BASE_URL": "",
                  "LLM_MODEL": "ollama-model", "LLM_BASE_URL": "https://ollama.example", "LLM_API_KEY": "ollama-key"}
        with patch.dict(os.environ, values):
            client = model_client.create_model_client()
            client.transport = httpx.MockTransport(handler)
            result = await client.chat(model=model_client.get_model(), messages=[], tools=[])
            self.assertEqual(result.message.content, "ok")
            with patch.dict(os.environ, {"SILICONFLOW_API_KEY": ""}):
                with self.assertRaisesRegex(ValueError, "SILICONFLOW_API_KEY"):
                    model_client.create_model_client()

    async def test_tool_history_json_usage_and_retry(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            if len(requests) == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(handler))
        messages = [
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lookup", "arguments": {"city": "杭州"}}}]},
            {"role": "tool", "tool_name": "lookup", "content": "ok"},
            {"role": "tool", "tool_name": "verify_last_tool_result", "content": "passed"},
        ]
        result, _, error = await request_model_with_retry(ollama=client, model="demo", messages=messages, tools=[], response_format="json", config=RetryConfig(max_attempts=2, base_delay_seconds=0, jitter_seconds=0))
        self.assertIsNone(error)
        self.assertEqual(result.usage["totalTokens"], 7)
        sent = requests[-1]
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertNotIn("tools", sent)
        self.assertEqual(sent["messages"][1]["tool_call_id"], sent["messages"][0]["tool_calls"][0]["id"])
        self.assertEqual(sent["messages"][2]["role"], "user")
        self.assertIsInstance(sent["messages"][0]["tool_calls"][0]["function"]["arguments"], str)
        self.assertEqual(messages[1]["role"], "tool")

    async def test_schema_is_sent_strictly_without_tools(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"value":"ok"}'}}]})
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(handler))
        schema = {"type": "object", "additionalProperties": False,
                  "required": ["value"], "properties": {"value": {"type": "string"}}}
        await client.chat(model="demo", messages=[], tools=[], format=schema)
        self.assertEqual(requests[0]["response_format"], {"type": "json_schema", "json_schema": {
            "name": "structured_result", "strict": True, "schema": schema}})
        self.assertNotIn("tools", requests[0])

    async def test_schema_unsupported_is_not_retried_as_json_object(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(400, json={"error": {"message": "json_schema unsupported"}})
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(handler))
        with self.assertRaises(httpx.HTTPStatusError):
            await client.chat(model="demo", messages=[], tools=[], format={"type": "object"})
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["response_format"]["type"], "json_schema")

    async def test_ollama_receives_schema_object(self):
        requests = []
        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"model": "demo", "message": {"role": "assistant", "content": "{}"}, "done": True})
        client = model_client.AsyncClient(host="http://localhost:11434")
        await client._client.aclose()
        client._client = httpx.AsyncClient(base_url="http://localhost:11434", transport=httpx.MockTransport(handler))
        try:
            schema = {"type": "object", "properties": {}, "additionalProperties": False, "required": []}
            await client.chat(model="demo", messages=[], tools=[], stream=False, format=schema)
            self.assertEqual(requests[0]["format"], schema)
        finally:
            await client._client.aclose()

    async def test_stream_merges_tool_arguments_and_usage(self):
        chunks = [
            {"choices": [{"delta": {"content": "hello", "tool_calls": [{"index": 0, "function": {"name": "lookup", "arguments": '{"city":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"杭州"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 4}},
        ]
        body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n"
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
        result, _, error = await request_model_with_retry(ollama=client, model="demo", messages=[], tools=[], use_streaming=True)
        self.assertIsNone(error)
        self.assertTrue(result.streamed)
        self.assertEqual(result.message.content, "hello")
        self.assertEqual(json.loads(result.message.tool_calls[0]["function"]["arguments"]), {"city": "杭州"})
        self.assertEqual(result.usage["totalTokens"], 7)

    async def test_stream_rejects_unsuccessful_tool_batches_atomically(self):
        for reason in ("length", "content_filter", "refusal", None):
            with self.subTest(reason=reason):
                chunks = [
                    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
                        "name": "lookup", "arguments": '{"city":"杭州"}'}}]}}]},
                    {"choices": [{"delta": {}, "finish_reason": reason}]},
                ]
                body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
                stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
                released = []
                with self.assertRaises((ValueError, ConnectionError)):
                    async for chunk in stream:
                        released.extend(chunk.message.tool_calls)
                self.assertEqual(released, [])

    async def test_stream_refusal_and_provider_error_preserve_details(self):
        for chunk, detail in (
            ({"choices": [{"delta": {"refusal": "cannot fulfill"}, "finish_reason": "stop"}]}, "cannot fulfill"),
            ({"error": {"message": "maximum context length exceeded"}}, "maximum context length exceeded"),
        ):
            with self.subTest(detail=detail):
                body = "data: " + json.dumps(chunk) + "\n\ndata: [DONE]\n\n"
                client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
                stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
                with self.assertRaisesRegex(ValueError, detail):
                    async for _ in stream:
                        pass

    async def test_invalid_call_rejects_entire_batch_before_release(self):
        valid = {"function": {"name": "lookup", "arguments": '{"city":"杭州"}'}}
        invalid_calls = [
            {"function": {"name": "lookup", "arguments": '{"city":'}},
            {"function": {"name": "lookup", "arguments": '[]'}},
            {"function": {"name": "lookup", "arguments": '{"value":NaN}'}},
            {"function": {"arguments": '{}'}},
            {"function": {"name": "lookup"}},
        ]
        for streaming in (False, True):
            for invalid in invalid_calls:
                with self.subTest(streaming=streaming, invalid=invalid):
                    calls = [valid, invalid]
                    if streaming:
                        chunks = [
                            {"choices": [{"delta": {"tool_calls": [dict(call, index=i) for i, call in enumerate(calls)]}}]},
                            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                        ]
                        body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n"
                        response = httpx.Response(200, text=body)
                    else:
                        response = httpx.Response(200, json={"choices": [{"message": {"tool_calls": calls}, "finish_reason": "tool_calls"}]})
                    client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: response))
                    released = []
                    with self.assertRaises(ValueError):
                        result = await client.chat(model="demo", messages=[], tools=[], stream=streaming)
                        if streaming:
                            async for chunk in result:
                                released.extend(chunk.message.tool_calls)
                        else:
                            released.extend(result.message.tool_calls)
                    self.assertEqual(released, [])

    async def test_text_only_stream_accepts_done_without_finish_reason(self):
        body = 'data: {"choices":[{"delta":{"content":"hello"}}]}\n\ndata: [DONE]\n\n'
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
        stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
        chunks = [chunk async for chunk in stream]
        self.assertEqual("".join(chunk.message.content for chunk in chunks), "hello")

    async def test_successful_tool_stream_supports_stop_and_eof(self):
        body = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n'
        body += 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
        stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
        chunks = [chunk async for chunk in stream]
        self.assertEqual(chunks[-1].message.tool_calls, [{"function": {"name": "lookup", "arguments": "{}"}}])

    async def test_truncated_tool_stream_releases_no_calls(self):
        body = 'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"lookup","arguments":"{}"}}]}}]}\n\n'
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
        stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
        released = []
        with self.assertRaises(ConnectionError):
            async for chunk in stream:
                released.extend(chunk.message.tool_calls)
        self.assertEqual(released, [])
