"""对过长的模型上下文做有损压缩，保留后续决策必需的信息。"""

from typing import Any


def compress_messages(
    messages: list[Any],
    *,
    keep_recent: int = 10,
    max_tool_result_chars: int = 3000,
) -> list[Any]:
    """
    上下文压缩器：保留系统消息和最近消息，并截断过长工具输出。

    It:
    1. Preserves system messages.
    2. Preserves the most recent messages.
    3. Truncates oversized tool outputs.

    这样可以避免上下文超限，同时保留当前任务最相关的信息。
    """

    normalized: list[Any] = []

    for message in messages:
        if isinstance(message, dict):
            copied = dict(message)

            if copied.get("role") == "tool":
                content = str(copied.get("content", ""))

                if len(content) > max_tool_result_chars:
                    copied["content"] = (
                        content[:max_tool_result_chars]
                        + "\n...[tool result truncated during context compression]"
                    )

            normalized.append(copied)

        else:
            normalized.append(message)

    system_messages = [
        message
        for message in normalized
        if (isinstance(message, dict) and message.get("role") == "system")
        or getattr(message, "role", None) == "system"
    ]

    non_system_messages = [
        message for message in normalized if message not in system_messages
    ]

    recent_messages = non_system_messages[-keep_recent:]

    compression_notice = {
        "role": "system",
        "content": (
            "Earlier conversation messages were removed because the context "
            "window was exceeded. Continue using the remaining recent context."
        ),
    }

    return [
        *system_messages,
        compression_notice,
        *recent_messages,
    ]
