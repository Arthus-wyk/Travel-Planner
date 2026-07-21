"""把自然语言旅行请求转换为结构化语义、槽位和工具执行计划。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from tools.catalog import domain_tools


@dataclass
class EntityMemory:
    """跨轮次记忆已知实体，避免用户补充行程时重复询问。"""
    current_plan_id: str | None = None
    destination: str | None = None
    hotel: str | None = None
    place: str | None = None
    restaurant: str | None = None
    route_origin: str | None = None
    route_destination: str | None = None
    day: int | None = None
    itinerary_item: str | None = None
    recommendation: str | None = None


@dataclass
class SemanticParse:
    """单次请求的语义解析结果：领域、动作、槽位和风险。"""
    primary_intent: str
    intent_keywords: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    slots: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0


@dataclass
class TaskPlan:
    """给主 agent 的执行建议，描述工具顺序而不是直接执行工具。"""
    steps: list[str] = field(default_factory=list)
    required_slots: list[str] = field(default_factory=list)
    tool_sequence: list[str] = field(default_factory=list)
    completion_criteria: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)


@dataclass
class UnderstandingResult:
    """意图理解阶段的完整输出，供模型上下文和日志共同使用。"""
    original_text: str
    rewritten_text: str
    changed: bool
    references: dict[str, str] = field(default_factory=dict)
    semantic: SemanticParse = field(
        default_factory=lambda: SemanticParse(primary_intent="unknown")
    )
    task_plan: TaskPlan = field(default_factory=TaskPlan)
    entity_memory: EntityMemory = field(default_factory=EntityMemory)
    memory_policy: dict[str, Any] = field(default_factory=dict)
    memory_context: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def format_for_model(self) -> str:
        semantic = self.semantic
        return "\n".join(
            [
                "[User intent understanding]",
                f"Original: {self.original_text}",
                f"Rewritten: {self.rewritten_text}",
                f"Primary intent: {semantic.primary_intent}",
                f"Domains: {', '.join(semantic.domains) if semantic.domains else 'unknown'}",
                f"Slots: {json.dumps(semantic.slots, ensure_ascii=False)}",
                "",
                "[Task plan]",
                f"Steps: {json.dumps(self.task_plan.steps, ensure_ascii=False)}",
                f"Required slots: {json.dumps(self.task_plan.required_slots, ensure_ascii=False)}",
                f"Tool sequence: {json.dumps(self.task_plan.tool_sequence, ensure_ascii=False)}",
                f"Completion criteria: {json.dumps(self.task_plan.completion_criteria, ensure_ascii=False)}",
                f"Risk flags: {json.dumps(self.task_plan.risk_flags, ensure_ascii=False)}",
                "",
                self.memory_context,
                "",
                "[User message]",
                self.rewritten_text,
            ]
        )


INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "query": (
        "查",
        "查询",
        "查看",
        "看看",
        "列出",
        "显示",
        "告诉我",
        "有没有",
        "多少",
        "什么",
        "where",
        "show",
        "list",
        "find",
        "search",
    ),
    "recommend": (
        "推荐",
        "建议",
        "适合",
        "候选",
        "备选",
        "选项",
        "安排几个",
        "recommend",
        "suggest",
        "option",
    ),
    "create": (
        "创建",
        "新建",
        "做一个",
        "规划一个",
        "开始",
        "create",
        "new",
        "start",
    ),
    "update": (
        "改",
        "修改",
        "调整",
        "换成",
        "更新",
        "删掉",
        "删除",
        "不要",
        "增加",
        "加入",
        "补充",
        "update",
        "change",
        "remove",
        "add",
    ),
    "compare": (
        "比较",
        "对比",
        "哪个更",
        "优缺点",
        "compare",
        "versus",
        "vs",
    ),
    "summarize": (
        "总结",
        "汇总",
        "概览",
        "进度",
        "summary",
        "summarize",
    ),
    "audit": (
        "检查",
        "合理吗",
        "冲突",
        "太赶",
        "是否超预算",
        "review",
        "audit",
        "validate",
    ),
}

DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "plan": ("计划", "行程单", "旅行", "旅程", "目的地", "plan", "trip"),
    "itinerary": ("行程", "第", "天", "上午", "下午", "晚上", "itinerary", "schedule"),
    "recommendation": ("推荐", "候选", "备选", "选项", "recommendation", "option"),
    "budget": ("预算", "费用", "花费", "价格", "成本", "钱", "budget", "cost", "price"),
    "hotel": ("酒店", "住宿", "住哪", "hotel", "stay"),
    "restaurant": ("餐厅", "吃饭", "午餐", "晚餐", "早餐", "restaurant", "food"),
    "place": ("景点", "地方", "地点", "公园", "博物馆", "寺", "place", "attraction"),
    "route": ("路线", "交通", "地铁", "打车", "距离", "多久", "route", "transit"),
    "memory": ("偏好", "画像", "记住", "我喜欢", "我不喜欢", "preference", "memory"),
}
ITINERARY_MUTATION_TOOLS = {
    "add_itinerary_item",
    "update_itinerary_item",
    "delete_itinerary_item",
    "move_itinerary_item",
    "update_travel_profile",
}
GLOBAL_AUDIT_FIELDS = {
    "start_date",
    "end_date",
    "budget",
    "currency",
    "constraints",
    "travel_style",
}

DOMAIN_TOOLS: dict[str, list[str]] = domain_tools()


async def understand_user_input(
    user_text: str,
    messages: list[Any],
    *,
    ollama: Any | None = None,
    model: str | None = None,
    memory_context: str = "",
    memory_policy: dict[str, Any] | None = None,
) -> UnderstandingResult:
    """读取上下文、解析意图并生成可执行计划。"""
    memory = build_entity_memory(messages)
    rewritten_text, references = await rewrite_references(
        user_text,
        memory,
        messages=messages,
        ollama=ollama,
        model=model,
    )
    semantic = parse_semantics(rewritten_text, memory)
    task_plan = build_task_plan(rewritten_text, semantic, memory)
    return UnderstandingResult(
        original_text=user_text,
        rewritten_text=rewritten_text,
        changed=rewritten_text != user_text,
        references=references,
        semantic=semantic,
        task_plan=task_plan,
        entity_memory=memory,
        memory_context=memory_context,
        memory_policy=memory_policy or {},
    )


def build_entity_memory(messages: list[Any]) -> EntityMemory:
    """从历史消息和工具结果中恢复当前行程实体。"""
    memory = EntityMemory()
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if content is None:
            continue
        if message.get("role") != "tool":
            update_memory_from_text(memory, str(content))
        update_memory_from_json(memory, content)
    return memory


def update_memory_from_json(memory: EntityMemory, content: Any) -> None:
    if isinstance(content, dict):
        _walk_json(memory, content)
        return

    if not isinstance(content, str):
        return

    candidates = [content]
    try:
        parsed = json.loads(content)
        candidates.append(parsed)
    except json.JSONDecodeError:
        pass

    embedded_plan = _extract_current_plan_context(content)
    if embedded_plan is not None:
        candidates.append(embedded_plan)

    for candidate in candidates:
        if isinstance(candidate, dict):
            _walk_json(memory, candidate)


def update_memory_from_text(memory: EntityMemory, text: str) -> None:
    quoted = re.findall(r"[「『“\"]([^」』”\"]{2,40})[」』”\"]", text)
    if quoted:
        memory.place = quoted[-1]

    day_match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*天", text)
    if day_match:
        memory.day = _parse_day_number(day_match.group(1))

    destination_match = re.search(
        r"(?:去|到|目的地是|目的地[:：]\s*)([\u4e00-\u9fa5A-Za-z\s]{2,30})(?:旅行|旅游|玩|$)",
        text,
    )
    if destination_match:
        memory.destination = destination_match.group(1).strip()

    for keyword, attr in (
        ("酒店", "hotel"),
        ("住宿", "hotel"),
        ("餐厅", "restaurant"),
        ("景点", "place"),
        ("博物馆", "place"),
        ("公园", "place"),
    ):
        if keyword in text:
            candidate = _nearby_entity(text, keyword)
            if candidate:
                setattr(memory, attr, candidate)


async def rewrite_references(
    text: str,
    memory: EntityMemory,
    *,
    messages: list[Any] | None = None,
    ollama: Any | None = None,
    model: str | None = None,
) -> tuple[str, dict[str, str]]:
    candidates = _reference_candidates(memory)
    if not candidates or ollama is None or not model:
        return text, {}

    prompt = "\n".join(
        [
            "你负责判断用户句子中的指代对象，并在必要时重写句子。",
            "不要用固定关键词规则；请根据当前用户句子、最近上下文和候选记忆判断。",
            "只有在指代对象明确时才重写。无法确定、没有指代或候选不匹配时，保持原文。",
            "重写时保留用户原意，只给被解析的短语补充“（指：具体对象）”。",
            "只输出 JSON，不要输出解释。JSON 格式：",
            '{"rewritten_text":"...","references":{"原短语":"具体对象"}}',
            "",
            f"用户句子：{text}",
            "候选记忆：" + json.dumps(candidates, ensure_ascii=False, indent=2),
            "最近上下文：" + json.dumps(_recent_context(messages or []), ensure_ascii=False, indent=2),
        ]
    )

    try:
        response = await ollama.chat(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a precise coreference resolver. Return strict JSON only.",
                },
                {"role": "user", "content": prompt},
            ],
            stream=False,
            format="json",
            options={"temperature": 0},
        )
    except Exception:
        return text, {}

    content = getattr(getattr(response, "message", None), "content", "") or ""
    parsed = _parse_json_object(content)
    if not isinstance(parsed, dict):
        return text, {}

    rewritten = parsed.get("rewritten_text")
    raw_references = parsed.get("references")
    if not isinstance(rewritten, str):
        return text, {}

    references: dict[str, str] = {}
    if isinstance(raw_references, dict):
        for phrase, value in raw_references.items():
            if isinstance(phrase, str) and isinstance(value, str) and phrase and value:
                references[phrase] = value

    if rewritten == text and not references:
        return text, {}
    return rewritten, references


def parse_semantics(text: str, memory: EntityMemory | None = None) -> SemanticParse:
    """使用规则和已有记忆抽取动作、领域、槽位及风险标记。"""
    normalized = text.lower()
    intent_scores: dict[str, int] = {}
    intent_hits: dict[str, list[str]] = {}
    for intent, keywords in INTENT_KEYWORDS.items():
        hits = [keyword for keyword in keywords if keyword.lower() in normalized]
        if hits:
            intent_scores[intent] = len(hits)
            intent_hits[intent] = hits

    primary_intent = "unknown"
    if intent_scores:
        primary_intent = max(intent_scores, key=lambda key: intent_scores[key])
    if primary_intent == "unknown" and re.search(
        r"(?:\u89c4\u5212|\u5b89\u6392|\u505a).*(?:\u884c\u7a0b|\u65c5\u884c|\u65c5\u6e38)",
        text,
    ):
        primary_intent = "create"

    domains = []
    for domain, keywords in DOMAIN_KEYWORDS.items():
        if any(keyword.lower() in normalized for keyword in keywords):
            domains.append(domain)
    if re.search(r"(?:\u884c\u7a0b|\u65e5\u7a0b|\u5929|\u665a)", text):
        domains.append("itinerary")

    slots = extract_slots(text, memory or EntityMemory())
    if slots.get("day") and "itinerary" not in domains:
        domains.append("itinerary")
    if slots.get("trip_days") and "itinerary" not in domains:
        domains.append("itinerary")
    if slots.get("budget") is not None and "budget" not in domains:
        domains.append("budget")
    if slots.get("destination") and "plan" not in domains:
        domains.append("plan")

    hit_count = sum(intent_scores.values()) + len(domains) + len(slots)
    confidence = min(0.95, 0.25 + hit_count * 0.1)

    return SemanticParse(
        primary_intent=primary_intent,
        intent_keywords=intent_hits.get(primary_intent, []),
        domains=domains,
        slots=slots,
        confidence=round(confidence, 2),
    )

def append_itinerary_post_actions(
    tool_sequence: list[str],
    *,
    requires_route_reconciliation: bool = True,
) -> list[str]:
    sequence = list(tool_sequence)

    if requires_route_reconciliation:
        sequence.append("reconcile_travel_plan")

    sequence.append("audit_travel_plan")
    sequence.append("verify_last_tool_result")

    return _dedupe(sequence)

def build_task_plan(
    text: str,
    semantic: SemanticParse,
    memory: EntityMemory,
) -> TaskPlan:
    """根据语义和当前是否已有 plan_id 选择工具执行链。"""
    steps: list[str] = []
    required_slots: list[str] = []
    tool_sequence: list[str] = []
    completion_criteria: list[str] = []
    risk_flags = detect_risk_flags(text, semantic)

    intent = semantic.primary_intent
    domains = semantic.domains
    slots = semantic.slots

    if _is_existing_plan_itinerary_request(text, semantic, memory):
        steps.extend(
            [
                "Read the current travel plan",
                "Fill the requested relative day-by-day itinerary",
                "Audit the updated plan before answering",
            ]
        )
        tool_sequence.extend(
            ["get_travel_plan", "add_itinerary_item", "reconcile_travel_plan", "audit_travel_plan"]
        )
        completion_criteria.append(
            "The existing plan_id is reused and the itinerary covers the requested trip length."
        )
    elif intent == "create":
        steps.extend(["收集核心旅行需求", "创建旅行计划工作区", "返回计划摘要和下一步建议"])
        for slot in ("destination", "dates", "travelers", "budget"):
            if slot not in slots:
                required_slots.append(slot)
        tool_sequence.append("create_travel_plan")
        completion_criteria.append("已创建 plan_id，并确认目的地、日期、人数和预算")
    elif intent == "recommend":
        steps.extend(["理解推荐约束", "搜索候选地点", "按偏好和质量排序", "解释推荐取舍"])
        if not (slots.get("destination") or memory.destination):
            required_slots.append("destination")
        tool_sequence.extend(["read_travel_preference_profile", *_tools_for_domains(domains)])
        completion_criteria.append("给出排序后的候选项、理由和注意事项")
    elif intent == "update":
        steps.extend([
            "定位要修改的计划或行程项",
            "执行对应更新",
            "刷新受影响的路线",
            "检查整个行程的时间、节奏、预算和冲突",
            "验证最终结果",
        ])
        if not (slots.get("plan_id") or memory.current_plan_id):
            required_slots.append("plan_id")
        mutation_tools = _tools_for_domains(domains)

        tool_sequence.extend(["get_travel_plan", mutation_tools])
        if any(tool in ITINERARY_MUTATION_TOOLS for tool in mutation_tools):
            tool_sequence = append_itinerary_post_actions(
                tool_sequence,
                requires_route_reconciliation=True,
            )
        else:
            tool_sequence.append("verify_last_tool_result")
        completion_criteria.append("工具返回 success=true，且更新内容与用户请求一致")
    elif intent == "audit":
        steps.extend(["读取当前计划", "检查日期、预算、节奏、路线和预订风险", "给出修正建议"])
        if not (slots.get("plan_id") or memory.current_plan_id):
            required_slots.append("plan_id")
        tool_sequence.extend(["get_travel_plan", "audit_travel_plan"])
        completion_criteria.append("输出一致性问题、严重程度和可执行修正建议")
    elif intent == "summarize":
        steps.extend(["读取计划摘要", "汇总进度、预算和未完成事项"])
        if not (slots.get("plan_id") or memory.current_plan_id):
            required_slots.append("plan_id")
        tool_sequence.append("summarize_travel_plan")
        completion_criteria.append("返回当前计划进度、预算使用和缺口")
    else:
        steps.extend(["判断用户目标", "必要时询问一个阻塞性问题", "在信息足够时调用工具或直接回答"])
        completion_criteria.append("用户得到清晰回答，或阻塞信息被明确指出")

    if "memory" in domains:
        tool_sequence.insert(0, "update_travel_preference_profile")
        completion_criteria.append("长期偏好已写入 Markdown 用户画像")

    return TaskPlan(
        steps=_dedupe(steps),
        required_slots=_dedupe(required_slots),
        tool_sequence=_dedupe(tool_sequence),
        completion_criteria=_dedupe(completion_criteria),
        risk_flags=risk_flags,
    )


def extract_slots(text: str, memory: EntityMemory) -> dict[str, Any]:
    slots: dict[str, Any] = {}

    day_match = re.search(r"第\s*([一二三四五六七八九十\d]+)\s*天", text)
    if day_match:
        slots["day"] = _parse_day_number(day_match.group(1))
    elif memory.day is not None and any(word in text for word in ("当天", "这天", "那天")):
        slots["day"] = memory.day

    budget_match = re.search(r"(\d+(?:\.\d+)?)\s*(元|块|人民币|cny|rmb)", text, flags=re.I)
    if budget_match:
        value = float(budget_match.group(1))
        slots["budget"] = int(value) if value.is_integer() else value
        slots["currency"] = "CNY"

    date_match = re.findall(r"\d{4}-\d{1,2}-\d{1,2}", text)
    if date_match:
        slots["dates"] = date_match

    people_match = re.search(r"(\d+)\s*(人|位|个大人|个小孩)", text)
    if people_match:
        slots["travelers"] = int(people_match.group(1))

    destination_match = re.search(
        r"(?:去|到|目的地是|目的地[:：]\s*)([\u4e00-\u9fa5A-Za-z\s]{2,30})(?:旅行|旅游|玩|$)",
        text,
    )
    if destination_match is None:
        destination_match = re.search(
            r"(?:规划|计划|做一个|安排)(?:一个|一下)?([\u4e00-\u9fa5A-Za-z\s]{2,30}?)(?:\d+\s*天|[一二三四五六七八九十]+\s*天|亲子游|自由行|旅行|旅游|游)",
            text,
        )
    if destination_match:
        slots["destination"] = destination_match.group(1).strip()
    elif memory.destination:
        slots["destination"] = memory.destination

    trip_length_match = re.search(r"(\d+|[一二三四五六七八九十]+)\s*天", text)
    if trip_length_match is None:
        trip_length_match = re.search(
            r"(\d+)\s*(?:\u5929|day|days)(?:\s*\d+\s*(?:\u665a|night|nights))?",
            text,
            flags=re.I,
        )
    if trip_length_match:
        slots["trip_days"] = _parse_day_number(trip_length_match.group(1))

    if memory.current_plan_id:
        slots["plan_id"] = memory.current_plan_id

    return slots


def detect_risk_flags(text: str, semantic: SemanticParse) -> list[str]:
    flags: list[str] = []
    normalized = text.lower()
    if any(word in text for word in ("预算", "便宜", "省钱", "超支")):
        flags.append("budget_risk")
    if any(word in text for word in ("太赶", "塞满", "特种兵", "一天很多", "紧凑")):
        flags.append("pace_risk")
    if any(word in text for word in ("冲突", "日期", "闭馆", "营业时间")):
        flags.append("date_or_availability_risk")
    if any(word in text for word in ("老人", "小孩", "轮椅", "带娃", "孕妇")):
        flags.append("accessibility_or_family_risk")
    if any(word in normalized for word in ("route", "transit", "drive")) or any(
        word in text for word in ("路线", "交通", "打车", "地铁", "距离")
    ):
        flags.append("route_risk")
    if "budget" in semantic.domains and semantic.slots.get("budget") is not None:
        flags.append("budget_constraint")
    return _dedupe(flags)


def _tools_for_domains(domains: list[str]) -> list[str]:
    tools: list[str] = []
    for domain in domains:
        tools.extend(DOMAIN_TOOLS.get(domain, []))
    return _dedupe(tools)


def _reference_candidates(memory: EntityMemory) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for attr in (
        "current_plan_id",
        "destination",
        "hotel",
        "place",
        "restaurant",
        "route_origin",
        "route_destination",
        "day",
        "itinerary_item",
        "recommendation",
    ):
        value = _memory_value(memory, attr)
        if value is not None:
            candidates[attr] = value
    return candidates


def _recent_context(messages: list[Any], limit: int = 8) -> list[dict[str, str]]:
    context: list[dict[str, str]] = []
    for message in messages[-limit:]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        if role == "system":
            continue
        content = str(message.get("content") or "")
        if len(content) > 800:
            content = content[:800] + "..."
        context.append({"role": role, "content": content})
    return context


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if match is None:
            return None
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, dict) else None


def _extract_current_plan_context(text: str) -> dict[str, Any] | None:
    marker = "Current travel plan context (JSON):"
    marker_index = text.find(marker)
    if marker_index < 0:
        return None

    payload_start = text.find("{", marker_index + len(marker))
    if payload_start < 0:
        return None

    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text[payload_start:])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _is_existing_plan_itinerary_request(
    text: str,
    semantic: SemanticParse,
    memory: EntityMemory,
) -> bool:
    if not (semantic.slots.get("plan_id") or memory.current_plan_id):
        return False
    if semantic.primary_intent not in {"create", "update"}:
        return False
    if semantic.slots.get("trip_days") or "itinerary" in semantic.domains:
        return True
    return bool(
        re.search(
            r"(?:\d+\s*(?:\u5929|day|days)|itinerary|schedule)",
            text,
            flags=re.I,
        )
    )


def _walk_json(memory: EntityMemory, value: Any) -> None:
    if isinstance(value, dict):
        _assign_known_fields(memory, value)
        for child in value.values():
            _walk_json(memory, child)
    elif isinstance(value, list):
        for child in value:
            _walk_json(memory, child)
    elif isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return
        _walk_json(memory, parsed)


def _assign_known_fields(memory: EntityMemory, value: dict[str, Any]) -> None:
    if isinstance(value.get("plan_id"), str):
        memory.current_plan_id = value["plan_id"]
    if isinstance(value.get("destination"), str):
        memory.destination = value["destination"]
    if isinstance(value.get("day"), int):
        memory.day = value["day"]
    if isinstance(value.get("title"), str):
        memory.itinerary_item = value["title"]
        memory.place = value["title"]
    if isinstance(value.get("location"), str):
        memory.place = value["location"]
    if isinstance(value.get("name"), str):
        name = value["name"]
        category = str(value.get("category", ""))
        memory.recommendation = name
        if "hotel" in category.lower() or "酒店" in category or "住宿" in category:
            memory.hotel = name
        elif "restaurant" in category.lower() or "餐" in category:
            memory.restaurant = name
        else:
            memory.place = name


def _memory_value(memory: EntityMemory, attr: str) -> str | None:
    if attr == "day":
        return f"第 {memory.day} 天" if memory.day is not None else None
    value = getattr(memory, attr)
    if value:
        return str(value)
    return None


def _nearby_entity(text: str, keyword: str) -> str | None:
    pattern = rf"([\u4e00-\u9fa5A-Za-z0-9路\s]{{2,24}}{keyword})"
    matches = re.findall(pattern, text)
    if matches:
        return matches[-1].strip(" ：:，,。.!！")
    return None


def _parse_day_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)

    numerals = {
        "一": 1,
        "二": 2,
        "三": 3,
        "四": 4,
        "五": 5,
        "六": 6,
        "七": 7,
        "八": 8,
        "九": 9,
        "十": 10,
    }
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2:
        return 10 + numerals.get(value[1], 0)
    if value.endswith("十") and len(value) == 2:
        return numerals.get(value[0], 0) * 10
    if "十" in value and len(value) == 3:
        return numerals.get(value[0], 0) * 10 + numerals.get(value[2], 0)
    return numerals.get(value)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
