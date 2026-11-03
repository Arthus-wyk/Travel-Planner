"""Bounded model tool invocation followed by schema-only POI disambiguation."""
import logging
import asyncio
from copy import deepcopy
import json
from jsonschema import validate

from service.mcp.server import amap_search_places
from service.travel.map_planning import has_coordinates
from service.utils.model_client import create_model_client, get_model

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


def search_and_match(plan: dict) -> dict:
    updated = deepcopy(plan)
    state = updated["planning"]
    events = state["place_events"] = []
    for area in state.get("lodging_areas", []):
        result = amap_search_places(f"{area['name'].split('/')[0]} 酒店", region_code=plan["destination"], max_results=5)
        candidates = [p for p in result.get("places", []) if has_coordinates(p) and compatible_type(p, "hotel")
                      and plan["destination"].removesuffix("市") in (p.get("address") or "")]
        if candidates:
            place = candidates[0]
            if not any((x.get("place") or {}).get("place_id") == place.get("place_id") for x in state.get("activities", [])):
                state.setdefault("activities", []).append({"id": f"hotel-{place['place_id']}", "name": place["name"],
                    "category": "hotel", "source": "lodging_search", "note": area.get("note", ""),
                    "visit_tips": area.get("note", ""), "location": place["address"], "place": place, "choice": "optional"})
    for position, item in enumerate(state.get("activities", []), 1):
        if item.get("choice") == "exclude" or item.get("source") in {"simulation", "simulated_suggestion"}:
            continue
        if has_coordinates(item.get("place")):
            events.append({"position": position, "event": "skipped_saved_location"})
            continue
        name = item.get("source_name") or item["name"]
        context = {"place_position": position, "name": name, "category": item.get("category"),
                   "address_hint": item.get("location_hint", ""), "city": plan["destination"],
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
            result = amap_search_places(query, region_code=plan["destination"], max_results=5)
            events.append({"position": position, "event": "tool_result", "success": bool(result.get("success"))})
            if not result.get("success"):
                raise ValueError("map_search_failed: 地图搜索失败")
            city = plan["destination"].removesuffix("市")
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
            from service.utils.request_log import log_error
            log_error(exc, stage="place_matching", position=position, plan_id=plan.get("plan_id"))
            logging.getLogger("travel.generation").exception("place_failed plan_id=%s activity_id=%s position=%s", plan.get("plan_id"), item.get("id"), position)
            item.update(place_status="failed", place_error=str(exc)[:300])
            events.append({"position": position, "event": "error", "message": str(exc)[:300]})
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
