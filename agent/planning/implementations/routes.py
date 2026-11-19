"""Resolve and optimize grounded map routes."""
from __future__ import annotations
from functools import partial
from math import isfinite
from service import config
from service.integrations.travel_amap import retry_amap_call
from service.mcp.server import amap_compute_route
from service.travel.parallel import ordered_parallel
from service.travel.itinerary_audit import is_main_meal, meal_types
from service.travel.reorder import optimize_itinerary
from service.travel.planning import apply_route_times
from agent.planning.schemas.state import PlanningState


def _resolve_route(route, origin, destination, action):
    from service.utils.request_log import context

    route = route.copy()
    context.set({**context.get(), "origin_item_id": route["origin_item_id"],
                 "destination_item_id": route["destination_item_id"]})
    route["intercity"] = False
    route["station_transfer"] = "intercity_transport" in {origin["category"], destination["category"]}
    route["origin"] = origin.get("arrival_location", origin["location"])
    route["destination"] = destination["location"]
    route.setdefault("travel_mode", "TRANSIT")
    if origin.get("lodging_pending") or destination.get("lodging_pending"):
        return {**route, "duration_minutes": 0, "source": "lodging_pending"}
    origin_poi = (origin.get("place") or {}).get("place_id")
    if origin_poi and origin_poi == (destination.get("place") or {}).get("place_id") and route["origin"] == route["destination"]:
        return {**route, "duration_minutes": 0, "source": "same_place", "legs": []}
    if not config.AMAP_API_KEY:
        return route

    def waypoint(item, arriving=False):
        if arriving and item.get("arrival_location"):
            return item["arrival_location"]
        place = item.get("place") or {}
        return f"{place['longitude']},{place['latitude']}" if place else item["location"]
    choices = []
    errors = []
    points = [waypoint(origin, arriving=True), waypoint(destination)]
    options = dict(route.get("route_options") or {}) if route.get("route_options_key") == points else {}
    refresh = {"refresh": True} if action == "optimize_routes" else {}
    if refresh:
        options = {}
    if action == "recalculate_routes":
        options = {mode: result for mode, result in options.items() if result.get("success")}
    if len(options) < 3:
        for mode in ("WALK", "TRANSIT", "DRIVE"):
            if mode in options:
                continue
            try:
                options[mode] = retry_amap_call(amap_compute_route, *points, travel_mode=mode, **refresh)
            except (TimeoutError, ConnectionError) as exc:
                options[mode] = {"success": False, "message": str(exc)}
            result = options[mode]
            result.pop("raw_routes", None)
            try:
                seconds = float(str(result.get('duration', '')).removesuffix('s'))
                if not result.get('success') or not 0 < seconds < float('inf'):
                    raise ValueError
                result['duration_minutes'] = (int(seconds) + 59) // 60
            except (TypeError, ValueError):
                result['success'] = False
            fare = result.get('fare')
            result['fare'] = (0 if mode == 'WALK' and result.get('success') else fare
                              if isinstance(fare, (int, float)) and not isinstance(fare, bool) and isfinite(fare) and fare >= 0 else None)

        route["route_options"] = options
        route["route_options_key"] = points
    for mode in ((route["travel_mode"],) if route.get("mode_locked") else ("WALK", "TRANSIT", "DRIVE")):
        result = options.get(mode) or {}
        try:
            seconds = float(str(result.get("duration", "")).removesuffix("s"))
            if not result.get("success") or not 0 < seconds < float("inf"):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(result.get("message", f"{mode} 路线不可用"))
            continue
        choices.append((seconds, mode, result))
    if not choices:
        # 单条路段查不到可用方式时保留估算，其他路段仍可采用高德结果。
        route.update(source="provisional_estimate", duration_minutes=30, legs=[], error="；".join(errors))
        route.pop("fare", None)
        return route
    seconds, mode, result = min(choices, key=lambda x: x[0])
    route.update(travel_mode=mode, duration_minutes=(int(seconds) + 59) // 60,
                 distance_meters=result.get("distance_meters"), source="高德地图",
                 legs=result.get("legs", []))
    route.pop("fare", None)
    if isinstance(result.get("fare"), (int, float)) and not isinstance(result["fare"], bool) and isfinite(result["fare"]) and result["fare"] >= 0:
        route["fare"] = result["fare"]
    if route["station_transfer"] and any(x["category"] == "intercity_transport" and not x.get("station_confirmed") for x in (origin, destination)):
        route["source"] = "provisional_estimate"
        route["error"] = "尚未确认具体车站或机场，接驳按城市位置估算，请填写出发站和到达站。"
        route["legs"] = []
        route.pop("fare", None)
    else:
        route.pop("error", None)
    return route


def route_agent(state: PlanningState) -> dict:
    """并行查询相邻路段，汇总后更新行程时间。"""
    plan = state["plan"]
    action = state["action"]
    types = meal_types(plan)
    explicit = action == "optimize_routes"
    if explicit or (config.AMAP_API_KEY and action == "generate_from_guide"
                    and any(is_main_meal(item, types) for item in plan["itinerary"])):
        old = {(r["origin_item_id"], r["destination_item_id"]): r for r in plan["routes"]}

        def candidate(origin, destination):
            pair = origin["item_id"], destination["item_id"]
            route = {"origin_item_id": pair[0], "destination_item_id": pair[1], "source": "provisional_estimate"}
            previous = old.get(pair, {})
            if previous.get("mode_locked"):
                route.update(mode_locked=True, travel_mode=previous["travel_mode"])
            return _resolve_route(route, origin, destination, "optimize_routes" if explicit else action)

        if config.AMAP_API_KEY:
            optimized, message = optimize_itinerary(plan, candidate)
        else:
            optimized, message = None, "未配置高德地图，无法验证候选路线，已保留原行程。"
        if optimized is not None or explicit:
            return {"plan": optimized if optimized is not None else plan,
                    "optimization_applied": optimized is not None, "optimization_message": message,
                    "trace": state["trace"] + ["route_agent"]}
    if action == "generate_from_guide":
        plan["planning"].pop("optimized_timetable", None)
    by_id = {item["item_id"]: item for item in plan["itinerary"]}
    # 每个任务只更新自己的路段，同一路段内按原顺序查询交通方式。
    routes = ordered_parallel(
        partial(_resolve_route, route, by_id[route["origin_item_id"]],
                by_id[route["destination_item_id"]], state["action"])
        for route in plan["routes"]
    )
    plan = {**plan, "routes": routes}
    if (action not in {"generate_from_guide", "draft"} and "day_departures" not in plan["planning"]
            and not plan["planning"].get("optimized_timetable")):
        plan = apply_route_times(plan)
    return {"plan": plan, "trace": state["trace"] + ["route_agent"]}
