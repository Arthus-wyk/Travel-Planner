"""Model configuration and OpenAI Chat Completions transport."""

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx

from service.config import get_env_or_dotenv as env
from ollama import AsyncClient


class OllamaClient(AsyncClient):
    async def chat(self, *, tool_choice=None, parallel_tool_calls=None, **kwargs):
        # Ollama lacks tool_choice; the caller must validate the returned invocation.
        return await super().chat(**kwargs)


def get_model():
    model = env("LLM_MODEL").strip()
    if not model:
        raise ValueError("LLM_MODEL is required")
    return model


def create_model_client():
    provider = env("LLM_PROVIDER").strip().lower()
    if provider not in {"ollama", "openai", "openai-compatible", "deepseek", "qwen"}:
        raise ValueError("LLM_PROVIDER must specify a supported provider")
    url = env("LLM_BASE_URL").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ValueError("LLM_BASE_URL must be an HTTP(S) API base URL")
    key = env("LLM_API_KEY").strip()
    if provider == "ollama":
        client = OllamaClient(host=url, headers={"Authorization": f"Bearer {key}"} if key else None)
        if not key:
            # The SDK reads OLLAMA_API_KEY itself; only LLM_API_KEY is allowed here.
            client._client.headers.pop("authorization", None)
        return client
    if not key:
        raise ValueError("LLM_API_KEY is required")
    return OpenAICompatibleClient(url, key)


def openai_messages(messages):
    """Rebuild call IDs because the Agent stores provider-neutral tool records."""
    converted = []
    pending = []
    for index, message in enumerate(messages):
        role = message["role"]
        item = {"role": role, "content": message.get("content") or ""}
        if role == "assistant" and message.get("tool_calls"):
            pending = []
            calls = []
            for number, call in enumerate(message["tool_calls"]):
                function = dict(call["function"])
                if not isinstance(function["arguments"], str):
                    function["arguments"] = json.dumps(function["arguments"], ensure_ascii=False)
                call_id = f"call_{index}_{number}"
                calls.append({"id": call_id, "type": "function", "function": function})
                pending.append((function["name"], call_id))
            item["tool_calls"] = calls
        elif role == "tool":
            match = next((p for p in pending if p[0] == message.get("tool_name")), None)
            if match:
                item["tool_call_id"] = match[1]
                pending.remove(match)
            else:
                # Verification and compressed orphan results are context, not API tool replies.
                item = {"role": "user", "content": f"[{message.get('tool_name', 'tool')} result]\n{item['content']}"}
        converted.append(item)
    # OpenAI requires all tool replies immediately after their assistant call.
    for index in range(len(converted) - 1, -1, -1):
        item = converted[index]
        if item.get("tool_calls"):
            end = next((i for i in range(index + 1, len(converted)) if converted[i]["role"] == "assistant"), len(converted))
            replies = [m for m in converted[index + 1:end] if m["role"] == "tool"]
            ids = {m["tool_call_id"] for m in replies}
            item["tool_calls"] = [c for c in item["tool_calls"] if c["id"] in ids]
            if not item["tool_calls"]:
                del item["tool_calls"]
            converted[index + 1:end] = replies + [m for m in converted[index + 1:end] if m["role"] != "tool"]
    return converted


def response_chunk(message, usage=None):
    usage = usage or {}
    return SimpleNamespace(
        message=SimpleNamespace(role="assistant", content=message.get("content") or "", tool_calls=message.get("tool_calls") or []),
        prompt_eval_count=usage.get("prompt_tokens", 0),
        eval_count=usage.get("completion_tokens", 0),
        cache_read_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0),
    )


class OpenAICompatibleClient:
    def __init__(self, base_url, api_key, *, transport=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    async def chat(self, *, model, messages, tools, stream=False, options=None, format=None, tool_choice=None, parallel_tool_calls=None):
        payload = {"model": model, "messages": openai_messages(messages), "stream": stream, "temperature": (options or {}).get("temperature", 0)}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        if parallel_tool_calls is not None:
            payload["parallel_tool_calls"] = parallel_tool_calls
        if format:
            payload["response_format"] = (
                {"type": "json_schema", "json_schema": {
                    "name": "structured_result", "strict": True, "schema": format
                }} if isinstance(format, dict) else {"type": "json_object"}
            )
        if stream:
            payload["stream_options"] = {"include_usage": True}
            return self._stream(payload)
        async with self._client() as client:
            response = await client.post(self.base_url + "/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            return response_chunk(data["choices"][0]["message"], data.get("usage"))

    def _client(self):
        return httpx.AsyncClient(headers={"Authorization": f"Bearer {self.api_key}"}, timeout=120, transport=self.transport)

    async def _stream(self, payload):
        calls = {}
        usage = {}
        complete = False
        async with self._client() as client:
            async with client.stream("POST", self.base_url + "/chat/completions", json=payload) as response:
                if response.is_error:
                    await response.aread()
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    value = line[5:].strip()
                    if value == "[DONE]":
                        complete = True
                        break
                    data = json.loads(value)
                    if "error" in data:
                        raise ValueError("Model stream returned an error")
                    usage = data.get("usage") or usage
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if choices[0].get("finish_reason"):
                        complete = True
                    for call in delta.get("tool_calls") or []:
                        function = calls.setdefault(call["index"], {"name": "", "arguments": ""})
                        for key in ("name", "arguments"):
                            function[key] += (call.get("function") or {}).get(key) or ""
                    if delta.get("content"):
                        yield response_chunk({"content": delta["content"]})
        if not complete:
            raise ConnectionError("Model stream ended before completion")
        yield response_chunk({"tool_calls": [{"function": calls[i]} for i in sorted(calls)]}, usage)
