"""Schedule and audit itinerary results."""
from __future__ import annotations
from service.travel.itinerary_audit import audit_plan
from service.travel.reorder import optimize_itinerary
from service.travel.planning import schedule_itinerary, schedule_review
from service.travel.map_planning import has_coordinates
from agent.planning.schemas.state import PlanningState


def schedule_agent(state: PlanningState) -> dict:
    """根据已查询路线顺排行程时间；固定车次保持原时刻。"""
    plan = state["plan"]
    action = state["action"]
    optimized = plan["planning"].get("optimized_timetable")
    if action == "optimize_routes" or (optimized and action == "generate_from_guide"
                                        and state.get("optimization_applied")):
        return {"plan": plan, "trace": state["trace"] + ["schedule_agent"]}
    if optimized and (action in {"recalculate_routes", "route_mode", "route_modes", "update_activity"} or
                      (action == "review_changes" and all(
                          operation["action"] in {"route_mode", "update_activity", "update_budget", "acknowledge_purchase"}
                          for operation in state["payload"]["operations"]))):
        routes = {(route["origin_item_id"], route["destination_item_id"]): route for route in plan["routes"]}
        updated, _ = optimize_itinerary(plan, lambda origin, destination: routes.get(
            (origin["item_id"], destination["item_id"]), {}), preserve_order=True)
        return {"plan": updated if updated is not None else plan, "trace": state["trace"] + ["schedule_agent"]}
    plan["planning"].pop("optimized_timetable", None)
    schedule = schedule_review if "day_departures" in plan["planning"] else schedule_itinerary
    return {"plan": schedule(plan), "trace": state["trace"] + ["schedule_agent"]}


def audit_agent(state: PlanningState) -> dict:
    """检查时间、预算和攻略约束；将问题作为下一轮提案的反馈。"""
    plan = state["plan"]
    if state["action"] == "confirm" and not plan.get("itinerary"):
        raise ValueError("Generate an itinerary before confirmation")
    if state["action"] == "confirm" and plan.get("planning", {}).get("needs_replan"):
        raise ValueError("旅行基础信息已变化，请重新查询候选并生成行程。")
    audit = audit_plan(plan)
    if (plan.get("planning", {}).get("generation") is not None
            and (state["action"] != "optimize_routes" or state.get("optimization_applied"))):
        plan["planning"]["generation"]["constraint_results"] = audit.get("guide_constraints", [])
    if state["action"] == "confirm":
        meal_issue = next((item for item in audit["issues"] if item["type"] in {"meal_window", "meal_spacing"}), None)
        if meal_issue:
            raise ValueError(meal_issue["message"])
        plan = {**plan, "planning": {**plan["planning"], "stage": "confirmed"}}
    ordering_feedback = [x for x in audit["issues"] if x["type"] in {
        "time_conflict", "transport_buffer", "schedule_overflow", "guide_constraint", "pace_risk"}]
    return {"plan": plan, "audit": audit, "feedback": ordering_feedback,
            "revision_attempts": state.get("revision_attempts", 0) + 1,
            "trace": state["trace"] + ["audit_agent"]}


def after_audit(state: PlanningState) -> str:
    audit = state.get("audit", {})
    route_or_time = any(x["severity"] == "high" and x["type"] in {"time_conflict", "transport_buffer", "schedule_overflow"}
                        for x in audit.get("issues", []))
    pending_ids = {x.get("reference_id") for x in state.get("plan", {}).get("planning", {}).get("unscheduled_items", []) if x.get("manual_pending")}
    routable = {x["id"] for x in state.get("plan", {}).get("planning", {}).get("activities", [])
                if x.get("choice") != "exclude" and x["id"] not in pending_ids and has_coordinates(x.get("place"))}
    guide_order = any(not x.get("satisfied") and x.get("hard") and x.get("type") in {"day", "time_window", "order", "duration"}
                      and (not state.get("plan") or set(x.get("activity_ids", [])) <= routable)
                      for x in audit.get("guide_constraints", []))
    if not state.get("revision_failed") and state["action"] in {"draft", "generate_from_guide"} and state.get("revision_attempts", 0) < 3 and (route_or_time or guide_order):
        return "itinerary_agent"
    return "done"
