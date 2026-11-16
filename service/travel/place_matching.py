"""Search guide locations and use the first compatible map candidate."""
import logging
from copy import deepcopy
from functools import partial

from service.integrations.travel_amap import retry_amap_call
from service.mcp.server import amap_search_places
from service.travel.guide import split_guide_days
from service.travel.map_planning import has_coordinates
from service.travel.parallel import ordered_parallel
from service.utils.request_log import context as log_context, log_error

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


def _search(query, **kwargs):
    return retry_amap_call(amap_search_places, query, **kwargs)


def _search_lodging(area, destination):
    log_context.set({**log_context.get(), "lodging_area": area["name"]})
    try:
        result = _search(f"{area['name'].split('/')[0]} 酒店", region_code=destination, max_results=5)
    except (TimeoutError, ConnectionError) as exc:
        # 住宿搜索重试耗尽时，该区域无候选，其余地点仍参与汇总。
        log_error(exc, stage="lodging_search")
        return None, str(exc)
    if not result.get("success"):
        return None, result.get("message") or "高德住宿搜索失败"
    return next((p for p in result.get("places", []) if has_coordinates(p) and compatible_type(p, "hotel")
                 and destination.removesuffix("市") in (p.get("address") or "")), None), None


def _match_activity(item, position, destination, plan_id):
    # 地点任务只修改活动副本，搜索和候选校验仍按顺序执行。
    item = deepcopy(item)
    events = []
    if item.get("choice") == "exclude" or item.get("source") in {"simulation", "simulated_suggestion"}:
        return item, events
    if has_coordinates(item.get("place")):
        return item, [{"position": position, "event": "skipped_saved_location"}]
    log_context.set({**log_context.get(), "plan_id": plan_id, "activity_id": item["id"], "position": position})
    name = item.get("source_name") or item["name"]
    try:
        query = f"{name} {item.get('location_hint') or ''}".strip()
        result = _search(query, region_code=destination, max_results=5)
        events.append({"position": position, "event": "tool_result", "success": bool(result.get("success"))})
        if not result.get("success"):
            raise ValueError(result.get("message") or "高德地点搜索失败")
        city = destination.removesuffix("市")
        candidates = [p for p in result.get("places", []) if has_coordinates(p)
                      and city in (p.get("address") or "") and compatible_type(p, item.get("category"))][:5]
        item["place_candidates"] = candidates
        if candidates:
            selected = candidates[0]
            item.update(place=deepcopy(selected), name=selected["name"], location=selected["address"],
                        place_status="auto_confirmed")
            item.pop("place_error", None)
        else:
            item.update(place_status="failed", place_error="未找到可靠位置，请补充地址后搜索")
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
    if "guide_days" not in state:
        state["guide_days"], state["guide_reserve"] = split_guide_days(state.get("guide_text", ""))
    day_texts = {section["day"]: section["text"].casefold() for section in state["guide_days"]}
    batches = {}
    hotels = []
    for position, item in enumerate(activities, 1):
        if item.get("category") == "hotel":
            hotels.append((position, item))
            continue
        day = item.get("day")
        if day is None:
            name = (item.get("source_name") or item["name"]).casefold()
            mentions = [day for day, text in day_texts.items() if name in text]
            if len(mentions) == 1:
                day = item["day"] = mentions[0]
        batches.setdefault(day, []).append((position, item))

    # Multiple day batches and hotel searches share the same worker limit.
    def match_batch(batch):
        return [(position, _match_activity(item, position, destination, plan.get("plan_id")))
                for position, item in batch]

    stay_jobs = [partial(_search_lodging, area, destination) for area in areas]
    poi_jobs = [partial(match_batch, [hotel]) for hotel in hotels]
    poi_jobs.extend(partial(match_batch, batches[day]) for day in sorted(batches, key=lambda day: (day is None, day)))
    results = ordered_parallel([
        *stay_jobs, *poi_jobs,
    ])
    matched = []
    for _, (item, item_events) in sorted(entry for batch in results[len(stay_jobs):] for entry in batch):
        matched.append(item)
        events.extend(item_events)
    # 沿用原始 POI 过滤条件，随后统一去重并重写约束和待排引用。
    lodging = []
    state["lodging_search_errors"] = {}
    for area, (place, error) in zip(areas, results[:len(stay_jobs)]):
        if error:
            state["lodging_search_errors"][area["name"]] = error
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
