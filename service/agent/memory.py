"""Long/short-term memory policy and LLM-mediated memory extraction."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from service.agent.preference_profile import update_preference_profile

MID_TERM_PATH = Path("data") / "memory" / "mid_term_memory.jsonl"
FORGET_WORDS = ("忘记以前", "忘掉以前", "不要参考历史", "不参考历史", "别用以前", "从零开始", "forget previous", "ignore history")


def memory_policy(text: str) -> dict[str, Any]:
    """Decide recall scope before intent understanding; this is deliberately deterministic."""
    lowered = text.lower()
    forget = any(word.lower() in lowered for word in FORGET_WORDS)
    if forget:
        return {"read_long_term": False, "read_mid_term": False, "reason": "explicit_forget_request"}
    return {"read_long_term": True, "read_mid_term": True, "reason": "default"}


def memory_context(text: str, profile: dict[str, Any] | None, mid_term: list[dict[str, Any]] | None) -> str:
    policy = memory_policy(text)
    if not policy["read_long_term"] and not policy["read_mid_term"]:
        return "[Memory policy]\nDo not use long-term or mid-term memory for this request. Use only the current short-term conversation."
    return "[Memory context]\n" + json.dumps({
        "long_term_profile": profile or {},
        "mid_term_memories": mid_term or [],
        "instruction": "Current explicit user requirements override remembered preferences.",
    }, ensure_ascii=False, indent=2)


def read_mid_term(limit: int = 30) -> list[dict[str, Any]]:
    if not MID_TERM_PATH.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in MID_TERM_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


async def extract_and_store(ollama: Any, model: str, user_text: str, answer: str, *, plan_id: str | None = None) -> dict[str, Any]:
    """Ask the model to classify memories, then persist only approved categories."""
    prompt = """从用户消息和本轮结果中提取记忆。只返回严格 JSON。
长期记忆只允许写入用户明确表达、且看起来跨旅行稳定的个人偏好；禁止把本次旅行的目的地、日期、预算、行程安排写入长期记忆。
本次旅行的明确要求写入 mid_term_memories。模型推测、临时要求、工具结果本身不要写入。
格式：{"long_term_preferences":{"category":["value"]},"mid_term_memories":[{"content":"...","type":"decision|constraint|fact"}]}
没有内容时返回空对象。"""
    try:
        response = await ollama.chat(model=model, messages=[{"role": "system", "content": prompt}, {"role": "user", "content": f"用户：{user_text}\n本轮结果：{answer}"}], stream=False, format="json", options={"temperature": 0})
        raw = getattr(getattr(response, "message", None), "content", "") or "{}"
        data = json.loads(raw)
    except Exception:
        return {"success": False, "message": "memory extraction failed"}
    long_term = data.get("long_term_preferences") if isinstance(data, dict) else {}
    mid_term = data.get("mid_term_memories") if isinstance(data, dict) else []
    changed = update_preference_profile(long_term, note="LLM-confirmed explicit long-term preference") if isinstance(long_term, dict) and long_term else {"changed": {}}
    if isinstance(mid_term, list) and mid_term:
        MID_TERM_PATH.parent.mkdir(parents=True, exist_ok=True)
        with MID_TERM_PATH.open("a", encoding="utf-8") as handle:
            for item in mid_term:
                if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"].strip():
                    handle.write(json.dumps({**item, "plan_id": plan_id}, ensure_ascii=False) + "\n")
    return {"success": True, "long_term": changed.get("changed", {}), "mid_term_count": len(mid_term) if isinstance(mid_term, list) else 0}
