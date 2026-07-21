from __future__ import annotations

import asyncio
import json
import random
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Awaitable, Callable

from mcp import ClientSession
from ollama import AsyncClient

from service.agent.compress import compress_messages

StreamCallback = Callable[[str], Awaitable[None]]

MAX_TOOL_ROUNDS = 10


# ============================================================
# Retry configuration
# ============================================================

@dataclass
class RetryConfig:
    max_attempts: int = 10

    # Exponential backoff:
    # delay = min(base_delay * 2 ** retry_index, max_delay)
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 32.0
    jitter_seconds: float = 0.5

    # Switch to fallback model after this many consecutive 529 errors.
    fallback_after_529: int = 2

    # Only compress the context this many times.
    max_context_compressions: int = 1

    # Only attempt authentication recovery this many times.
    max_auth_recoveries: int = 1


# ============================================================
# Normalized model response
# ============================================================

@dataclass
class NormalizedMessage:
    role: str
    content: str
    tool_calls: list[Any]


@dataclass
class NormalizedResponse:
    message: NormalizedMessage
    model: str
    streamed: bool
    usage: dict[str, Any]


# ============================================================
# Error helpers
# ============================================================

def get_status_code(exc: Exception) -> int | None:
    """
    Try to extract an HTTP status code from different SDK exceptions.
    """

    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    if response is not None:
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code

    return None


def get_error_text(exc: Exception) -> str:
    return str(exc).lower()


def is_auth_error(exc: Exception) -> bool:
    status = get_status_code(exc)
    text = get_error_text(exc)

    return status in {401, 403} or any(
        keyword in text
        for keyword in (
            "unauthorized",
            "authentication",
            "invalid api key",
            "expired token",
            "access token expired",
            "permission denied",
        )
    )


def is_prompt_too_long_error(exc: Exception) -> bool:
    status = get_status_code(exc)
    text = get_error_text(exc)

    return status == 413 or any(
        keyword in text
        for keyword in (
            "context length",
            "context window",
            "prompt is too long",
            "prompt too long",
            "maximum context",
            "maximum sequence length",
            "too many tokens",
            "input length exceeds",
            "request too large",
        )
    )


def is_network_error(exc: Exception) -> bool:
    text = get_error_text(exc)

    return isinstance(
        exc,
        (
            TimeoutError,
            ConnectionError,
            asyncio.TimeoutError,
        ),
    ) or any(
        keyword in text
        for keyword in (
            "connection refused",
            "connection reset",
            "connection aborted",
            "network error",
            "timed out",
            "timeout",
            "temporary failure",
            "server disconnected",
            "remote protocol error",
        )
    )


def is_retryable_error(exc: Exception) -> bool:
    status = get_status_code(exc)

    if is_network_error(exc):
        return True

    if status == 429:
        return True

    if status is not None and 500 <= status <= 599:
        return True

    return False


def calculate_retry_delay(
    retry_index: int,
    config: RetryConfig,
) -> float:
    delay = min(
        config.base_delay_seconds * (2 ** retry_index),
        config.max_delay_seconds,
    )

    return delay + random.uniform(0, config.jitter_seconds)


# ============================================================
# Message conversion
# ============================================================

def normalize_non_stream_response(
    response: Any,
    model: str,
) -> NormalizedResponse:
    message = response.message

    return NormalizedResponse(
        message=NormalizedMessage(
            role=getattr(message, "role", "assistant"),
            content=getattr(message, "content", "") or "",
            tool_calls=list(getattr(message, "tool_calls", None) or []),
        ),
        model=model,
        streamed=False,
        usage=extract_usage_summary(response, model),
    )


async def collect_stream_response(
    stream: Any,
    model: str,
    onText: StreamCallback | None = None,
) -> NormalizedResponse:
    """
    Collect an Ollama streaming response into the same structure used by
    non-streaming responses.

    Tool calls are collected from stream chunks when present.
    """

    content_parts: list[str] = []
    tool_calls: list[Any] = []
    usage_source: Any | None = None

    async for chunk in stream:
        usage_source = chunk
        message = getattr(chunk, "message", None)
        if message is None:
            continue

        content = getattr(message, "content", None)
        if content:
            content_parts.append(content)
            if onText is not None:
                await onText(content)

        chunk_tool_calls = getattr(message, "tool_calls", None)
        if chunk_tool_calls:
            tool_calls.extend(chunk_tool_calls)



    return NormalizedResponse(
        message=NormalizedMessage(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_calls,
        ),
        model=model,
        streamed=True,
        usage=extract_usage_summary(usage_source, model),
    )


def extract_usage_summary(response: Any, model: str) -> dict[str, Any]:
    """从 Ollama 响应里提取统一的 token / 耗时统计。

    Ollama 的字段名通常是：
    - prompt_eval_count: 输入 token 数
    - eval_count: 输出 token 数
    - total_duration: 总耗时，单位纳秒
    本函数统一转换成 /usage 和 API 事件都能直接使用的字段。
    """

    input_tokens = int(getattr(response, "prompt_eval_count", 0) or 0)
    output_tokens = int(getattr(response, "eval_count", 0) or 0)
    total_duration_ns = int(getattr(response, "total_duration", 0) or 0)

    return {
        "model": model,
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": input_tokens + output_tokens,
        "cacheReadTokens": 0,
        "modelDurationMs": round(total_duration_ns / 1_000_000, 2)
        if total_duration_ns
        else None,
    }

# ============================================================
# Retry wrapper
# ============================================================

async def request_model_with_retry(
    *,
    ollama: AsyncClient,
    model: str,
    messages: list[Any],
    tools: list[dict[str, Any]],
    config: RetryConfig | None = None,
    fallback_model: str | None = None,
    use_streaming: bool = False,
    on_text: StreamCallback | None = None,
    auth_recovery: Callable[[], Awaitable[None]] | None = None,
    context_compressor: (
        Callable[[list[Any]], list[Any] | Awaitable[list[Any]]] | None
    ) = None,
    api_telemetry: Any | None = None,
) -> tuple[NormalizedResponse | None, list[Any], str | None]:
    """
    Request the model with retry and recovery logic.

    Returns:
        response:
            Normalized model response, or None after final failure.

        updated_messages:
            The possibly compressed message list.

        error_message:
            None on success, otherwise a user-facing API error message.
    """

    config = config or RetryConfig()

    current_model = model
    current_messages = list(messages)
    streaming_enabled = use_streaming

    consecutive_529 = 0
    context_compression_count = 0
    auth_recovery_count = 0

    last_exception: Exception | None = None

    for attempt in range(1, config.max_attempts + 1):
        request_started = perf_counter()

        try:
            print(
                f"[model request] attempt={attempt}, "
                f"model={current_model}, "
                f"stream={streaming_enabled}"
            )

            if api_telemetry is not None:
                api_telemetry.record_query(
                    model=current_model,
                    message_count=len(current_messages),
                    tool_count=len(tools),
                    attempt=attempt,
                    streamed=streaming_enabled,
                )

            if streaming_enabled:
                stream = await ollama.chat(
                    model=current_model,
                    messages=current_messages,
                    tools=tools,
                    stream=True,
                    options={"temperature": 0},
                )

                response = await collect_stream_response(
                    stream,
                    current_model,
                    onText=on_text,
                )

            else:
                raw_response = await ollama.chat(
                    model=current_model,
                    messages=current_messages,
                    tools=tools,
                    stream=False,
                    options={"temperature": 0},
                )

                response = normalize_non_stream_response(
                    raw_response,
                    current_model,
                )

            if api_telemetry is not None:
                api_telemetry.record_success(
                    model=current_model,
                    message_count=len(current_messages),
                    tool_count=len(tools),
                    attempt=attempt,
                    streamed=streaming_enabled,
                    duration_seconds=perf_counter() - request_started,
                    usage=response.usage,
                )

            return response, current_messages, None

        except Exception as exc:
            last_exception = exc

            status = get_status_code(exc)
            error_text = str(exc)

            print(
                f"[model error] attempt={attempt}, "
                f"status={status}, "
                f"type={type(exc).__name__}, "
                f"message={error_text}"
            )

            # ------------------------------------------------
            # 1. Recoverable authentication failure
            # ------------------------------------------------

            if (
                is_auth_error(exc)
                and auth_recovery is not None
                and auth_recovery_count < config.max_auth_recoveries
            ):
                if api_telemetry is not None:
                    api_telemetry.record_error(
                        model=current_model,
                        message_count=len(current_messages),
                        tool_count=len(tools),
                        attempt=attempt,
                        streamed=streaming_enabled,
                        duration_seconds=perf_counter() - request_started,
                        error=exc,
                        status_code=status,
                        will_retry=True,
                    )

                auth_recovery_count += 1

                print("[retry] attempting authentication recovery")

                try:
                    await auth_recovery()
                except Exception as recovery_exc:
                    print(
                        "[retry] authentication recovery failed: "
                        f"{recovery_exc}"
                    )

                continue

            # ------------------------------------------------
            # 2. Prompt too long: compress context and retry
            # ------------------------------------------------

            if (
                is_prompt_too_long_error(exc)
                and context_compression_count
                < config.max_context_compressions
            ):
                if api_telemetry is not None:
                    api_telemetry.record_error(
                        model=current_model,
                        message_count=len(current_messages),
                        tool_count=len(tools),
                        attempt=attempt,
                        streamed=streaming_enabled,
                        duration_seconds=perf_counter() - request_started,
                        error=exc,
                        status_code=status,
                        will_retry=True,
                    )

                context_compression_count += 1

                print("[retry] compressing context")

                compressor = context_compressor or compress_messages
                compressed = compressor(current_messages)

                if asyncio.iscoroutine(compressed):
                    compressed = await compressed

                current_messages = compressed
                consecutive_529 = 0

                continue

            # ------------------------------------------------
            # 3. Streaming failed: downgrade to non-streaming
            # ------------------------------------------------

            if streaming_enabled:
                if api_telemetry is not None:
                    api_telemetry.record_error(
                        model=current_model,
                        message_count=len(current_messages),
                        tool_count=len(tools),
                        attempt=attempt,
                        streamed=streaming_enabled,
                        duration_seconds=perf_counter() - request_started,
                        error=exc,
                        status_code=status,
                        will_retry=True,
                    )

                print("[retry] streaming failed; switching to non-streaming")

                streaming_enabled = False
                consecutive_529 = 0

                continue

            # ------------------------------------------------
            # 4. Track consecutive 529 errors
            # ------------------------------------------------

            if status == 529:
                consecutive_529 += 1
            else:
                consecutive_529 = 0

            # ------------------------------------------------
            # 5. Switch to fallback model after repeated 529
            # ------------------------------------------------

            if (
                status == 529
                and fallback_model
                and current_model != fallback_model
                and consecutive_529 >= config.fallback_after_529
            ):
                if api_telemetry is not None:
                    api_telemetry.record_error(
                        model=current_model,
                        message_count=len(current_messages),
                        tool_count=len(tools),
                        attempt=attempt,
                        streamed=streaming_enabled,
                        duration_seconds=perf_counter() - request_started,
                        error=exc,
                        status_code=status,
                        will_retry=True,
                    )

                print(
                    f"[retry] switching model from "
                    f"{current_model} to {fallback_model}"
                )

                current_model = fallback_model
                consecutive_529 = 0

                continue

            # ------------------------------------------------
            # 6. Network, 429 and server errors: backoff
            # ------------------------------------------------

            if is_retryable_error(exc):
                if attempt >= config.max_attempts:
                    if api_telemetry is not None:
                        api_telemetry.record_error(
                            model=current_model,
                            message_count=len(current_messages),
                            tool_count=len(tools),
                            attempt=attempt,
                            streamed=streaming_enabled,
                            duration_seconds=perf_counter() - request_started,
                            error=exc,
                            status_code=status,
                            will_retry=False,
                        )

                    break

                delay = calculate_retry_delay(
                    retry_index=attempt - 1,
                    config=config,
                )

                if api_telemetry is not None:
                    api_telemetry.record_error(
                        model=current_model,
                        message_count=len(current_messages),
                        tool_count=len(tools),
                        attempt=attempt,
                        streamed=streaming_enabled,
                        duration_seconds=perf_counter() - request_started,
                        error=exc,
                        status_code=status,
                        will_retry=True,
                    )

                print(f"[retry] waiting {delay:.2f} seconds")
                await asyncio.sleep(delay)

                continue

            # Non-retryable error.
            if api_telemetry is not None:
                api_telemetry.record_error(
                    model=current_model,
                    message_count=len(current_messages),
                    tool_count=len(tools),
                    attempt=attempt,
                    streamed=streaming_enabled,
                    duration_seconds=perf_counter() - request_started,
                    error=exc,
                    status_code=status,
                    will_retry=False,
                )

            break

    # ========================================================
    # Final API error
    # ========================================================

    if last_exception is None:
        error_message = "The model request failed for an unknown reason."
    else:
        status = get_status_code(last_exception)

        details = (
            f"{type(last_exception).__name__}: "
            f"{str(last_exception)}"
        )

        if status is not None:
            error_message = (
                f"API request failed after retries "
                f"(HTTP {status}). {details}"
            )
        else:
            error_message = (
                f"API request failed after retries. {details}"
            )

    return None, current_messages, error_message


# ============================================================
# MCP result serialization
# ============================================================

def tool_result_to_text(result: Any) -> str:
    """Serialize an MCP CallToolResult for the model."""

    structured = getattr(result, "structuredContent", None)

    if structured is None:
        structured = getattr(result, "structured_content", None)

    if structured is not None:
        return json.dumps(
            structured,
            ensure_ascii=False,
        )

    texts = [
        item.text
        for item in getattr(result, "content", [])
        if hasattr(item, "text")
    ]

    if texts:
        return "\n".join(texts)

    return repr(result)


# ============================================================
# Agent loop
# ============================================================

