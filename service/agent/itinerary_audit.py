"""对已生成行程做确定性检查，不依赖模型主观判断。"""

from __future__ import annotations

from datetime import date
from typing import Any

from service.validation import time_minutes


def audit_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """检查天数、每日强度、餐饮空缺、预算和预订风险，并生成建议。"""
    itinerary = plan.get("itinerary") or []
    recommendations = plan.get("recommendations") or []
    budget_items = plan.get("budget_items") or []
    budget = number(plan.get("budget"))
    trip_days = date_span_days(plan.get("start_date"), plan.get("end_date"))

    issues: list[dict[str, Any]] = []
    suggestions: list[str] = []

    if trip_days is not None:
        for item in itinerary:
            day = item.get("day")
            if not isinstance(day, int) or day < 1 or day > trip_days:
                issues.append(
                    issue(
                        "date_conflict",
                        "high",
                        f"行程项 {item.get('item_id')} 的 day={day} 超出旅行天数 {trip_days}。",
                        item.get("item_id"),
                    )
                )

    by_day: dict[int, list[dict[str, Any]]] = {}
    for item in itinerary:
        if isinstance(item.get("day"), int):
            by_day.setdefault(item["day"], []).append(item)

    for day, items in sorted(by_day.items()):
        ordered = sorted(
            items,
            key=lambda item: time_minutes(item.get("time_of_day"))
            if time_minutes(item.get("time_of_day")) is not None
            else 1440,
        )
        for item in items:
            if time_minutes(item.get("time_of_day")) is None:
                issues.append(
                    issue(
                        "missing_schedule_time",
                        "high",
                        f"第 {day} 天的行程项 {item.get('item_id')} 缺少明确的 HH:MM 开始时间。",
                        item.get("item_id"),
                    )
                )
        for previous, current in zip(ordered, ordered[1:]):
            previous_start = time_minutes(previous.get("time_of_day"))
            current_start = time_minutes(current.get("time_of_day"))
            previous_duration = previous.get("duration_minutes")
            if (
                previous_start is not None
                and current_start is not None
                and isinstance(previous_duration, int)
                and current_start < previous_start + previous_duration
            ):
                issues.append(
                    issue(
                        "time_conflict",
                        "high",
                        f"第 {day} 天 {previous.get('item_id')} 与 {current.get('item_id')} 时间重叠。",
                        [previous.get("item_id"), current.get("item_id")],
                    )
                )
        total_minutes = sum(number(item.get("duration_minutes")) for item in items)
        if len(items) >= 5:
            issues.append(
                issue(
                    "pace_risk",
                    "medium",
                    f"第 {day} 天安排了 {len(items)} 个项目，可能过于紧凑。",
                    day,
                )
            )
        if total_minutes > 600:
            issues.append(
                issue(
                    "pace_risk",
                    "high",
                    f"第 {day} 天活动时长约 {total_minutes} 分钟，建议减少或拆分。",
                    day,
                )
            )
        categories = {str(item.get("category", "")).lower() for item in items}
        if not any("food" in c or "餐" in c or "restaurant" in c for c in categories):
            issues.append(
                issue(
                    "meal_gap",
                    "low",
                    f"第 {day} 天没有明确餐饮安排。",
                    day,
                )
            )

    itinerary_cost = sum(number(item.get("estimated_cost")) for item in itinerary)
    recommendation_cost = sum(number(item.get("estimated_cost")) for item in recommendations)
    budget_total = sum(number(item.get("amount")) for item in budget_items)
    tracked_total = max(itinerary_cost, budget_total)
    if budget and tracked_total > budget:
        issues.append(
            issue(
                "budget_overrun",
                "high",
                f"已跟踪费用 {tracked_total} 超过预算 {budget}。",
                None,
            )
        )
    elif budget and (itinerary_cost + recommendation_cost) > budget:
        issues.append(
            issue(
                "budget_risk",
                "medium",
                f"行程和候选项估算合计 {itinerary_cost + recommendation_cost}，接近或超过预算 {budget}。",
                None,
            )
        )

    booking_items = [
        item for item in itinerary if item.get("booking_required") and not item.get("notes")
    ]
    if booking_items:
        issues.append(
            issue(
                "booking_risk",
                "medium",
                f"{len(booking_items)} 个需要预订的项目缺少预订备注。",
                [item.get("item_id") for item in booking_items],
            )
        )

    if not itinerary:
        suggestions.append("当前还没有日程项，可以先补齐每天的主线活动。")
    if issues:
        suggestions.append("优先处理 high 严重程度的问题，再调整 medium/low 项。")
    if any(item["type"] == "pace_risk" for item in issues):
        suggestions.append("把高强度日程拆成上午、下午、晚上三段，并保留通勤和休息缓冲。")
    if any(item["type"].startswith("budget") for item in issues):
        suggestions.append("把酒店、交通、门票和餐饮拆成预算项，避免只估算活动费用。")

    return {
        "success": True,
        "plan_id": plan.get("plan_id"),
        "status": "ok" if not issues else "needs_attention",
        "trip_days": trip_days,
        "metrics": {
            "itinerary_item_count": len(itinerary),
            "recommendation_count": len(recommendations),
            "budget_item_count": len(budget_items),
            "estimated_itinerary_cost": itinerary_cost,
            "estimated_recommendation_cost": recommendation_cost,
            "budget_item_total": budget_total,
            "budget": budget,
        },
        "issues": issues,
        "suggestions": suggestions,
    }


def issue(kind: str, severity: str, message: str, target: Any) -> dict[str, Any]:
    """统一问题结构，便于 API、前端和模型消费。"""
    return {
        "type": kind,
        "severity": severity,
        "message": message,
        "target": target,
    }


def date_span_days(start_value: Any, end_value: Any) -> int | None:
    """把 ISO 日期范围转换为包含首尾日期的旅行天数。"""
    try:
        start = date.fromisoformat(str(start_value))
        end = date.fromisoformat(str(end_value))
    except ValueError:
        return None
    return (end - start).days + 1


def number(value: Any) -> float:
    """安全转换金额或时长；无效值按 0 处理，避免审计中断。"""
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0
