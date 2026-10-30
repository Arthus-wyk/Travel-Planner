"""Main/subagent LangGraph for the form-led planning flow.

The coordinator owns decisions; specialist nodes only return structured plan
proposals. The HTTP layer persists a completed graph result once.
"""

from __future__ import annotations

import base64
from contextvars import ContextVar
from copy import deepcopy
from hashlib import sha256
import json
import logging
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from agent.itinerary import generate_itinerary_proposal

from service.config import AMAP_API_KEY
from service.mcp.server import amap_compute_route, amap_search_hotels, amap_search_places
from service.travel.guide import GuideNeedsInput, recognize_guide
from service.travel.place_matching import search_and_match
from service.travel.itinerary_audit import audit_plan
from service.travel.planning import (
    add_manual_activity, build_activities, build_candidates, draft_itinerary,
    move_activity, record_purchase, select_activities, select_candidates,
    update_activity_details,
    update_route_mode, apply_route_times, schedule_itinerary, update_transport_stops,
    build_hotel_activities, _selected, _positive_money,
)
from service.travel.map_planning import resolve_activity_place, locate_activities, has_coordinates

generation_job_id = ContextVar("generation_job_id", default="-")


class PlanningState(TypedDict, total=False):
    resume_stage: str
    plan: dict[str, Any]
    action: str
    payload: dict[str, Any]
    guide_text: str
    audit: dict[str, Any]
    trace: list[str]
    attempts: int
    format_attempts: int
    revision_attempts: int
    feedback: list
    proposal_error: str
    input_hash: str
    forced_unscheduled: list


def _hotel_places(plan: dict[str, Any]) -> list[dict]:
    if not AMAP_API_KEY:
        return []
    result = amap_search_hotels(plan["destination"], max_results=3)
    return result.get("places", []) if result.get("success") else []


def _activity_places(plan: dict[str, Any]) -> list[dict]:
    places = []
    if AMAP_API_KEY:
        for interest in (plan.get("interests") or ["景点"])[:3]:
            result = amap_search_places(f"{plan['destination']} {interest}", max_results=3)
            if result.get("success"):
                places.extend(result.get("places", []))
    if not places:
        for interest in (plan.get("interests") or ["城市漫步", "当地文化"])[:3]:
            places.append({"name": f"{plan['destination']}·{interest}",
                           "address": plan["destination"], "source": "simulated_suggestion",
                           "note": "主题建议，请选择具体地点后再确认路线"})
    return places


def coordinator(state: PlanningState) -> dict:
    """入口节点：校验用户操作并更新计划基础数据，交由路由函数选择后续节点。"""
    action, payload, plan = state["action"], state.get("payload") or {}, state["plan"]
    if action in {"add_activity", "resolve_place", "candidates"} and "choices" in payload:
        plan = _save_choices(plan, payload)
    if action == "choose_candidates":
        plan = select_candidates(plan, payload)
    elif action == "choose_activities":
        plan = _save_choices(plan, payload)
    elif action == "add_activity":
        plan = add_manual_activity(plan, payload.get("name", ""), payload.get("location", ""), payload.get("category", "attraction"))
    elif action == "move_activity":
        plan = move_activity(plan, payload.get("item_id", ""), payload.get("target_day"), payload.get("before_item_id"))
    elif action == "update_activity":
        plan = update_activity_details(plan, payload.get("activity_id", ""), payload)
    elif action == "route_mode":
        plan = update_route_mode(plan, payload.get("origin_item_id"), payload.get("destination_item_id"), payload.get("travel_mode"))
    elif action == "transport_stops":
        plan = update_transport_stops(plan, payload.get("item_id"), payload.get("departure_location"), payload.get("arrival_location"))
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
            items.sort(key=lambda x: x["time_of_day"])
            for origin, destination in zip(items, items[1:]):
                pair = (origin["item_id"], destination["item_id"])
                routes.append({**old.get(pair, {}), "origin_item_id": pair[0], "destination_item_id": pair[1],
                               "origin": origin.get("arrival_location", origin["location"]), "destination": destination["location"],
                               "duration_minutes": old.get(pair, {}).get("duration_minutes", 30),
                               "travel_mode": old.get(pair, {}).get("travel_mode", "TRANSIT"),
                               "source": "provisional_estimate", "legs": []})
        plan["routes"] = routes
    elif action not in {"candidates", "guide", "activities", "draft", "generate_from_guide", "confirm"}:
        raise ValueError("Unknown planning action")
    if action in {"guide", "activities", "draft", "generate_from_guide", "confirm"} and not (plan.get("planning") or {}).get("selected_transport"):
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
    return {
        "candidates": "transport_agent", "guide": "guide_agent",
        "activities": "activity_agent", "choose_candidates": "hotel_agent",
        "generate_from_guide": "generate_guide", "draft": "prepare_draft", "add_activity": "place_agent",
        "exclude_activity": "audit_agent",
        "resolve_place": "audit_agent" if state["plan"]["planning"]["stage"] == "review" else "done",
        "confirm": "audit_agent", "purchase": "audit_agent",
        "move_activity": "route_agent", "update_activity": "route_agent", "route_mode": "route_agent", "transport_stops": "route_agent",
        "update_budget": "audit_agent", "acknowledge_purchase": "audit_agent", "recalculate_routes": "route_agent",
    }.get(state["action"], "done")


def transport_agent(state: PlanningState) -> dict:
    """生成往返交通候选及初始规划选项。"""
    return {"plan": build_candidates(state["plan"]), "trace": state["trace"] + ["transport_agent"]}


def hotel_agent(state: PlanningState) -> dict:
    """搜索住宿候选，并把酒店加入可选择的活动列表。"""
    places = _hotel_places(state["plan"])
    plan = build_hotel_activities(state["plan"], places or None)
    return {"plan": plan, "trace": state["trace"] + ["hotel_agent"]}


def guide_agent(state: PlanningState) -> dict:
    """读取攻略文字；上传图片时先识别图片中的文字。"""
    payload = state.get("payload") or {}
    if payload.get("image_base64"):
        try:
            image = base64.b64decode(payload["image_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid guide image encoding") from exc
        text = recognize_guide(image)
    else:
        text = str(payload.get("text") or "")
    if not text.strip():
        raise ValueError("Guide image or corrected text is required")
    return {"guide_text": text, "trace": state["trace"] + ["guide_agent"]}


def activity_agent(state: PlanningState) -> dict:
    """从攻略和兴趣整理活动、地点及建议，并保存攻略草稿。"""
    saved = state["plan"].get("planning", {})
    text = state.get("guide_text", state.get("payload", {}).get("text", saved.get("draft_text", saved.get("guide_text", ""))))
    plan = build_activities(state["plan"], text, _activity_places(state["plan"]))
    plan["planning"]["draft_text"] = text
    return {"plan": plan, "trace": state["trace"] + ["activity_agent"]}


def place_agent(state: PlanningState) -> dict:
    """为活动定位地图地点；未配置高德密钥时保留原计划。"""
    plan = locate_activities(state["plan"]) if AMAP_API_KEY else state["plan"]
    return {"plan": plan, "trace": state["trace"] + ["place_agent"]}


def prepare_draft(state: PlanningState) -> dict:
    """准备行程输入：同步攻略、筛出未定位地点并重置本轮提案次数与反馈。"""
    plan, payload = state["plan"], state.get("payload", {})
    text = plan["planning"].get("guide_text", "") if state["action"] == "generate_from_guide" else payload.get("text", plan["planning"].get("guide_text", ""))
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("攻略文字最长20000字")
    if text != plan["planning"].get("guide_text", ""):
        plan = build_activities(plan, text)
        if AMAP_API_KEY:
            plan = locate_activities(plan)
    _, _, hotel = _selected(plan)
    if hotel and not has_coordinates(hotel.get("place")) and state["action"] != "generate_from_guide":
        raise ValueError("请先在第二步确认住宿酒店的具体位置")
    forced = []
    for item in plan["planning"].get("activities", []):
        if item.get("choice") == "exclude" or item.get("category") == "hotel":
            continue
        if not has_coordinates(item.get("place")):
            if item.get("choice") == "must" and state["action"] != "generate_from_guide":
                raise ValueError(f"请先在第二步确认 {item['name']} 的具体位置")
            forced.append({"activity_id": item["id"], "reason": "位置尚未确认，暂不安排"})
    snapshot = {key: plan.get(key) for key in ("destination", "departure_city", "start_date", "end_date", "travelers", "budget", "pace", "interests", "constraints")}
    snapshot["planning"] = {key: value for key, value in plan["planning"].items() if key not in {"generation", "stage", "unscheduled_items", "unplaced_activities", "needs_replan"}}
    digest = sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"plan": plan, "attempts": 0, "format_attempts": 0, "revision_attempts": 0,
            "feedback": [], "proposal_error": "", "forced_unscheduled": forced,
            "input_hash": digest, "trace": state["trace"] + ["prepare_draft"]}


def itinerary_agent(state: PlanningState) -> dict:
    """让模型安排每日地点；格式错误写入反馈并重试，最多生成三次提案。"""
    attempts = state.get("attempts", 0) + 1
    format_attempts = state.get("format_attempts", 0) + 1
    model_plan = deepcopy(state["plan"])
    forced = state.get("forced_unscheduled", [])
    forced_ids = {x["activity_id"] for x in forced}
    for activity in model_plan["planning"].get("activities", []):
        if activity["id"] in forced_ids:
            activity["choice"] = "exclude"
    try:
        proposal = generate_itinerary_proposal(model_plan, state.get("feedback", []))
    except ValueError as exc:
        if format_attempts >= 3:
            raise ValueError(f"行程 Agent 生成失败，请重试：{exc}") from exc
        return {"attempts": attempts, "format_attempts": format_attempts, "proposal_error": str(exc),
                "feedback": [{"type": "invalid_proposal", "message": str(exc)}],
                "trace": state["trace"] + ["itinerary_agent"]}
    proposal["unscheduled"] += forced
    plan = draft_itinerary(state["plan"], proposal)
    plan["planning"]["generation"].update(input_hash=state["input_hash"], revisions=attempts - 1)
    return {"plan": plan, "attempts": attempts, "format_attempts": 0,
            "proposal_error": "", "trace": state["trace"] + ["itinerary_agent"]}


def after_itinerary(state: PlanningState) -> str:
    return "itinerary_agent" if state.get("proposal_error") else "route_agent"


def route_agent(state: PlanningState) -> dict:
    """补全相邻行程的路线信息，查询交通时间并更新行程时间。"""
    plan = state["plan"]
    by_id = {item["item_id"]: item for item in plan["itinerary"]}
    for route in plan["routes"]:
        origin, destination = by_id[route["origin_item_id"]], by_id[route["destination_item_id"]]
        route["intercity"] = False
        route["station_transfer"] = "intercity_transport" in {origin["category"], destination["category"]}
        route["origin"] = origin.get("arrival_location", origin["location"])
        route["destination"] = destination["location"]
        route.setdefault("travel_mode", "TRANSIT")
    if AMAP_API_KEY:
        for route in plan["routes"]:
            origin = by_id[route["origin_item_id"]]
            destination = by_id[route["destination_item_id"]]
            def waypoint(item, arriving=False):
                if arriving and item.get("arrival_location"):
                    return item["arrival_location"]
                place = item.get("place") or {}
                return f"{place['longitude']},{place['latitude']}" if place else item["location"]
            result = amap_compute_route(waypoint(origin, arriving=True), waypoint(destination), travel_mode=route["travel_mode"])
            if not result.get("success"):
                route.update(source="provisional_estimate", duration_minutes=30, legs=[], error=result.get("message", "路线计算失败"))
                continue
            duration = result.get("duration") or ""
            if isinstance(duration, str) and duration.endswith("s"):
                try:
                    route["duration_minutes"] = (int(float(duration[:-1])) + 59) // 60
                    route["distance_meters"] = result.get("distance_meters")
                    route["source"] = "高德地图"
                    route["legs"] = result.get("legs", [])
                    if route["station_transfer"] and any(x["category"] == "intercity_transport" and not x.get("station_confirmed") for x in (origin, destination)):
                        route["source"] = "provisional_estimate"
                        route["error"] = "尚未确认具体车站或机场，接驳按城市位置估算，请填写出发站和到达站。"
                        route["legs"] = []
                    else:
                        route.pop("error", None)
                except ValueError:
                    pass
    if state["action"] == "generate_from_guide" or state["action"] == "draft":
        return {"plan": plan, "trace": state["trace"] + ["route_agent"]}
    return {"plan": apply_route_times(plan), "trace": state["trace"] + ["route_agent"]}


def schedule_agent(state: PlanningState) -> dict:
    """根据已查询路线顺排行程时间；固定车次保持原时刻。"""
    return {"plan": schedule_itinerary(state["plan"]), "trace": state["trace"] + ["schedule_agent"]}


def audit_agent(state: PlanningState) -> dict:
    """检查时间、预算和攻略约束；将问题作为下一轮提案的反馈。"""
    plan = state["plan"]
    if state["action"] == "confirm" and not plan.get("itinerary"):
        raise ValueError("Generate an itinerary before confirmation")
    audit = audit_plan(plan)
    if plan.get("planning", {}).get("generation") is not None:
        plan["planning"]["generation"]["constraint_results"] = audit.get("guide_constraints", [])
    if state["action"] == "confirm":
        if any(item["severity"] == "high" for item in audit["issues"]):
            raise ValueError("Resolve high-severity itinerary issues before confirmation")
        plan = {**plan, "planning": {**plan["planning"], "stage": "confirmed"}}
    return {"plan": plan, "audit": audit, "feedback": audit["issues"],
            "revision_attempts": state.get("revision_attempts", 0) + 1,
            "trace": state["trace"] + ["audit_agent"]}


def after_audit(state: PlanningState) -> str:
    repairable = {"time_conflict", "transport_buffer", "guide_constraint", "unplaced_activities", "budget_overrun"}
    if state["action"] in {"draft", "generate_from_guide"} and state.get("revision_attempts", 0) < 3 and any(
        x["severity"] == "high" and x["type"] in repairable for x in state.get("audit", {}).get("issues", [])
    ):
        return "itinerary_agent"
    return "done"


def generate_guide(state: PlanningState) -> dict:
    """自动生成入口：识别攻略并提取活动；没有可识别地点时要求人工补充。"""
    text = guide_agent(state)["guide_text"]
    plan = build_activities(state["plan"], text)
    if not plan["planning"].get("guide_places"):
        raise GuideNeedsInput("攻略中没有可识别的地点，请补充具体地点名称，或手动添加地点后继续规划")
    plan["planning"]["guide_auto"] = True
    plan["planning"]["draft_text"] = text
    return {"plan": plan, "trace": state["trace"] + ["guide_agent", "activity_agent"]}


def search_places(state: PlanningState) -> dict:
    """批量搜索攻略地点并匹配地图候选，记录定位状态。"""
    return {"plan": search_and_match(state["plan"]), "trace": state["trace"] + ["place_search_agent", "place_match_agent"]}


def default_choices(state: PlanningState) -> dict:
    """从已定位的住宿候选中选择默认酒店，并记录自动选择的假设。"""
    plan = deepcopy(state["plan"])
    planning = plan["planning"]
    excluded = {x["id"] for x in planning.get("activities", []) if x.get("choice") == "exclude"}
    hotels = [x for x in planning.get("hotel_candidates", []) if has_coordinates(x.get("place")) and x["id"] not in excluded]
    chosen = next((x for x in hotels if x["id"] == planning.get("selected_hotel")), None)
    if not chosen and hotels:
        guide_ids = {x["id"] for x in planning.get("activities", []) if x.get("source") == "guide_text"}
        chosen = sorted(hotels, key=lambda x: (x["id"] not in guide_ids, x.get("estimated_total") or 0, x["id"]))[0]
        planning["lodging_assumption"] = "按攻略住宿和已定位候选自动建议住宿，可修改"
    planning["selected_hotel"] = chosen["id"] if chosen else None
    return {"plan": plan, "trace": state["trace"] + ["default_choices"]}


def _logged_node(name, node):
    def redact(value):
        if isinstance(value, dict):
            return {key: "[redacted]" if any(word in key.lower() for word in
                    ("base64", "api_key", "password", "secret", "token", "authorization"))
                    else redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        return value

    def run(state):
        result = node(state)
        logging.getLogger("travel.generation").info("job_id=%s node=%s result=%s", generation_job_id.get(), name, json.dumps(redact(result), ensure_ascii=False, default=str))
        return result

    return run


def build_planning_graph():
    graph = StateGraph(PlanningState)
    for name, node in {
        "generate_guide": generate_guide, "search_places": search_places, "default_choices": default_choices,
        "coordinator": coordinator, "transport_agent": transport_agent,
        "hotel_agent": hotel_agent, "guide_agent": guide_agent,
        "activity_agent": activity_agent, "route_agent": route_agent, "schedule_agent": schedule_agent,
        "audit_agent": audit_agent,
        "place_agent": place_agent, "prepare_draft": prepare_draft, "itinerary_agent": itinerary_agent,
    }.items():
        graph.add_node(name, _logged_node(name, node))
    graph.add_conditional_edges(START, lambda state: state.get("resume_stage", "coordinator"),
        {name: name for name in ("coordinator", "generate_guide", "search_places", "default_choices", "prepare_draft", "itinerary_agent", "route_agent", "schedule_agent", "audit_agent")})
    graph.add_conditional_edges("coordinator", after_coordinator, {
        "transport_agent": "transport_agent", "guide_agent": "guide_agent",
        "activity_agent": "activity_agent", "route_agent": "route_agent",
        "audit_agent": "audit_agent", "done": END,
        "generate_guide": "generate_guide", "hotel_agent": "hotel_agent", "prepare_draft": "prepare_draft", "place_agent": "place_agent",
    })
    graph.add_edge("generate_guide", "search_places")
    graph.add_edge("search_places", "default_choices")
    graph.add_edge("default_choices", "prepare_draft")
    graph.add_edge("transport_agent", END)
    graph.add_edge("hotel_agent", "activity_agent")
    graph.add_edge("guide_agent", "activity_agent")
    graph.add_edge("activity_agent", "place_agent")
    graph.add_edge("place_agent", END)
    graph.add_edge("prepare_draft", "itinerary_agent")
    graph.add_conditional_edges("itinerary_agent", after_itinerary, {"itinerary_agent": "itinerary_agent", "route_agent": "route_agent"})
    graph.add_edge("route_agent", "schedule_agent")
    graph.add_edge("schedule_agent", "audit_agent")
    graph.add_conditional_edges("audit_agent", after_audit, {"itinerary_agent": "itinerary_agent", "done": END})
    return graph.compile()


GRAPH = build_planning_graph()


def run_planning_action(plan: dict[str, Any], action: str, payload: dict[str, Any], progress=None, resume=None) -> PlanningState:
    initial = deepcopy(resume) if resume else {"plan": deepcopy(plan), "action": action, "payload": payload, "trace": []}
    if initial.get("resume_stage") == "done":
        return initial
    if progress is None:
        return GRAPH.invoke(initial)
    outcome = initial
    for outcome in GRAPH.stream(initial, stream_mode="values"):
        if outcome.get("trace") and (not resume or outcome["trace"] != resume.get("trace")):
            progress(outcome["trace"][-1], outcome)
    return outcome
