"""Model configuration and OpenAI Chat Completions transport."""

import json
from contextvars import ContextVar
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from jsonschema import ValidationError, validate

from service.config import get_env_or_dotenv as env
from ollama import AsyncClient
from service.utils.request_log import instrument_client


class OllamaClient(AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instrument_client(self._client)

    async def chat(self, *, tool_choice=None, parallel_tool_calls=None, **kwargs):
        # Ollama lacks tool_choice; the caller must validate the returned invocation.
        return await super().chat(**kwargs)


AVAILABLE_MODELS = (
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "Qwen/Qwen3.5-122B-A10B",
    "zai-org/GLM-5.3-Flash",
)
selected_model = ContextVar("selected_model", default=AVAILABLE_MODELS[0])


def get_model():
    return selected_model.get()


def validate_model(model):
    if model not in AVAILABLE_MODELS:
        raise ValueError("请选择列表中的模型")
    return model


def create_model_client():
    provider = env("LLM_PROVIDER").strip().lower()
    if provider not in {"ollama", "openai", "openai-compatible", "deepseek", "qwen", "siliconflow"}:
        raise ValueError("LLM_PROVIDER must specify a supported provider")
    url_name, key_name = "LLM_BASE_URL", "LLM_API_KEY"
    if provider == "siliconflow":
        url_name, key_name = "SILICONFLOW_BASE_URL", "SILICONFLOW_API_KEY"
    url = env(url_name).strip()
    if provider == "siliconflow" and not url:
        url = "https://api.siliconflow.cn/v1"
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ValueError(f"{url_name} must be an HTTP(S) API base URL")
    key = env(key_name).strip()
    if provider == "ollama":
        client = OllamaClient(host=url, headers={"Authorization": f"Bearer {key}"} if key else None)
        if not key:
            # The SDK reads OLLAMA_API_KEY itself; only LLM_API_KEY is allowed here.
            client._client.headers.pop("authorization", None)
        return client
    if not key:
        raise ValueError(f"{key_name} is required")
    return OpenAICompatibleClient(url, key)


def openai_messages(messages):
    """Preserve native call pairing and add IDs for legacy tool records."""
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
                call_id = call.get("id") or f"call_{index}_{number}"
                calls.append({"id": call_id, "type": "function", "function": function})
                pending.append((function["name"], call_id))
            item["tool_calls"] = calls
        elif role == "tool":
            call_id = message.get("tool_call_id")
            match = next((p for p in pending if p[1] == call_id), None) if call_id else next(
                (p for p in pending if p[0] == message.get("tool_name")), None)
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


def validate_tool_calls(calls: list[dict]) -> None:
    """Reject the whole provider batch before any tool call reaches execution."""
    if not isinstance(calls, list):
        raise ValueError("模型工具调用必须是列表")
    for call in calls:
        function = call.get("function") if isinstance(call, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"].strip():
            raise ValueError("模型工具调用缺少有效的函数名称")
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError as exc:
                raise ValueError("模型工具参数不是有效 JSON") from exc
        if not isinstance(arguments, dict):
            raise ValueError("模型工具参数必须是 JSON 对象")
        try:
            json.dumps(arguments, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("模型工具参数不是有效 JSON") from exc


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
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise ValueError(f"模型拒绝生成结果：{message['refusal']}")
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ValueError(f"模型响应未完整生成：{choice['finish_reason']}")
            validate_tool_calls(message.get("tool_calls") or [])
            if isinstance(format, dict) and not message.get("tool_calls"):
                try:
                    parsed = json.loads(message.get("content") or "")
                    validate(parsed, format)
                except (ValueError, ValidationError) as exc:
                    raise ValueError(f"模型结构化结果不符合 JSON Schema：{exc}") from exc
            result = response_chunk(choice["message"], data.get("usage"))
            result.finish_reason = choice.get("finish_reason")
            result.message.reasoning_content = choice["message"].get("reasoning_content")
            return result

    def _client(self):
        return instrument_client(httpx.AsyncClient(headers={"Authorization": f"Bearer {self.api_key}"},
                                                    timeout=120, transport=self.transport))

    async def _stream(self, payload):
        calls = {}
        usage = {}
        complete = False
        finish_reason = None
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
                        raise ValueError(f"Model stream returned an error: {data['error']}")
                    usage = data.get("usage") or usage
                    choices = data.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    if delta.get("refusal"):
                        raise ValueError(f"模型拒绝生成结果：{delta['refusal']}")
                    if choices[0].get("finish_reason"):
                        finish_reason = choices[0]["finish_reason"]
                        if finish_reason not in {"tool_calls", "stop"}:
                            raise ValueError(f"模型响应未完整生成：{finish_reason}")
                        complete = True
                    for call in delta.get("tool_calls") or []:
                        if not isinstance(call, dict) or not isinstance(call.get("index"), int) or call["index"] < 0:
                            raise ValueError("模型流式工具调用缺少有效索引")
                        fragment = call.get("function") or {}
                        if not isinstance(fragment, dict) or any(not isinstance(fragment.get(key, ""), str) for key in ("name", "arguments")):
                            raise ValueError("模型流式工具调用片段无效")
                        function = calls.setdefault(call["index"], {"name": "", "arguments": ""})
                        for key in ("name", "arguments"):
                            function[key] += fragment.get(key, "")
                    if delta.get("content"):
                        yield response_chunk({"content": delta["content"]})
        if not complete:
            raise ConnectionError("Model stream ended before completion")
        if calls and finish_reason not in {"tool_calls", "stop"}:
            raise ConnectionError("Model tool stream ended before a successful finish_reason")
        tool_calls = [{"function": calls[i]} for i in sorted(calls)]
        validate_tool_calls(tool_calls)
        yield response_chunk({"tool_calls": tool_calls}, usage)
