"""记忆策略与提取；实际文件操作只有 memory_store 一处负责。"""

import json
from typing import Any
from agent.compress import compress_messages
from agent.context import AgentContext
from infrastructure import llm
from service.storage import memory_store

FORGET_WORDS = (
    "忘记以前",
    "忘掉以前",
    "不要参考历史",
    "不参考历史",
    "别用以前",
    "从零开始",
    "forget previous",
    "ignore history",
)


def memory_policy(text: str) -> dict[str, Any]:
    enabled = not any(word in text.lower() for word in FORGET_WORDS)
    return {
        "read_long_term": enabled,
        "read_mid_term": enabled,
        "reason": "default" if enabled else "explicit_forget_request",
    }


def recall(text: str, enabled: bool) -> tuple[dict[str, Any], str]:
    policy = memory_policy(text)
    if not enabled or not policy["read_long_term"]:
        return (
            policy,
            "[Memory policy]\nUse only the current conversation and current plan.",
        )
    # 先判断策略再读取，用户要求忽略历史时连文件 IO 也跳过。
    data = {
        "long_term_profile": memory_store.read_preference_profile().get("profile", {}),
        "mid_term_memories": memory_store.read_mid_term(),
    }
    return policy, "[Memory context]\n" + json.dumps(data, ensure_ascii=False)


async def extract_and_store(
    context: AgentContext, text: str, answer: str, plan_id: str | None
) -> dict[str, Any]:
    prompt = (
        "从用户明确表达中提取记忆，只返回 JSON。长期偏好必须跨旅行稳定，禁止保存本次目的地、日期、预算和安排。"
        "本次旅行明确要求放 mid_term_memories，禁止保存模型推测或工具结果。格式："
        '{"long_term_preferences":{"category":["value"]},"mid_term_memories":[{"content":"...","type":"decision|constraint|fact"}]}。'
    )
    response, _, error = await llm.request_model_with_retry(
        ollama=context.client,
        model=context.model,
        messages=[
            {"role": "system", "content": prompt},
            {"role": "user", "content": f"用户：{text}\n结果：{answer}"},
        ],
        tools=[],
        response_format="json",
        context_compressor=compress_messages,
        api_telemetry=context.telemetry,
    )
    if response is None:
        return {"success": False, "message": error}
    try:
        data = json.loads(response.message.content)
    except (json.JSONDecodeError, TypeError):
        return {"success": False, "message": "Invalid memory extraction JSON"}
    if not isinstance(data, dict):
        return {"success": False, "message": "Memory extraction must be an object"}
    preferences = data.get("long_term_preferences", {})
    if not isinstance(preferences, dict) or set(preferences) - set(
        memory_store.PROFILE_SECTIONS
    ):
        return {"success": False, "message": "Invalid preference categories"}
    changed = (
        memory_store.update_preference_profile(
            preferences, note="用户明确表达的长期偏好"
        )
        if preferences
        else {"changed": {}}
    )
    items = data.get("mid_term_memories", [])
    valid = (
        [
            item
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("content"), str)
            and item["content"].strip()
            and item.get("type") in {"decision", "constraint", "fact"}
        ]
        if isinstance(items, list)
        else []
    )
    count = memory_store.append_mid_term(valid, plan_id=plan_id)
    return {
        "success": changed.get("success", True),
        "long_term": changed.get("changed", {}),
        "mid_term_count": count,
    }
