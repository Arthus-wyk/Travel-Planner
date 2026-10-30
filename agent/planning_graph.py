"""Main/subagent LangGraph for the form-led planning flow.

The coordinator owns decisions; specialist nodes only return structured plan
proposals. The HTTP layer persists a completed graph result once.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from agent.itinerary import generate_itinerary_proposal

from service.config import AMAP_API_KEY
from service.mcp.server import amap_compute_route, amap_search_hotels, amap_search_places
from service.travel.guide import recognize_guide
from service.travel.itinerary_audit import audit_plan
from service.travel.planning import (
    add_manual_activity, build_activities, build_candidates, draft_itinerary,
    move_activity, record_purchase, select_activities, select_candidates,
    update_activity_details,
    update_route_mode, apply_route_times, update_transport_stops,
    build_hotel_activities, _selected, _positive_money,
)
from service.travel.map_planning import resolve_activity_place, locate_activities, has_coordinates


class PlanningState(TypedDict, total=False):
    plan: dict[str, Any]
    action: str
    payload: dict[str, Any]
    guide_text: str
    audit: dict[str, Any]
    trace: list[str]
    attempts: int
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
        if plan.get("planning", {}).get("stage") != "activities":
            raise ValueError("Return to place selection before changing a POI")
        plan = resolve_activity_place(plan, payload.get("activity_id"), payload.get("poi_id"))
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
    elif action not in {"candidates", "guide", "activities", "draft", "confirm"}:
        raise ValueError("Unknown planning action")
    if action in {"guide", "activities", "draft", "confirm"} and not (plan.get("planning") or {}).get("selected_transport"):
        raise ValueError("请先选择往返交通")
    if action in {"guide", "activities", "draft"}:
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
        "draft": "prepare_draft", "add_activity": "place_agent",
        "confirm": "audit_agent", "purchase": "audit_agent",
        "move_activity": "route_agent", "update_activity": "route_agent", "route_mode": "route_agent", "transport_stops": "route_agent",
        "update_budget": "audit_agent", "acknowledge_purchase": "audit_agent", "recalculate_routes": "route_agent",
    }.get(state["action"], "done")


def transport_agent(state: PlanningState) -> dict:
    return {"plan": build_candidates(state["plan"]), "trace": state["trace"] + ["transport_agent"]}


def hotel_agent(state: PlanningState) -> dict:
    places = _hotel_places(state["plan"])
    plan = build_hotel_activities(state["plan"], places or None)
    return {"plan": plan, "trace": state["trace"] + ["hotel_agent"]}


def guide_agent(state: PlanningState) -> dict:
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
    saved = state["plan"].get("planning", {})
    text = state.get("guide_text", state.get("payload", {}).get("text", saved.get("draft_text", saved.get("guide_text", ""))))
    plan = build_activities(state["plan"], text, _activity_places(state["plan"]))
    plan["planning"]["draft_text"] = text
    return {"plan": plan, "trace": state["trace"] + ["activity_agent"]}


def place_agent(state: PlanningState) -> dict:
    plan = locate_activities(state["plan"]) if AMAP_API_KEY else state["plan"]
    return {"plan": plan, "trace": state["trace"] + ["place_agent"]}


def prepare_draft(state: PlanningState) -> dict:
    plan, payload = state["plan"], state.get("payload", {})
    text = payload.get("text", plan["planning"].get("guide_text", ""))
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("攻略文字最长20000字")
    if text != plan["planning"].get("guide_text", ""):
        plan = build_activities(plan, text)
        if AMAP_API_KEY:
            plan = locate_activities(plan)
    _, _, hotel = _selected(plan)
    if hotel and not has_coordinates(hotel.get("place")):
        raise ValueError("请先在第二步确认住宿酒店的具体位置")
    forced = []
    for item in plan["planning"].get("activities", []):
        if item.get("choice") == "exclude" or item.get("category") == "hotel":
            continue
        if not has_coordinates(item.get("place")):
            if item.get("choice") == "must":
                raise ValueError(f"请先在第二步确认 {item['name']} 的具体位置")
            forced.append({"activity_id": item["id"], "reason": "位置尚未确认，暂不安排"})
    snapshot = {key: plan.get(key) for key in ("destination", "departure_city", "start_date", "end_date", "travelers", "budget", "pace", "interests", "constraints")}
    snapshot["planning"] = {key: value for key, value in plan["planning"].items() if key not in {"generation", "stage", "unscheduled_items", "unplaced_activities", "needs_replan"}}
    digest = sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return {"plan": plan, "attempts": 0, "feedback": [], "proposal_error": "", "forced_unscheduled": forced,
            "input_hash": digest, "trace": state["trace"] + ["prepare_draft"]}


def itinerary_agent(state: PlanningState) -> dict:
    attempts = state.get("attempts", 0) + 1
    model_plan = deepcopy(state["plan"])
    forced = state.get("forced_unscheduled", [])
    forced_ids = {x["activity_id"] for x in forced}
    for activity in model_plan["planning"].get("activities", []):
        if activity["id"] in forced_ids:
            activity["choice"] = "exclude"
    try:
        proposal = generate_itinerary_proposal(model_plan, state.get("feedback", []))
    except ValueError as exc:
        if attempts >= 3:
            raise ValueError(f"行程 Agent 生成失败，请重试：{exc}") from exc
        return {"attempts": attempts, "proposal_error": str(exc),
                "feedback": [{"type": "invalid_proposal", "message": str(exc)}],
                "trace": state["trace"] + ["itinerary_agent"]}
    proposal["unscheduled"] += forced
    plan = draft_itinerary(state["plan"], proposal)
    plan["planning"]["generation"].update(input_hash=state["input_hash"], revisions=attempts - 1)
    return {"plan": plan, "attempts": attempts, "proposal_error": "", "trace": state["trace"] + ["itinerary_agent"]}


def after_itinerary(state: PlanningState) -> str:
    return "itinerary_agent" if state.get("proposal_error") else "route_agent"


def route_agent(state: PlanningState) -> dict:
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
    return {"plan": apply_route_times(plan), "trace": state["trace"] + ["route_agent"]}


def audit_agent(state: PlanningState) -> dict:
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
    return {"plan": plan, "audit": audit, "feedback": audit["issues"], "trace": state["trace"] + ["audit_agent"]}


def after_audit(state: PlanningState) -> str:
    repairable = {"time_conflict", "transport_buffer", "guide_constraint", "unplaced_activities", "budget_overrun"}
    if state["action"] == "draft" and state.get("attempts", 0) < 3 and any(
        x["severity"] == "high" and x["type"] in repairable for x in state.get("audit", {}).get("issues", [])
    ):
        return "itinerary_agent"
    return "done"


def build_planning_graph():
    graph = StateGraph(PlanningState)
    for name, node in {
        "coordinator": coordinator, "transport_agent": transport_agent,
        "hotel_agent": hotel_agent, "guide_agent": guide_agent,
        "activity_agent": activity_agent, "route_agent": route_agent,
        "audit_agent": audit_agent,
        "place_agent": place_agent, "prepare_draft": prepare_draft, "itinerary_agent": itinerary_agent,
    }.items():
        graph.add_node(name, node)
    graph.add_edge(START, "coordinator")
    graph.add_conditional_edges("coordinator", after_coordinator, {
        "transport_agent": "transport_agent", "guide_agent": "guide_agent",
        "activity_agent": "activity_agent", "route_agent": "route_agent",
        "audit_agent": "audit_agent", "done": END,
        "hotel_agent": "hotel_agent", "prepare_draft": "prepare_draft", "place_agent": "place_agent",
    })
    graph.add_edge("transport_agent", END)
    graph.add_edge("hotel_agent", "activity_agent")
    graph.add_edge("guide_agent", "activity_agent")
    graph.add_edge("activity_agent", "place_agent")
    graph.add_edge("place_agent", END)
    graph.add_edge("prepare_draft", "itinerary_agent")
    graph.add_conditional_edges("itinerary_agent", after_itinerary, {"itinerary_agent": "itinerary_agent", "route_agent": "route_agent"})
    graph.add_edge("route_agent", "audit_agent")
    graph.add_conditional_edges("audit_agent", after_audit, {"itinerary_agent": "itinerary_agent", "done": END})
    return graph.compile()


GRAPH = build_planning_graph()


def run_planning_action(plan: dict[str, Any], action: str, payload: dict[str, Any]) -> PlanningState:
    return GRAPH.invoke({"plan": deepcopy(plan), "action": action, "payload": payload, "trace": []})
