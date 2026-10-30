"""统一提示与输出护栏；不创建连接、不处理存储。"""

import json
from datetime import datetime
from typing import Any


def public_answer(answer: str, plan: dict[str, Any] | None) -> str:
    """已保存计划后模型偶尔只返回编号；统一生成可展示、可入库的完成说明。"""
    if plan is None:
        return answer
    try:
        parsed = json.loads(answer)
    except (json.JSONDecodeError, TypeError):
        return answer
    if not isinstance(parsed, dict) or set(parsed) != {"plan_id"}:
        return answer
    title = plan.get("title") or plan.get("destination") or "当前行程"
    return (
        f"已更新行程“{title}”。\n"
        f"已保存 {len(plan.get('itinerary') or [])} 个行程安排和 {len(plan.get('recommendations') or [])} 个推荐。\n"
        f"行程编号：{plan.get('plan_id', '')}"
    )


SYSTEM_PROMPT = """
TRIP-DAY ITINERARY ANCHOR RULES:
- For a guided planning trip, preserve the selected transport, hotel, home anchor and purchases. Day 1 starts from home, middle days start and end at the hotel, and the final day ends at home. The selected intercity transport belongs in the itinerary timeline.
- For a legacy free-form trip without guided planning, use the known hotel as the usual daily anchor and follow the user's explicit start or end point.
- Never invent a hotel, departure point or transport time that has not been selected or provided.

你是一个旅行规划 Agent，可以通过 MCP 工具创建、读取和更新旅行计划。

核心目标：
- 把用户的模糊旅行需求转成可执行、节奏合理、预算可解释的行程。
- 优先维护当前行程；只有用户明确要求“新建/另开一个行程”时才创建新计划。
- 不重复索要上下文里已经存在的信息。

工作流程：
1. 先理解用户真实意图：新建行程、补全日程、修改安排、推荐地点、查路线、查预算或总结。
2. 判断是否真的缺少阻塞信息。只有目的地、行程对象、人数、预算、日期等信息完全无法从当前上下文或工具结果获得，且没有合理默认值时，才问一个简短澄清问题。
3. 只要当前信息足以行动，就立即调用工具。不要先说“我会处理”“请稍等”“接下来我将”。
4. 工具调用后必须阅读结果，再决定继续调用工具、修正参数或给最终回复。
5. 创建或修改行程后，用工具结果、摘要或审计结果支撑最终答复。

当前行程处理规则：
- 如果系统消息提供了 Current travel plan context，请把它当作权威的当前行程状态。
- 当前行程里已有 destination、travelers、budget、currency、interests、constraints、itinerary 等字段时，不要再次要求用户提供这些字段。
- 用户说“帮我规划一个 N 天 M 晚行程”“补一下行程”“安排三天怎么玩”时，默认是在补全当前行程的 day-by-day itinerary。
- 缺少具体日历日期不阻塞相对天数行程；使用 day=1、day=2 等相对天数即可。
- 如果当前行程已经有部分 itinerary，新增或修改时要尽量保留已有安排，避免无提示覆盖。

工具使用规则：
- 需要持久化计划、日程、推荐、预算时必须调用工具，不能只在文字里承诺完成。
- 不要无意义调用工具；简单解释、总结已有消息时可以直接回答。
- 只能使用系统提供的工具，不要把工具名和参数以 JSON 或 Markdown 代码块展示给用户。
- 工具报错时，根据错误修改参数后重试；不要原样重复同一个失败调用。
- 工具结果未确认 success=true 时，不要声称已经完成修改。

意图理解块规则：
- 用户消息可能包含 "User intent understanding" 和 "Task plan"。
- 把 "Rewritten" 当作解析后的用户请求，把 slots/domains/tool sequence 当作参考，不要机械服从明显错误的计划。
- 如果 Task plan 建议 create_travel_plan，但当前上下文已有 plan_id，且用户是在规划/补全当前行程，应改用读取和更新当前行程的工具。

行程质量规则：
- 安排要符合真实旅行节奏，避免一天塞太满。
- 明确说明关键假设，尤其是日期未知、预算估算、跨城市交通和预订风险。
- 推荐候选项时，尽量结合长期偏好画像；用户明确表达稳定偏好时再更新画像。
- 最终回复用用户语言，简洁说明已完成什么、行程亮点、预算/交通注意事项和下一步可调整项。
"""

PROMISE_PHRASES = (
    "我将",
    "我会",
    "接下来",
    "现在，请稍等",
    "请稍等",
    "稍等片刻",
    "我将为您",
    "我会为您",
    "我将获取",
    "我会查询",
    "我会将",
    "我将添加",
    "我会添加",
    "开始考虑",
    "首先，我会",
    "然后，我会",
    "最后，我会",
    "I will",
    "I'll",
    "Let me",
    "please wait",
)

ACTION_VERBS = (
    "添加",
    "查询",
    "获取",
    "检查",
    "创建",
    "更新",
    "修改",
    "安排",
    "加入",
    "搜索",
    "估算",
    "验证",
    "add",
    "search",
    "check",
    "create",
    "update",
    "verify",
)


def is_empty_execution_promise(content: str) -> bool:
    normalized = content.strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    has_promise = any(phrase.lower() in lowered for phrase in PROMISE_PHRASES)
    has_action = any(verb.lower() in lowered for verb in ACTION_VERBS)
    return has_promise and has_action


def build_system_prompt(plan: dict[str, Any] | None) -> str:
    if not plan:
        return SYSTEM_PROMPT
    trip_days = _trip_day_count(plan)
    summary = _current_plan_summary(plan, trip_days)
    planning = plan.get("planning") or {}
    selected_ids = set((planning.get("selected_transport") or {}).values())
    selected_ids.add(planning.get("selected_hotel"))
    context = json.dumps(
        {
            "plan_id": plan.get("plan_id"),
            "title": plan.get("title"),
            "destination": plan.get("destination"),
            "start_date": plan.get("start_date"),
            "end_date": plan.get("end_date"),
            "travelers": plan.get("travelers"),
            "budget": plan.get("budget"),
            "currency": plan.get("currency"),
            "interests": plan.get("interests"),
            "constraints": plan.get("constraints"),
            "itinerary": plan.get("itinerary"),
            "recommendations": plan.get("recommendations"),
            "budget_items": plan.get("budget_items"),
            "guided planning": {
                "stage": planning.get("stage"),
                "home_location": planning.get("home_location"),
                "selected_candidates": [item for item in (planning.get("transport_candidates") or []) + (planning.get("hotel_candidates") or []) if item.get("id") in selected_ids],
                "purchases": planning.get("purchases") or [],
                "needs_replan": planning.get("needs_replan", False),
            } if planning else None,
        },
        ensure_ascii=False,
    )
    return (
        SYSTEM_PROMPT
        + "\n\n[Current travel plan summary]\n"
        + summary
        + f"\n\nCurrent travel plan context (JSON):\n{context}\n\n"
        + "[Current-plan execution rules]\n"
        + f"- Always reuse plan_id: {plan.get('plan_id')} for edits to this trip.\n"
        + "- The summary and JSON above are authoritative. Never ask again for any "
        + "field that is already present there.\n"
        + "- If the user asks for a multi-day plan, day-by-day route, or N-day "
        + "N-night itinerary, call tools to add or update itinerary items on this "
        + "existing plan.\n"
        + "- Missing start_date/end_date does not block planning by relative days. "
        + "Use day 1, day 2, day 3, etc.\n"
        + "- Create a new plan only when the user explicitly asks for a separate "
        + "trip, a new destination workspace, or to start over."
    )


def _current_plan_summary(plan: dict[str, Any], trip_days: int | None) -> str:
    date_text = _date_summary(plan, trip_days)
    itinerary_count = len(plan.get("itinerary") or [])
    recommendation_count = len(plan.get("recommendations") or [])
    budget_item_count = len(plan.get("budget_items") or [])
    return "\n".join(
        [
            f"- plan_id: {plan.get('plan_id')}",
            f"- title: {plan.get('title') or ''}",
            f"- destination: {plan.get('destination') or ''}",
            f"- dates: {date_text}",
            f"- travelers: {plan.get('travelers') or 'unknown'}",
            f"- budget: {plan.get('budget') or 'unknown'} {plan.get('currency') or ''}".rstrip(),
            f"- interests: {', '.join(plan.get('interests') or []) or 'none'}",
            f"- constraints: {', '.join(plan.get('constraints') or []) or 'none'}",
            f"- existing itinerary items: {itinerary_count}",
            f"- existing recommendations: {recommendation_count}",
            f"- existing budget items: {budget_item_count}",
        ]
    )


def _date_summary(plan: dict[str, Any], trip_days: int | None) -> str:
    start = plan.get("start_date")
    end = plan.get("end_date")
    if start and end:
        day_text = f", {trip_days} days" if trip_days else ""
        return f"{start} to {end}{day_text}"
    return "not set; relative day planning is allowed"


def _trip_day_count(plan: dict[str, Any]) -> int | None:
    start = str(plan.get("start_date") or "")
    end = str(plan.get("end_date") or "")
    if not start or not end:
        return None
    try:
        start_date = datetime.fromisoformat(start).date()
        end_date = datetime.fromisoformat(end).date()
    except ValueError:
        return None
    days = (end_date - start_date).days + 1
    return days if days > 0 else None
