"""Model configuration and OpenAI Chat Completions transport."""

import json
from contextvars import ContextVar
import logging
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from json_repair import loads as repair_json
from jsonschema import ValidationError, validate

from service.config import get_env_or_dotenv as env
from ollama import AsyncClient
from service.utils.request_log import instrument_client

logger = logging.getLogger(__name__)


class StructuredOutputError(ValueError):
    def __init__(self, code: str, message: str, *, content: str = ""):
        super().__init__(message)
        self.code = code
        self.content = content


def structured_messages(messages: list, schema: dict) -> list:
    """Give every provider the same final-answer contract without changing caller history."""
    instruction = (
        "工具调用仍按工具协议执行。最终答案只返回符合以下 JSON Schema 的完整 JSON 对象。"
        "严格使用字段名，所有 required 字段必须存在，无内容的数组填 []，不要输出 Markdown。"
        "不得为了通过校验编造地点、ID、工具结果或完成状态。JSON Schema:\n"
        + json.dumps(schema, ensure_ascii=False)
    )
    schema_text = json.dumps(schema, ensure_ascii=False)
    if any(message.get("role") == "system" and schema_text in str(message.get("content", ""))
           for message in messages):
        return list(messages)
    return [*messages, {"role": "system", "content": instruction}]


def decode_structured_output(content: str | None, schema: dict) -> tuple[dict, bool]:
    """Decode final JSON and report syntax recovery after validating the original schema."""
    if not isinstance(content, str) or not content.strip():
        raise StructuredOutputError("empty_output", "模型返回了空的结构化结果。")
    repaired = False
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        try:
            payload = repair_json(content, skip_json_loads=True)
        except (ValueError, TypeError, RecursionError):
            raise StructuredOutputError("invalid_json", "模型返回的 JSON 无法解析。", content=content) from None
        if not isinstance(payload, dict) or not payload:
            raise StructuredOutputError("invalid_json", "模型返回的 JSON 无法解析。", content=content)
        repaired = True
    try:
        if not isinstance(payload, dict):
            raise ValidationError("expected object")
        validate(payload, schema)
    except ValidationError as exc:
        path = "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in exc.absolute_path)
        detail = f"{path}: {exc.validator}"
        if exc.validator == "required":
            missing = [key for key in exc.validator_value if key not in exc.instance]
            detail += " 缺少字段 " + ", ".join(missing)
        raise StructuredOutputError("invalid_schema", "模型结果不符合 JSON Schema。" + detail,
                                    content=content) from None
    return payload, repaired


class OllamaClient(AsyncClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instrument_client(self._client)

    async def chat(self, *, tool_choice=None, parallel_tool_calls=None, **kwargs):
        # Ollama lacks tool_choice; the caller must validate the returned invocation.
        schema = kwargs.get("format")
        if isinstance(schema, dict):
            kwargs["messages"] = structured_messages(kwargs["messages"], schema)
        response = await super().chat(**kwargs)
        if not isinstance(schema, dict):
            return response
        if kwargs.get("stream"):
            return self._structured_stream(response, schema)
        self._validate_final(response, schema)
        return response

    @staticmethod
    def _validate_final(response, schema):
        reason = getattr(response, "done_reason", None)
        if reason in {"length", "max_tokens"}:
            raise StructuredOutputError("truncated_output", "模型结构化结果被截断。")
        if not response.message.tool_calls:
            parsed, repaired = decode_structured_output(response.message.content, schema)
            if repaired:
                response.message.content = json.dumps(parsed, ensure_ascii=False)

    async def _structured_stream(self, stream, schema):
        chunks = []
        async for chunk in stream:
            chunks.append(chunk)
        content = "".join(chunk.message.content or "" for chunk in chunks)
        if chunks:
            final = chunks[-1].model_copy(deep=True)
            final.message.content = content
            final.message.tool_calls = [call for chunk in chunks for call in (chunk.message.tool_calls or [])]
            self._validate_final(final, schema)
            yield final
        else:
            raise StructuredOutputError("empty_output", "模型返回了空的结构化结果。")


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
    return OpenAICompatibleClient(url, key, json_object_only=provider == "deepseek")


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
    def __init__(self, base_url, api_key, *, transport=None, json_object_only=False):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.json_object_only = json_object_only

    async def chat(self, *, model, messages, tools, stream=False, options=None, format=None, tool_choice=None, parallel_tool_calls=None):
        if isinstance(format, dict):
            messages = structured_messages(messages, format)
        payload = {"model": model, "messages": openai_messages(messages), "stream": stream, "temperature": (options or {}).get("temperature", 0)}
        if (options or {}).get("num_predict") is not None:
            payload["max_tokens"] = options["num_predict"]
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
                }} if isinstance(format, dict) and not self.json_object_only else {"type": "json_object"}
            )
        if stream:
            payload["stream_options"] = {"include_usage": True}
            return self._stream(payload, format if isinstance(format, dict) else None)
        async with self._client() as client:
            response = await client.post(self.base_url + "/chat/completions", json=payload)
            response.raise_for_status()
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            if message.get("refusal"):
                raise ValueError(f"模型拒绝生成结果：{message['refusal']}")
            finish_reason = choice.get("finish_reason")
            if finish_reason == "tool_calls" and not message.get("tool_calls"):
                raise StructuredOutputError("invalid_tool_response", "模型声明工具调用但未返回调用内容。")
            if finish_reason == "length" and isinstance(format, dict) and not message.get("tool_calls"):
                logger.info("Structured output truncated model=%s usage=%s", model, data.get("usage"))
                raise StructuredOutputError("truncated_output", "模型结构化结果被截断。")
            if finish_reason in {"length", "content_filter"}:
                raise ValueError(f"模型响应未完整生成：{finish_reason}")
            validate_tool_calls(message.get("tool_calls") or [])
            if isinstance(format, dict) and not message.get("tool_calls"):
                parsed, repaired = decode_structured_output(message.get("content"), format)
                if repaired:
                    logger.info("Repaired structured output model=%s finish_reason=%s usage=%s",
                                model, finish_reason, data.get("usage"))
                    message = {**message, "content": json.dumps(parsed, ensure_ascii=False)}
            result = response_chunk(message, data.get("usage"))
            result.finish_reason = finish_reason
            result.message.reasoning_content = choice["message"].get("reasoning_content")
            return result

    def _client(self):
        return instrument_client(httpx.AsyncClient(headers={"Authorization": f"Bearer {self.api_key}"},
                                                    timeout=120, transport=self.transport))

    async def _stream(self, payload, schema: dict | None = None):
        calls = {}
        usage = {}
        content_parts = []
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
                        if finish_reason == "length" and schema is not None and not (calls or delta.get("tool_calls")):
                            raise StructuredOutputError("truncated_output", "模型结构化结果被截断。")
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
                        if schema is None:
                            yield response_chunk({"content": delta["content"]})
                        else:
                            content_parts.append(delta["content"])
        if not complete:
            raise ConnectionError("Model stream ended before completion")
        if finish_reason == "tool_calls" and not calls:
            raise StructuredOutputError("invalid_tool_response", "模型声明工具调用但未返回调用内容。")
        if calls and finish_reason not in {"tool_calls", "stop"}:
            raise ConnectionError("Model tool stream ended before a successful finish_reason")
        tool_calls = [{"function": calls[i]} for i in sorted(calls)]
        validate_tool_calls(tool_calls)
        content = "".join(content_parts)
        if schema is not None and not tool_calls:
            parsed, repaired = decode_structured_output(content, schema)
            if repaired:
                logger.info("Repaired structured output model=%s finish_reason=%s usage=%s",
                            payload["model"], finish_reason, usage)
                content = json.dumps(parsed, ensure_ascii=False)
        result = response_chunk({"content": content, "tool_calls": tool_calls}, usage)
        result.finish_reason = finish_reason
        yield result
