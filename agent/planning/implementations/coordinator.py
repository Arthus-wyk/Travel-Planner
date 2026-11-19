"""Apply planning actions before graph routing."""
from __future__ import annotations
from copy import deepcopy
from service.travel.planning import (
    add_manual_activity,
    move_activity,
    record_purchase,
    select_activities,
    select_candidates,
    update_activity_details,
    update_route_mode,
    update_transport_stops,
    update_day_departure,
    edit_review_place,
    complete_review_lodging,
    remove_review_activity,
    MOVABLE_CATEGORIES,
    _positive_money,
)
from service.travel.map_planning import resolve_activity_place
from agent.planning.schemas.state import PlanningState


def coordinator(state: PlanningState) -> dict:
    """入口节点：校验用户操作并更新计划基础数据，交由路由函数选择后续节点。"""
    action, payload, plan = state["action"], state.get("payload") or {}, state["plan"]
    planning = plan.get("planning") or {}
    defaults = {"stage": "requirements", "activities": [], "hotel_candidates": [],
                "transport_candidates": [], "selected_transport": {}}
    if not defaults.keys() <= planning.keys():
        plan = {**plan, "planning": {**defaults, **planning}}
    if (action in {"add_activity", "resolve_place", "candidates"} and "choices" in payload
            or action == "candidates" and "text" in payload):
        plan = _save_choices(plan, payload)
    if action == 'review_changes':
        operations = payload.get('operations')
        if plan.get('planning', {}).get('stage') != 'review' or not isinstance(operations, list) or not 1 <= len(operations) <= 1000:
            raise ValueError('行程修改格式无效，请在审核阶段保存')
        if "day_departures" not in plan["planning"]:
            departures = {}
            for item in plan["itinerary"]:
                if item.get("anchor_role") == "start" or item["category"] in MOVABLE_CATEGORIES:
                    departures.setdefault(str(item["day"]), item["time_of_day"])
            plan = {**plan, "planning": {**plan["planning"], "day_departures": departures}}
        original_items = {x['item_id']: x for x in plan.get('itinerary', []) + plan.get('planning', {}).get('unscheduled_items', [])}
        def current_id(item_id):
            if not isinstance(item_id, str):
                raise ValueError('修改引用了无效地点')
            items = plan['itinerary'] + plan['planning'].get('unscheduled_items', [])
            if any(x['item_id'] == item_id for x in items):
                return item_id
            original = original_items.get(item_id)
            if not original or original["category"] not in MOVABLE_CATEGORIES:
                raise ValueError('修改引用了无效地点')
            return next((x['item_id'] for x in items if x.get('reference_id') == original['reference_id']
                         and x['category'] == original['category']), item_id)
        for operation in operations:
            if not isinstance(operation, dict) or operation.get('action') not in {'move_activity', 'update_activity', 'route_mode', 'transport_stops', 'update_budget', 'acknowledge_purchase', 'day_departure', 'review_place', 'complete_lodging', 'remove_activity'} or not isinstance(operation.get('payload'), dict):
                raise ValueError('不支持的行程修改')
            values = deepcopy(operation['payload'])
            for key in ('item_id', 'before_item_id', 'origin_item_id', 'destination_item_id'):
                if values.get(key):
                    values[key] = current_id(values[key])
            name = operation['action']
            if name == 'review_place':
                plan = edit_review_place(plan, values)
            elif name == 'complete_lodging':
                plan = complete_review_lodging(plan, values.get('activity_id'))
            elif name == 'remove_activity':
                plan = remove_review_activity(plan, values.get('item_id'))
            else:
                plan = coordinator({'plan': plan, 'action': name, 'payload': values})['plan']
            if name == 'review_place' and not values.get('replace'):
                original_items.update((x['item_id'], x) for x in plan['itinerary']
                                      if x.get('reference_id') == values['activity_id'])
    elif action == "choose_candidates":
        plan = select_candidates(plan, payload)
    elif action == "choose_activities":
        plan = _save_choices(plan, payload)
    elif action == "add_activity":
        plan = add_manual_activity(plan, payload.get("name", ""), payload.get("location", ""), payload.get("category", "attraction"))
    elif action == "move_activity":
        plan = move_activity(plan, payload.get("item_id", ""), payload.get("target_day"), payload.get("before_item_id"))
    elif action == "update_activity":
        plan = update_activity_details(plan, payload.get("activity_id", ""), payload)
    elif action == "route_modes":
        selections = payload.get("selections")
        if not isinstance(selections, list) or len(selections) > len(plan.get("routes", [])):
            raise ValueError("交通选择格式无效")
        for selection in selections:
            if not isinstance(selection, dict):
                raise ValueError("交通选择格式无效")
            plan = update_route_mode(plan, selection.get("origin_item_id"), selection.get("destination_item_id"), selection.get("travel_mode"))
    elif action == "route_mode":
        plan = update_route_mode(plan, payload.get("origin_item_id"), payload.get("destination_item_id"), payload.get("travel_mode"))
    elif action == "transport_stops":
        plan = update_transport_stops(plan, payload.get("item_id"), payload.get("departure_location"), payload.get("arrival_location"))
    elif action == "day_departure":
        plan = update_day_departure(plan, payload.get("day"), payload.get("time"))
    elif action == "resolve_place":
        if plan.get("planning", {}).get("stage") not in {"activities", "review"}:
            raise ValueError("Return to place selection before changing a POI")
        plan = resolve_activity_place(plan, payload.get("activity_id"), payload.get("poi_id"))
        if plan["planning"]["stage"] == "review":
            plan["planning"]["needs_replan"] = True
    elif action == "exclude_activity":
        if plan.get("planning", {}).get("stage") != "review":
            raise ValueError("请在每日行程草稿中移除地点")
        activity_id = payload.get("activity_id")
        plan = select_activities(plan, {activity_id: "exclude"})
        plan["planning"]["unscheduled_items"] = [x for x in plan["planning"].get("unscheduled_items", []) if x.get("reference_id") != activity_id]
        plan["planning"]["unplaced_activities"] = [x for x in plan["planning"].get("unplaced_activities", []) if x != activity_id]
    elif action == "edit_places":
        plan = {**plan, "planning": {**plan["planning"], "stage": "activities"}}
    elif action == "purchase":
        if (plan.get("planning") or {}).get("stage") != "confirmed":
            raise ValueError("Confirm the itinerary before recording purchases")
        plan = record_purchase(plan, payload)
    elif action == "update_budget":
        amount = _positive_money(payload.get("budget"), "总预算")
        if abs(amount * 100 - round(amount * 100)) > 1e-7:
            raise ValueError("总预算最多保留两位小数")
        plan["budget"] = amount
    elif action == "acknowledge_purchase":
        if plan.get("planning", {}).get("stage") != "review":
            raise ValueError("请在路线审核时确认已购项目变更")
        record = next((x for x in plan["planning"].get("purchases", []) if x["reference_id"] == payload.get("reference_id")), None)
        if record is None:
            raise ValueError("未找到购买记录")
        record["change_acknowledged"] = True
    elif action == "optimize_routes":
        if plan["planning"]["stage"] != "review":
            raise ValueError("请在路线审核时重排行程")
    elif action == "recalculate_routes":
        if plan.get("planning", {}).get("stage") != "review":
            raise ValueError("请在路线审核时重新计算路线")
        # Rebuild adjacency as well as durations; do not regenerate chosen stops.
        by_day = {}
        old = {(r["origin_item_id"], r["destination_item_id"]): r for r in plan.get("routes", [])}
        for item in plan.get("itinerary", []):
            by_day.setdefault(item["day"], []).append(item)
        routes = []
        for items in by_day.values():
            if "day_departures" not in plan["planning"]:
                items.sort(key=lambda x: x["time_of_day"])
            for origin, destination in zip(items, items[1:]):
                pair = (origin["item_id"], destination["item_id"])
                routes.append({**old.get(pair, {}), "origin_item_id": pair[0], "destination_item_id": pair[1],
                               "origin": origin.get("arrival_location", origin["location"]), "destination": destination["location"],
                               "duration_minutes": old.get(pair, {}).get("duration_minutes", 30),
                               "travel_mode": old.get(pair, {}).get("travel_mode", "TRANSIT"),
                               "source": "provisional_estimate", "legs": []})
                routes[-1].pop("fare", None)
        plan["routes"] = routes
    elif action not in {"candidates", "guide", "activities", "draft", "generate_from_guide", "confirm"}:
        raise ValueError("Unknown planning action")
    if action in {"guide", "activities", "draft", "generate_from_guide", "confirm"}:
        planning = plan.get("planning") or {}
        transport = planning.get("selected_transport") or {}
        candidates = {(item.get("direction"), item.get("id")) for item in planning.get("transport_candidates", [])}
        if any(not transport.get(direction) or (direction, transport[direction]) not in candidates
               for direction in ("outbound", "return")):
            raise ValueError("请先选择往返交通")
    if action in {"guide", "activities", "draft", "generate_from_guide"}:
        plan = _save_choices(plan, payload)
    return {"plan": plan, "trace": ["coordinator"]}


def _save_choices(plan: dict, payload: dict) -> dict:
    fields = {key: payload[key] for key in ("categories", "selected_hotel", "details") if key in payload}
    updated = select_activities(plan, payload.get("choices", {}), **fields)
    if "text" in payload:
        text = payload["text"]
        if not isinstance(text, str) or len(text) > 20000:
            raise ValueError("攻略文字最长20000字")
        updated["planning"]["draft_text"] = text
    return updated


def after_coordinator(state: PlanningState) -> str:
    if (state["action"] == "review_changes" and state["plan"]["planning"].get("optimized_timetable")
            and all(operation["action"] in {"update_budget", "acknowledge_purchase"}
                    for operation in state["payload"]["operations"])):
        return "audit_agent"
    return {
        "candidates": "transport_agent", "guide": "guide_agent",
        "activities": "activity_agent", "choose_candidates": "hotel_agent",
        "generate_from_guide": "generate_guide", "draft": "prepare_draft", "add_activity": "place_agent",
        "exclude_activity": "audit_agent",
        "resolve_place": "audit_agent" if state["plan"]["planning"]["stage"] == "review" else "done",
        "confirm": "audit_agent", "purchase": "audit_agent",
        "move_activity": "route_agent", "update_activity": "route_agent", "route_mode": "route_agent", "route_modes": "route_agent", "transport_stops": "route_agent",
        "update_budget": "audit_agent", "acknowledge_purchase": "audit_agent", "recalculate_routes": "route_agent",
        "optimize_routes": "route_agent", 'review_changes': 'route_agent',
        "day_departure": "schedule_agent",
    }.get(state["action"], "done")
