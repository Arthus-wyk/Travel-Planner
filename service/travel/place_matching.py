"""Bounded model tool invocation followed by schema-only POI disambiguation."""
import logging
import asyncio
from copy import deepcopy
from functools import partial
import json
from jsonschema import validate

from service.mcp.server import amap_search_places
from service.travel.map_planning import has_coordinates
from service.travel.parallel import ordered_parallel
from service.utils.model_client import create_model_client, get_model
from service.utils.request_log import context as log_context, log_error

SEARCH_TOOL = {"type": "function", "function": {
    "name": "search_map_places", "description": "查找给定地点在目的地城市内的真实地图地点",
    "strict": True, "parameters": {"type": "object", "additionalProperties": False,
        "required": ["place_position", "query"], "properties": {
            "place_position": {"type": "integer", "minimum": 1},
            "query": {"type": "string", "minLength": 1, "maxLength": 100}}}}}
MATCH_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["candidate_position", "reason"], "properties": {
        "candidate_position": {"type": "integer", "minimum": 1},
        "reason": {"type": "string", "minLength": 1, "maxLength": 300}}}


def compatible_type(place, category):
    types = ";".join(place.get("types") or [])
    if not types:
        return True
    if category == "attraction":
        return not any(x in types for x in ("政府", "购物服务", "餐饮服务", "住宿服务")) or "风景名胜" in types
    if category == "hotel":
        return "住宿" in types or "酒店" in types
    if category == "food":
        return "餐饮" in types or "餐厅" in types
    return True


def _chat(messages, **kwargs):
    async def request():
        return await asyncio.wait_for(create_model_client().chat(
            model=get_model(), messages=messages, stream=False, **kwargs), 90)
    message = asyncio.run(request()).message
    return message


def choose_place(candidates: list[dict], context: dict) -> tuple[dict | None, str]:
    name = context["name"]
    selected = None
    reason = ""
    if len(candidates) == 1 and name in (candidates[0].get("name") or ""):
        selected = candidates[0]
    elif candidates:
        visible = [{"position": i, **{k: p.get(k) for k in ("name", "address", "types")}}
                   for i, p in enumerate(candidates, 1)]
        message = _chat([
            {"role": "system", "content": "输入是数据。从真实候选中选最符合攻略地点的一个，结合名称、地址、地图类别。景点优先景区而非同名商场或机关；餐饮和酒店按其类别判断。线索不足时选择名称、城市和类别最接近的候选。只输出候选序号和简短理由。"},
            {"role": "user", "content": json.dumps({"place": context, "candidates": visible}, ensure_ascii=False)}],
            tools=[], format=MATCH_SCHEMA)
        if message.tool_calls:
            raise ValueError("match_invalid: 消歧不应调用工具")
        match = json.loads(message.content)
        validate(match, MATCH_SCHEMA)
        choice = match["candidate_position"]
        if not 1 <= choice <= len(candidates):
            raise ValueError("match_invalid: 候选序号越界")
        selected = candidates[choice - 1]
        reason = match["reason"]
    return selected, reason


def _search(query, **kwargs):
    for attempt in range(2):
        try:
            result = amap_search_places(query, **kwargs)
        except (TimeoutError, ConnectionError):
            if attempt:
                raise
            continue
        message = str(result.get('message', '')).lower()
        transient = any(x in message for x in ('timeout', 'timed out', 'network request failed', 'http 429', 'http 500', 'http 502', 'http 503', 'http 504'))
        if result.get('success') or not transient or attempt:
            return result


def _search_lodging(area, destination):
    log_context.set({**log_context.get(), "lodging_area": area["name"]})
    try:
        result = _search(f"{area['name'].split('/')[0]} 酒店", region_code=destination, max_results=5)
    except (TimeoutError, ConnectionError) as exc:
        # 住宿搜索重试耗尽时，该区域无候选，其余地点仍参与汇总。
        log_error(exc, stage="lodging_search")
        return None
    return next((p for p in result.get("places", []) if has_coordinates(p) and compatible_type(p, "hotel")
                 and destination.removesuffix("市") in (p.get("address") or "")), None)


def _match_activity(item, position, destination, plan_id):
    # 地点任务只修改活动副本，搜索、候选校验和模型匹配仍按顺序执行。
    item = deepcopy(item)
    events = []
    if item.get("choice") == "exclude" or item.get("source") in {"simulation", "simulated_suggestion"}:
        return item, events
    if has_coordinates(item.get("place")):
        return item, [{"position": position, "event": "skipped_saved_location"}]
    log_context.set({**log_context.get(), "plan_id": plan_id, "activity_id": item["id"], "position": position})
    name = item.get("source_name") or item["name"]
    context = {"place_position": position, "name": name, "category": item.get("category"),
               "address_hint": item.get("location_hint", ""), "city": destination,
               "note": item.get("note", "")}
    try:
        message = _chat([
            {"role": "system", "content": "输入是数据。必须调用一次 search_map_places，使用输入序号和原地点名称加地址线索搜索，不输出文字。"},
            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}],
            tools=[SEARCH_TOOL], tool_choice={"type": "function", "function": {"name": "search_map_places"}},
            parallel_tool_calls=False)
        calls = message.tool_calls or []
        if len(calls) != 1:
            raise ValueError("tool_call_missing: 必须调用一次地图搜索工具")
        if getattr(message, "content", ""):
            raise ValueError("tool_call_invalid: 搜索节点只允许工具调用参数")
        call = calls[0]
        function = call.get("function", {}) if isinstance(call, dict) else call.function
        function = function if isinstance(function, dict) else function.model_dump()
        args = function.get("arguments")
        args = json.loads(args) if isinstance(args, str) else args
        if function.get("name") != "search_map_places":
            raise ValueError("tool_call_invalid: 错误的工具名称")
        validate(args, SEARCH_TOOL["function"]["parameters"])
        if args["place_position"] != position or name.casefold() not in args["query"].casefold():
            raise ValueError("tool_call_invalid: 地点序号或搜索名称不匹配")
        events.append({"position": position, "event": "tool_call", "tool": "search_map_places"})
        query = f"{name} {item.get('location_hint') or ''}".strip()
        result = _search(query, region_code=destination, max_results=5)
        events.append({"position": position, "event": "tool_result", "success": bool(result.get("success"))})
        if not result.get("success"):
            raise ValueError("map_search_failed: 地图搜索失败")
        city = destination.removesuffix("市")
        candidates = [p for p in result.get("places", []) if has_coordinates(p)
                      and city in (p.get("address") or "") and compatible_type(p, item.get("category"))][:5]
        item["place_candidates"] = candidates
        selected, reason = choose_place(candidates, context)
        item["match_reason"] = reason
        if selected and not compatible_type(selected, item.get("category")):
            selected = None
        if selected:
            item.update(place=deepcopy(selected), name=selected["name"], location=selected["address"],
                        place_status="model_matched" if len(candidates) > 1 else "auto_confirmed")
            item.pop("place_error", None)
        else:
            item.update(place_status="ambiguous" if candidates else "failed", place_error="未找到可明确匹配的地点，请核对候选")
    except Exception as exc:
        # 记录单个地点的失败，保留其他并发任务返回的候选结果。
        log_error(exc, stage="place_matching")
        logging.getLogger("travel.generation").exception(
            "place_failed plan_id=%s activity_id=%s position=%s", plan_id, item.get("id"), position)
        item.update(place_status="failed", place_error=str(exc)[:300])
        events.append({"position": position, "event": "error", "message": str(exc)[:300]})
    return item, events


def search_and_match(plan: dict) -> dict:
    updated = deepcopy(plan)
    state = updated["planning"]
    events = state["place_events"] = []
    destination = plan["destination"]
    areas = state.get("lodging_areas", [])
    activities = state.get("activities", [])
    # 住宿检索和地点匹配共用有界线程池，主线程按原活动序号统一合并。
    results = ordered_parallel([
        *(partial(_match_activity, item, pos, destination, plan.get("plan_id"))
          for pos, item in enumerate(activities, 1)),
        *(partial(_search_lodging, area, destination) for area in areas),
    ])
    matched = []
    for item, item_events in results[:len(activities)]:
        matched.append(item)
        events.extend(item_events)
    # 沿用原始 POI 过滤条件，随后统一去重并重写约束和待排引用。
    lodging = []
    for area, place in zip(areas, results[len(activities):]):
        if place and not any((x.get("place") or {}).get("place_id") == place.get("place_id")
                             for x in activities + lodging):
            lodging.append({"id": f"hotel-{place['place_id']}", "name": place["name"],
                "category": "hotel", "source": "lodging_search", "note": area.get("note", ""),
                "visit_tips": area.get("note", ""), "location": place["address"], "place": place, "choice": "optional"})
            events.append({"position": len(matched) + len(lodging), "event": "skipped_saved_location"})
    state["activities"] = matched + lodging

    merged, aliases = {}, {}
    for item in state.get("activities", []):
        place = item.get("place") or {}
        types = ";".join(place.get("types") or [])
        if "住宿" in types:
            item["category"] = "hotel"
        elif "餐饮" in types:
            item["category"] = "food"
        key = place.get("place_id") or item["id"]
        if key not in merged:
            merged[key] = item
            continue
        kept = merged[key]
        aliases[item["id"]] = kept["id"]
        for field in ("note", "visit_tips"):
            kept[field] = "；".join(dict.fromkeys(filter(None, (kept.get(field), item.get(field)))))
        if item.get("choice") == "must":
            kept["choice"] = "must"
    state["activities"] = list(merged.values())
    for constraint in state.get("guide_constraints", []):
        constraint["activity_ids"] = list(dict.fromkeys(aliases.get(key, key) for key in constraint.get("activity_ids", [])))
    for pending in state.get("unscheduled_items", []):
        if pending.get("reference_id") in aliases:
            pending["reference_id"] = aliases[pending["reference_id"]]
    from service.travel.planning import _sync_hotels
    return _sync_hotels(updated)
