"""Final-output contracts must not repeat business tools while correcting JSON."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from ollama import ChatResponse

from infrastructure.llm import RetryConfig, request_model_with_retry
from service.utils.model_client import OpenAICompatibleClient, OllamaClient, StructuredOutputError
from agent.chat.runtime.memory import MEMORY_SCHEMA, extract_and_store

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["rewritten_text"],
          "properties": {"rewritten_text": {"type": "string"}}}


class StructuredContractsTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_feedback_corrects_once_without_tools(self):
        requests = []
        def respond(request):
            requests.append(json.loads(request.content))
            content = {"resolved_text": "park"} if len(requests) == 1 else {"rewritten_text": "park"}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(content)}}]})
        client = OpenAICompatibleClient("https://example.test/v1", "test", transport=httpx.MockTransport(respond))
        response, _, error = await request_model_with_retry(ollama=client, model="offline", messages=[],
            tools=[{"type": "function", "function": {"name": "search"}}],
            response_format=SCHEMA, config=RetryConfig(max_attempts=2))
        self.assertIsNone(error)
        self.assertEqual(json.loads(response.message.content), {"rewritten_text": "park"})
        self.assertNotIn("tools", requests[1])
        self.assertTrue(any("rewritten_text" in str(m["content"]) and "required" in str(m["content"])
                            for m in requests[1]["messages"]))
        self.assertEqual(len(requests), 2)

    async def test_invalid_output_has_only_one_correction_and_protocol_never_retries(self):
        for finish, expected in (("stop", 2), ("tool_calls", 1), ("length", 1)):
            requests = []
            def respond(request):
                requests.append(request)
                return httpx.Response(200, json={"choices": [{"finish_reason": finish,
                    "message": {"content": "{}"}}]})
            client = OpenAICompatibleClient("https://example.test/v1", "test", transport=httpx.MockTransport(respond))
            with self.assertRaises(StructuredOutputError):
                await request_model_with_retry(ollama=client, model="offline", messages=[], tools=[],
                    response_format=SCHEMA, config=RetryConfig(max_attempts=6))
            self.assertEqual(len(requests), expected)

    async def test_ollama_uses_prompt_and_validates_final_object(self):
        client = OllamaClient(host="http://localhost:11434")
        try:
            with patch("ollama.AsyncClient.chat", new=AsyncMock(return_value=ChatResponse(
                model="offline", message={"role": "assistant", "content": "{}"}))) as chat:
                with self.assertRaises(StructuredOutputError):
                    await client.chat(model="offline", messages=[], tools=[], format=SCHEMA)
                self.assertIn("JSON Schema", chat.call_args.kwargs["messages"][-1]["content"])
        finally:
            await client._client.aclose()

    async def test_ollama_stream_validates_before_exposing_content(self):
        async def chunks():
            for text in ('{"rewritten_', 'text":"park"}'):
                yield ChatResponse(model="offline", message={"role": "assistant", "content": text})
        client = OllamaClient(host="http://localhost:11434")
        try:
            with patch("ollama.AsyncClient.chat", new=AsyncMock(return_value=chunks())):
                stream = await client.chat(model="offline", messages=[], tools=[], format=SCHEMA, stream=True)
                output = [chunk async for chunk in stream]
            self.assertEqual(len(output), 1)
            self.assertEqual(json.loads(output[0].message.content), {"rewritten_text": "park"})
        finally:
            await client._client.aclose()

    async def test_memory_rejects_invalid_data_before_writing(self):
        context = SimpleNamespace(client=None, model="offline", telemetry=None)
        invalid = json.dumps({"long_term_preferences": {}, "mid_term_memories": [{"type": "fact"}]})
        with patch("agent.chat.runtime.memory.llm.request_model_with_retry", new=AsyncMock(return_value=(
            SimpleNamespace(message=SimpleNamespace(content=invalid)), [], None))), patch(
            "agent.chat.runtime.memory.memory_store.update_preference_profile") as update, patch(
            "agent.chat.runtime.memory.memory_store.append_mid_term") as append:
            result = await extract_and_store(context, "hi", "hello", None)
        self.assertFalse(result["success"])
        update.assert_not_called()
        append.assert_not_called()

