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
    guide_results = []

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
            key=lambda item: (
                time_minutes(item.get("time_of_day"))
                if time_minutes(item.get("time_of_day")) is not None
                else 1440
            ),
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

    planning = plan.get("planning") or {}
    if planning.get("needs_replan"):
        issues.append(issue("itinerary_outdated", "high", "旅行基础信息已变化，请重新查询候选并生成行程。", None))
    if planning.get("stage") in {"review", "confirmed"}:
        from service.travel.planning import budget_summary

        home_location = planning.get("home_location") or plan.get("departure_city")
        selected_hotel = next((x for x in planning.get("hotel_candidates") or [] if x.get("id") == planning.get("selected_hotel")), None)
        hotel_location = (selected_hotel or {}).get("location")
        if trip_days and trip_days > 1 and not selected_hotel:
            issues.append(issue("missing_hotel", "high", "尚未确定住宿，请选择具体酒店后重新生成。", None))
        unresolved_places = [x["id"] for x in planning.get("activities", [])
                             if x.get("choice") != "exclude" and x.get("category") != "hotel" and not x.get("place")]
        if planning.get("guide_auto") and unresolved_places:
            issues.append(issue("unresolved_places", "high", "部分地点位置待确定，请选择候选或移除后重新生成。", unresolved_places))
        for day in range(1, (trip_days or 0) + 1):
            ordered = sorted(by_day.get(day, []), key=lambda x: time_minutes(x.get("time_of_day")) if time_minutes(x.get("time_of_day")) is not None else 1440)
            expected_start = "home_anchor" if day == 1 else "hotel_anchor"
            expected_end = "home_anchor" if day == trip_days else "hotel_anchor"
            start_location = home_location if day == 1 else hotel_location
            end_location = home_location if day == trip_days else hotel_location
            if not ordered or ordered[0].get("category") != expected_start or ordered[0].get("anchor_role") != "start" or ordered[0].get("location") != start_location:
                issues.append(issue("missing_start_anchor", "high", f"第 {day} 天没有从指定的家或酒店出发。", day))
            if not ordered or ordered[-1].get("category") != expected_end or ordered[-1].get("anchor_role") != "end" or ordered[-1].get("location") != end_location:
                issues.append(issue("missing_end_anchor", "high", f"第 {day} 天没有返回指定的家或酒店。", day))
            route_pairs = {(x.get("origin_item_id"), x.get("destination_item_id")): x for x in plan.get("routes") or []}
            for previous, current in zip(ordered, ordered[1:]):
                route = route_pairs.get((previous.get("item_id"), current.get("item_id")))
                if route is None:
                    issues.append(issue("missing_route", "high", f"第 {day} 天 {previous.get('title')} 至 {current.get('title')} 缺少路线。", [previous.get("item_id"), current.get("item_id")]))
                else:
                    if route.get("origin") != previous.get("arrival_location", previous.get("location")) or route.get("destination") != current.get("location"):
                        issues.append(issue("stale_route", "high", f"第 {day} 天 {previous.get('title')} 至 {current.get('title')} 的路线地点已变化，请重新计算。", [previous.get("item_id"), current.get("item_id")]))
                    previous_start = time_minutes(previous.get("time_of_day"))
                    current_start = time_minutes(current.get("time_of_day"))
                    travel_minutes = number(route.get("duration_minutes"))
                    if previous_start is not None and current_start is not None and previous_start + number(previous.get("duration_minutes")) + travel_minutes > current_start:
                        issues.append(issue("transport_buffer", "high", f"第 {day} 天 {previous.get('title')} 至 {current.get('title')} 的交通缓冲不足。", [previous.get("item_id"), current.get("item_id")]))
        if planning.get("unplaced_activities"):
            missing = set(planning["unplaced_activities"])
            must_missing = any(x["id"] in missing and x.get("choice") == "must" for x in planning.get("activities", []))
            issues.append(issue("unplaced_activities", "high" if must_missing else "medium", "部分已选地点仍在未确定行程中，请查看原因或返回地点选择调整。", planning["unplaced_activities"]))
        guide_results = _check_guide_constraints(plan)
        for result in guide_results:
            if not result["satisfied"]:
                issues.append(issue("guide_constraint", "high" if result["hard"] else "medium", result["reason"], result["activity_ids"]))
        unresolved = [x["id"] for x in planning.get("activities") or []
                      if x.get("source") == "simulated_suggestion" and x.get("choice") != "exclude"]
        if unresolved:
            issues.append(issue("unresolved_activity", "high", "主题推荐尚不是具体地点，请选择具体地点或排除这些推荐后重新规划。", unresolved))
        if any(x.get("source") == "provisional_estimate" for x in plan.get("routes") or []):
            severity = "high" if planning.get("guide_auto") else "medium"
            issues.append(issue("route_estimate", severity, "地点间交通时间为临时估算，出行前请核对实时路线。", None))
        guided_budget = budget_summary(plan)
        for item in guided_budget["items"]:
            if item.get("selection_changed"):
                issues.append(issue("purchased_item_changed", "high", f"已购买的 {item['label']} 不在当前选择中，请确认改签或保留。", item["reference_id"]))
        if guided_budget["remaining"] < 0:
            issues.append(issue("budget_overrun", "high", f"当前估算与已付金额合计 {guided_budget['estimated_total']}，超过预算 {budget}。", None))
        if guided_budget["has_unknown_ticket_costs"]:
            issues.append(issue("unknown_ticket_cost", "medium", "部分活动门票价格未知，预算可能继续增加。", None))
        if guided_budget.get("has_unknown_transport_costs"):
            issues.append(issue("unknown_transport_cost", "medium", "自驾燃油和路桥费用待补充，请按实际车辆费用记录，当前预算尚不完整。", None))
        if guided_budget["has_possible_duplicates"]:
            issues.append(issue("budget_item_overlap", "medium", "聊天中添加的费用已计入预算，请核对是否与机酒、门票或每日预算重复。", None))

    itinerary_cost = sum(number(item.get("estimated_cost")) for item in itinerary)
    recommendation_cost = sum(
        number(item.get("estimated_cost")) for item in recommendations
    )
    budget_total = sum(number(item.get("amount")) for item in budget_items)
    tracked_min = max(itinerary_cost, budget_total)
    tracked_max = itinerary_cost + budget_total
    if planning.get("stage") in {"review", "confirmed"}:
        tracked_min = tracked_max = guided_budget["estimated_total"]
    elif budget and tracked_min > budget:
        issues.append(
            issue(
                "budget_overrun",
                "high",
                f"已跟踪费用至少 {tracked_min}，超过预算 {budget}。",
                None,
            )
        )
    elif budget and tracked_max > budget:
        issues.append(
            issue(
                "budget_risk",
                "medium",
                f"行程估算与预算项合计最多 {tracked_max}，可能超过预算 {budget}；请核对是否重复记账。",
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
        item
        for item in itinerary
        if item.get("booking_required") and not item.get("notes")
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
        suggestions.append(
            "把高强度日程拆成上午、下午、晚上三段，并保留通勤和休息缓冲。"
        )
    if any(item["type"].startswith("budget") for item in issues):
        suggestions.append(
            "核对日程估算与预算项是否重复，并补齐酒店、交通、门票和餐饮费用。"
        )

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
            "tracked_cost_range": {"min": tracked_min, "max": tracked_max},
            "budget": budget,
        },
        "issues": issues,
        "suggestions": suggestions,
        "guide_constraints": guide_results,
    }


def _check_guide_constraints(plan: dict) -> list[dict]:
    planning = plan.get("planning", {})
    activities = {x["id"]: x for x in planning.get("activities", [])}
    visits = {x.get("reference_id"): x for x in plan.get("itinerary", []) if x.get("category") in {"attraction", "food", "other"}}
    constraints = list(planning.get("guide_constraints", []))
    for activity in activities.values():
        for field, kind in (("day", "day"), ("time_window", "time_window")):
            if activity.get(field):
                constraints.append({"type": kind, field: activity[field], "activity_ids": [activity["id"]],
                                    "source": activity.get("note") or activity["name"], "hard": True})
    results = []
    for constraint in constraints:
        ids = constraint.get("activity_ids", [])
        active = [key for key in ids if key in activities and activities[key].get("choice") != "exclude"]
        if ids and not active:
            continue
        kind = constraint["type"]
        scheduled = [visits.get(key) for key in active]
        satisfied = bool(scheduled) and all(x is not None for x in scheduled)
        if kind == "hotel_change":
            satisfied = False
        elif satisfied and kind == "day":
            satisfied = all(x["day"] == constraint.get("day") for x in scheduled)
        elif satisfied and kind == "time_window":
            bounds = constraint.get("time_window", "").split("-")
            satisfied = len(bounds) == 2 and all(bounds[0] <= x["time_of_day"] <= bounds[1] for x in scheduled)
        elif satisfied and kind == "duration":
            satisfied = all(x.get("duration_minutes") == constraint.get("duration_minutes") for x in scheduled)
        elif satisfied and kind == "order":
            ordering = [(x["day"], x["time_of_day"]) for x in scheduled]
            satisfied = ordering == sorted(ordering) and len(set(ordering)) == len(ordering)
        names = "、".join(activities[key]["name"] for key in active)
        label = {"day": "指定日期", "time_window": "指定时间段", "duration": "停留时长", "order": "先后顺序", "hotel_change": "更换住宿酒店"}.get(kind, kind)
        results.append({"type": kind, "activity_ids": active, "source": constraint.get("source", ""),
                        "satisfied": satisfied, "hard": constraint.get("hard", True), "verified": True,
                        "reason": f"{names or '攻略'}的{label}{'已满足' if satisfied else '未满足，请调整安排或修改要求'}"})
    return results


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
