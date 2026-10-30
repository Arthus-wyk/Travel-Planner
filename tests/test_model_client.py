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

    def test_cli_and_http_use_only_environment_model(self):
        from service import cli
        from service.api import server
        with patch.dict(os.environ, {"LLM_MODEL": "configured"}):
            for entry in (cli, server):
                with patch("sys.argv", ["program"]):
                    self.assertEqual(entry.parse_args().model, "configured")
                with patch("sys.argv", ["program", "--model", "override"]):
                    with self.assertRaises(SystemExit):
                        entry.parse_args()

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

    async def test_truncated_stream_fails_instead_of_executing_partial_calls(self):
        client = model_client.OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, text='data: {"choices":[{"delta":{"content":"partial"}}]}\n\n')))
        stream = await client.chat(model="demo", messages=[], tools=[], stream=True)
        with self.assertRaisesRegex(ConnectionError, "ended"):
            async for _ in stream:
                pass

    def test_configuration_requires_explicit_values(self):
        values = {"LLM_PROVIDER": "openai", "LLM_MODEL": "demo", "LLM_BASE_URL": "https://example.com/v1", "LLM_API_KEY": "key", "AGENT_MODEL": "legacy", "OLLAMA_HOST": "https://legacy.example"}
        with patch.dict(os.environ, values):
            self.assertEqual(model_client.get_model(), "demo")
            self.assertIsInstance(model_client.create_model_client(), model_client.OpenAICompatibleClient)
            for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_API_KEY"):
                with self.subTest(name=name), patch.dict(os.environ, {name: ""}):
                    with self.assertRaisesRegex(ValueError, name):
                        model_client.create_model_client()
            with patch.dict(os.environ, {"LLM_MODEL": ""}):
                with self.assertRaisesRegex(ValueError, "LLM_MODEL"):
                    model_client.get_model()
            with patch.dict(os.environ, {"LLM_PROVIDER": "unknown"}):
                with self.assertRaisesRegex(ValueError, "LLM_PROVIDER"):
                    model_client.create_model_client()

    def test_explicit_ollama_uses_only_llm_values(self):
        with patch.dict(os.environ, {"LLM_PROVIDER": "ollama", "LLM_BASE_URL": "http://localhost:11434", "LLM_API_KEY": "", "OLLAMA_API_KEY": "ignored"}):
            client = model_client.create_model_client()
            self.assertNotIn("authorization", client._client.headers)
            self.assertEqual(str(client._client.base_url), "http://localhost:11434")

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
