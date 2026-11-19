"""由调用方绑定到单个审核页行程的工具注册表。"""

import asyncio
from uuid import uuid4

from service.mcp.server import amap_compute_route, amap_search_hotels, amap_search_places
from service.storage import planning_jobs
from service.storage.travel_db import get_plan
from service.travel.itinerary_audit import audit_plan
from service.travel.review import preview_review_action, review_acceptance, save_review_action
from agent.tools.registry import ToolRegistry
from agent.tools.schemas.errors import ErrorCode, ToolRejected
from agent.tools.schemas.inputs import ToolSpec
from agent.tools.implementations.maps.service import compact_map_result


from agent.tools.schemas.review import REVIEW_TOOL_MODELS


# ============提取指定日期的行程上下文 ============
def review_context_for_day(snapshot: dict, day: int | None) -> dict:
    """返回用于模型决策的日程、路线和预算摘要，按需限定日期。"""
    items = [item for item in snapshot.get("itinerary", []) if day is None or item["day"] == day]
    item_ids = {item["item_id"] for item in items}
    references = item_ids | {item["reference_id"] for item in items if item.get("reference_id")}
    audit = snapshot.get("audit") or {}
    issues = []
    for issue in audit.get("issues", []):
        target = issue.get("target")
        targets = target if isinstance(target, list) else [target]
        if day is None or target is None or target == day or any(value in references for value in targets if isinstance(value, str)):
            issues.append({key: issue[key] for key in ("type", "severity", "message", "target") if key in issue})
    budget = audit.get("budget")
    if budget is not None:
        budget = {
            **{key: budget[key] for key in (
                "estimated_total", "remaining", "has_possible_duplicates", "has_unknown_ticket_costs",
                "has_unknown_hotel_costs", "has_unknown_transport_costs", "has_unknown_food_costs",
            ) if key in budget},
            "limit": budget.get("limit", (audit.get("metrics") or {}).get("budget")),
        }
    return {
        **{key: snapshot[key] for key in ("plan_version", "stage", "destination", "start_date", "end_date") if key in snapshot},
        **({"day": day} if day is not None else {}),
        "itinerary": [
            {key: item[key] for key in (
                "item_id", "reference_id", "day", "day_offset", "time_of_day", "title", "location",
                "category", "duration_minutes", "estimated_cost", "visit_tips", "ticket_required", "anchor_role",
            ) if key in item} for item in items
        ],
        "routes": [
            {key: route[key] for key in (
                "origin_item_id", "destination_item_id", "travel_mode", "duration", "duration_minutes",
                "fare", "status", "source", "intercity",
            ) if key in route} for route in snapshot.get("routes", [])
            if day is None or (route["origin_item_id"] in item_ids and route["destination_item_id"] in item_ids)
        ],
        "audit": {"issues": issues, "budget": budget},
    }


def review_registry(plan_id: str, *, writable: bool = False, draft: dict | None = None) -> ToolRegistry:
    """绑定审核工具；草稿模式只读取和试算调用方重建的行程。"""
    def load():
        """读取当前工具集使用的行程快照。"""
        if draft is not None:
            return draft["plan"]
        plan, error = get_plan(plan_id)
        if error and error.get("message") != "Travel plan not found.":
            raise ToolRejected(ErrorCode.INTERNAL_ERROR, "无法读取行程。")
        if plan is None:
            raise ToolRejected(ErrorCode.NOT_FOUND, "未找到当前行程。")
        if plan.get("planning", {}).get("stage") not in {"review", "confirmed"}:
            raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "行程尚未进入路线审核。")
        return plan

    def snapshot(plan):
        """提取模型使用的行程和审核信息。"""
        return {
            "plan_version": draft["plan_version"] if draft is not None else planning_jobs.fingerprint(plan),
            "stage": plan["planning"]["stage"],
            "destination": plan["destination"],
            "start_date": plan.get("start_date"),
            "end_date": plan.get("end_date"),
            "itinerary": plan.get("itinerary", []),
            "routes": [{k: v for k, v in route.items() if k not in {"legs", "route_options"}}
                       for route in plan.get("routes", [])],
            "audit": audit_plan(plan),
        }

    def search(args):
        query = args["query"].strip()
        if not 1 <= len(query) <= 100:
            raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "请输入 1–100 字的地点名称。")
        result = amap_search_places(query, region_code=load()["destination"])
        return compact_map_result("search_review_places", result)

    def search_hotels(args):
        destination = load()["destination"]
        query = args.get("query", "").strip()
        if len(query) > 100:
            raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "酒店搜索条件最多 100 字。")
        result = amap_search_hotels(destination, query, region_code=destination)
        return compact_map_result("search_review_hotels", result)

    def compare_route(args):
        plan = load()
        items = {item["item_id"]: item for item in plan["itinerary"]}
        origin = items.get(args["origin_item_id"])
        destination = items.get(args["destination_item_id"])
        if not origin or not destination:
            raise ToolRejected(ErrorCode.NOT_FOUND, "找不到指定的行程项目。")
        if origin == destination:
            raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "请选择两个不同的行程项目。")
        start = origin.get("arrival_location") or origin.get("location")
        end = destination.get("departure_location") or destination.get("location")
        if not start or not end:
            raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "路线端点缺少地点。")
        result = amap_compute_route(start, end, travel_mode=args["travel_mode"])
        return compact_map_result("compare_review_route", result)

    def prepare(action, args):
        """为试算或保存生成业务层需要的操作和新增项目标识。"""
        plan = load()
        if plan["planning"]["stage"] != "review":
            raise ToolRejected(ErrorCode.PERMISSION_DENIED, "只可修改审核中的行程。")
        if action == "review_changes":
            operations = [
                {"action": op["action"], "payload": {**op["payload"], "activity_id": f"manual-{uuid4().hex}"}}
                if op["action"] == "review_place" else op
                for op in args["operations"]
            ]
            args = {**args, "operations": operations}
        return plan, args

    def preview(args):
        """试算编辑并保留实际生成的项目标识，便于追加到草稿。"""
        action = "review_changes" if "operations" in args else "optimize_routes"
        plan, payload = prepare(action, args)
        if draft is not None:
            if args["plan_version"] != draft["plan_version"]:
                raise planning_jobs.PlanConflict("草稿版本已变化")
            payload = {**payload, "plan_version": planning_jobs.fingerprint(plan)}
        selection = draft.get("selection") if draft is not None else None
        if selection and selection["kind"] == "select_place":
            places = selection.get("places") or [selection["place"]]
            operations = payload.get("operations", [])
            if (len(operations) != len(places)
                    or any(op["action"] != "review_place" or op["payload"].get("replace") for op in operations)
                    or {op["payload"]["poi_id"] for op in operations} != {place["place_id"] for place in places}):
                raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "只能添加用户选中的地点，每个地点添加一次。")
            if len(places) == 1:
                outcome, payload = _best_place_insertion(plan, payload, places[0]["place_id"])
            else:
                outcome = preview_review_action(plan, action, payload)
        else:
            outcome = preview_review_action(plan, action, payload)
        return {**snapshot(outcome["plan"]), **review_acceptance(plan, outcome),
                "plan_version": args["plan_version"], "optimization_message": outcome.get("optimization_message"),
                **({"draft_plan": outcome["plan"], "operations": payload.get("operations", [])} if draft is not None else {})}

    def save(action, args):
        plan, args = prepare(action, args)
        result = save_review_action(plan, action, args, require_acceptance=True)
        if result.get("accepted") is False:
            return {"success": False, "error_type": "review_rejected",
                    "message": result.get("optimization_message") or "试算未通过验收，修改未保存。",
                    "preview": {**snapshot(result["plan"]), "plan_version": args["plan_version"],
                                "blocking_issues": result["blocking_issues"], "accepted": False}}
        return {**snapshot(result["plan"]), "applied": result.get("optimization_applied", True),
                "optimization_message": result.get("optimization_message")}

    def spec(name, description, handle, *, write=False):
        inputs, outputs = REVIEW_TOOL_MODELS[name]
        async def invoke(args, context):
            return await asyncio.to_thread(handle, args)

        return ToolSpec(name, description, inputs, invoke, frozenset({"route"}),
                        required_permission="review:write" if write else None, output_model=outputs,
                        replay_safe=name in {"search_review_places", "search_review_hotels", "compare_review_route"})

    registry = ToolRegistry([
        spec("get_review_context", "读取路线、日程、版本和审核问题。处理具体日期时传 day（从 1 开始）。全程问题可省略。",
             lambda args: review_context_for_day(snapshot(load()), args.get("day"))),
        spec("preview_review_changes", "只读试算。传入当前 plan_version，批量编辑同时传 operations；只传版本则试算路线优化。返回验收和阻塞问题。",
             preview),
        spec("search_review_places", "按当前行程目的地搜索高德 POI。", search),
        spec("search_review_hotels", "在当前目的地查找住宿候选。", search_hotels),
        spec("compare_review_route", "估算当前行程两点之间的一种交通方式。支持 DRIVE、WALK、BICYCLE、TWO_WHEELER、TRANSIT。",
             compare_route),
    ])
    if writable and draft is None:
        registry.register(spec("apply_review_changes", "按版本批量保存审核页编辑。",
                               lambda args: save("review_changes", args), write=True))
        registry.register(spec("optimize_review_routes", "按版本重排并保存当前路线。",
                               lambda args: save("optimize_routes", args), write=True))
    return registry


# ============选择顺路插入点 ============
def _best_place_insertion(plan: dict, payload: dict, poi_id: str) -> tuple[dict, dict]:
    """在建议日期内比较有限的合法锚点，按全天路线耗时选择通过验收的方案。"""
    operations = payload.get("operations", [])
    if len(operations) != 1 or operations[0]["action"] != "review_place":
        raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "选择地点后仅能试算添加该地点")
    details = operations[0]["payload"]
    if details.get("poi_id") != poi_id or details.get("replace"):
        raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "编辑提案必须添加用户选中的地点")
    anchor = next((item for item in plan["itinerary"] if item["item_id"] == details["item_id"]), None)
    if anchor is None:
        raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "候选插入位置不在当前草稿中，请使用当前草稿中的 item_id。")
    day_items = [item for item in plan["itinerary"] if item["day"] == anchor["day"]]
    position = day_items.index(anchor)
    neighbors = sorted(day_items, key=lambda item: abs(day_items.index(item) - position))
    trials = []
    # shortcut: 在建议日期试算最靠近建议锚点的五个位置，需要跨天全局优化时扩充搜索。
    for item in neighbors[:5]:
        if item.get("anchor_role") == "end" or item["item_id"].endswith("-return"):
            continue
        candidate = {**payload, "operations": [{"action": "review_place",
                     "payload": {**details, "item_id": item["item_id"]}}]}
        try:
            trials.append((preview_review_action(plan, "review_changes", candidate), candidate))
        except ValueError:
            continue
    if not trials:
        raise ToolRejected(ErrorCode.INVALID_ARGUMENT, "建议日期没有可试算的插入位置，请调整日期或插入位置。")
    accepted = [trial for trial in trials if review_acceptance(plan, trial[0])["accepted"]]
    if not accepted:
        return trials[0]
    return min(accepted, key=lambda trial: sum(
        route.get("duration_minutes") or 0 for route in trial[0]["plan"]["routes"] if not route.get("intercity")
    ))
