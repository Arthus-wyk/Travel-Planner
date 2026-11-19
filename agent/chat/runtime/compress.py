"""Compress message history while retaining task evidence and complete native tool exchanges."""
import json
from typing import Any


def compress_messages(messages: list[Any], *, keep_recent: int = 10,
                      max_tool_result_chars: int = 3000) -> list[Any]:
    """Preserve role instructions, first/current tasks and whole recent tool-call groups.

    Content truncation changes only copied messages; caller history is never mutated.
    """
    normalized = [dict(message) if isinstance(message, dict) else message for message in messages]

    def role(message):
        return message.get("role") if isinstance(message, dict) else getattr(message, "role", None)

    def is_task(message):
        if not isinstance(message, dict) or role(message) != "user":
            return False
        try:
            value = json.loads(message.get("content", ""))
        except (ValueError, TypeError):
            return False
        return isinstance(value, dict) and "task" in value

    tasks = [index for index, message in enumerate(normalized) if is_task(message)]
    users = [index for index, message in enumerate(normalized) if role(message) == "user"]
    preserved = {index for index, message in enumerate(normalized) if role(message) == "system"}
    if tasks:
        preserved.update((tasks[0], tasks[-1]))
    elif users:
        preserved.add(users[-1])
    groups = []
    for index, message in enumerate(normalized):
        if role(message) == "system":
            continue
        if role(message) == "tool":
            if groups and isinstance(normalized[groups[-1][0]], dict) and normalized[groups[-1][0]].get("tool_calls"):
                groups[-1].append(index)
            continue
        groups.append([index])
    recent = 0
    for group in reversed(groups):
        if recent >= keep_recent:
            break
        preserved.update(group)
        recent += len(group)
    for index in preserved:
        message = normalized[index]
        if isinstance(message, dict) and role(message) == "tool":
            content = str(message.get("content", ""))
            if len(content) > max_tool_result_chars:
                message["content"] = content[:max_tool_result_chars] + "\n...[tool result truncated during context compression]"
    notice = {"role": "system", "content": "Earlier conversation was compressed. Use the retained task and verified tool evidence."}
    result = [normalized[index] for index in sorted(preserved)]
    # Keep the original role prompt first, including when recent history contains tool calls.
    result.insert(1 if result and role(result[0]) == "system" else 0, notice)
    return result
