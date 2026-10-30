"""理解请求，不生成另一套执行流程；当前计划由应用层显式传入。"""

import re
from typing import Any

DOMAINS = {
    "plan": ("计划", "旅行", "目的地", "plan", "trip"),
    "itinerary": (
        "行程",
        "日程",
        "活动",
        "第",
        "天",
        "上午",
        "下午",
        "挪",
        "提前",
        "推迟",
        "itinerary",
        "schedule",
    ),
    "recommendation": ("推荐", "候选", "备选", "recommend", "option"),
    "budget": ("预算", "费用", "花费", "价格", "budget", "cost", "price"),
    "hotel": ("酒店", "住宿", "hotel", "stay"),
    "restaurant": ("餐厅", "吃饭", "午餐", "晚餐", "restaurant", "food"),
    "place": ("景点", "公园", "博物馆", "地点", "place", "attraction"),
    "route": ("路线", "交通", "地铁", "距离", "route", "transit"),
    "memory": ("偏好", "画像", "记住", "我喜欢", "preference", "memory"),
}
INTENTS = {
    "audit": ("检查", "合理吗", "冲突", "太赶", "audit", "review"),
    "update": (
        "修改",
        "改",
        "挪",
        "调整",
        "换成",
        "提前",
        "推迟",
        "删除",
        "增加",
        "补充",
        "update",
        "remove",
        "add",
    ),
    "recommend": ("推荐", "建议", "候选", "recommend", "suggest"),
    "summarize": ("总结", "汇总", "概览", "summary", "summarize"),
    "create": ("创建", "新建", "规划", "安排", "create", "new"),
    "query": ("查", "看看", "显示", "告诉我", "show", "list", "find"),
}


def day_number(value: str) -> int:
    if value.isdigit():
        return int(value)
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    if "十" in value:
        before, after = value.split("十", 1)
        return digits.get(before, 1) * 10 + digits.get(after, 0)
    return digits.get(value, 0)


def understand(
    text: str, current_plan: dict[str, Any] | None, history: list[dict[str, Any]]
) -> dict[str, Any]:
    """采用确定性解析，模糊指代交给拥有完整上下文的模型，不另发一次猜测请求。"""
    lowered = text.lower()
    domains = [
        name
        for name, words in DOMAINS.items()
        if any(word in lowered for word in words)
    ]
    intent = next(
        (
            name
            for name, words in INTENTS.items()
            if any(word in lowered for word in words)
        ),
        "unknown",
    )
    slots: dict[str, Any] = {}
    if current_plan:
        slots.update(
            {
                k: current_plan[k]
                for k in ("plan_id", "destination")
                if current_plan.get(k)
            }
        )
        if "plan" not in domains:
            domains.append("plan")
        explicit_new = any(
            word in lowered
            for word in ("新建", "另开", "新行程", "new trip", "new plan")
        )
        if intent == "create" and not explicit_new:
            intent = "update"
    trip = re.search(r"([零一二三四五六七八九十\d]+)\s*(?:天|days?)", lowered)
    if trip:
        slots["trip_days"] = day_number(trip.group(1))
        if "itinerary" not in domains:
            domains.append("itinerary")
    day = re.search(r"第\s*([零一二三四五六七八九十\d]+)\s*天", text)
    if day:
        slots["day"] = day_number(day.group(1))
    people = re.search(r"(\d+)\s*(?:人|位)", text)
    if people:
        slots["travelers"] = int(people.group(1))
    budget = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|人民币|cny|rmb)", lowered)
    if budget:
        slots.update(budget=float(budget.group(1)), currency="CNY")
    dates = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", text)
    if dates:
        slots["dates"] = dates
    destination = re.search(
        r"(?:去|到|目的地是)([\u4e00-\u9fa5A-Za-z ]{2,25}?)(?:旅行|旅游|玩|\d+天|$)",
        text,
    )
    if destination:
        slots["destination"] = destination.group(1).strip()
    if (slots.get("destination") or intent == "create") and "plan" not in domains:
        domains.append("plan")
    required = (
        ["destination"] if intent == "create" and not slots.get("destination") else []
    )
    # 保留 HTTP 的既有字段形状，task_plan 仅是兼容摘要，不控制图或工具顺序。
    return {
        "original_text": text,
        "rewritten_text": text,
        "changed": False,
        "references": {},
        "semantic": {
            "primary_intent": intent,
            "domains": domains,
            "slots": slots,
            "intent_keywords": [],
            "confidence": 0.8 if domains else 0.3,
        },
        "task_plan": {
            "steps": [],
            "required_slots": required,
            "tool_sequence": [],
            "completion_criteria": [],
            "risk_flags": [],
        },
        "entity_memory": {"current_plan_id": (current_plan or {}).get("plan_id")},
    }
