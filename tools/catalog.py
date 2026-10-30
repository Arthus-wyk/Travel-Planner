"""Discovery and registration for business-domain MCP modules."""

from importlib import import_module
from typing import Any

MODULE_NAMES = ("plan", "itinerary", "maps", "recommendation", "memory")


def modules() -> list[Any]:
    return [import_module(f"tools.{name}.mcp_tools") for name in MODULE_NAMES]


def tool_domains() -> dict[str, set[str]]:
    return {
        name: set(domains)
        for module in modules()
        for name, domains in module.TOOL_DOMAINS.items()
    }


def domain_tools() -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for name, domains in tool_domains().items():
        for domain in domains:
            result.setdefault(domain, []).append(name)
    return result


def register_all(mcp: Any) -> None:
    for module in modules():
        module.register(mcp)


TOOL_GUIDANCE: dict[str, str] = {
    "create_travel_plan": "Use only for an explicitly new trip. Do not use when a current plan_id exists; requires trip fields and persists data. Side effect: creates a plan. Unlike update_travel_profile, it creates the plan itself.",
    "get_travel_plan": "Use to read one plan by plan_id. Do not use to change data; requires plan_id and has no side effect. Unlike summarize_travel_plan, returns detailed state.",
    "list_travel_plans": "Use to discover existing plans when no plan_id is known. Do not use for detailed contents; read-only and returns summaries.",
    "summarize_travel_plan": "Use for a compact overview of one plan. Do not use to edit; requires plan_id and is read-only. Unlike get_travel_plan, omits detail. Use remaining_budget_range for the whole trip; remaining_budget_after_budget_items excludes itinerary estimates.",
    "update_travel_profile": "Use to update fields on the current travel plan. Do not use to create a new plan; requires plan_id and valid profile fields. Side effect: persists changes.",
    "add_itinerary_item": "Use to add one scheduled activity. Do not use for recommendations that are not scheduled; requires plan_id/day/item fields. Side effect: persists item and may invalidate routes.",
    "update_itinerary_item": "Use to edit an existing scheduled item. Do not use when item_id is unknown; requires plan_id and item_id. Side effect: persists changes and may invalidate routes.",
    "delete_itinerary_item": "Use only when the user asks to remove a scheduled item. Requires plan_id and item_id; destructive side effect: deletes the item and related routes.",
    "move_itinerary_item": "Use to move an existing item between days/times. Requires plan_id/item_id and destination day. Side effect: persists the move and invalidates related routes.",
    "list_itinerary": "Use to inspect scheduled items for a plan. Do not use to modify; requires plan_id and is read-only.",
    "reconcile_travel_plan": "Use after itinerary changes to refresh affected routes and audit pacing/budget. Do not use for a simple read; may call maps and persist route/audit data.",
    "amap_geocode": "Resolve an address to GCJ-02 coordinates. Read-only external API call.",
    "amap_static_map": "Fetch a static map image for a location and markers. Read-only external API call; does not persist the plan.",
    "amap_search_places": "Use to find attractions/restaurants. Do not use for hotels or route calculation; requires location/query and has external API/network cost, but does not persist the plan.",
    "amap_search_hotels": "Use to find hotels. Do not use for attractions or routes; requires destination/query and has external API/network cost, without plan mutation.",
    "amap_compute_route": "Use to calculate one route between known locations. Do not use to persist route records; external API/network cost, read-only. For saving routes use update_itinerary_routes or reconcile_travel_plan.",
    "rank_recommendations": "Use to rank candidate places against preferences. Do not use to search the web or schedule items; requires candidates and is read-only.",
    "add_recommendation": "Use to persist an unscheduled candidate. Do not use for scheduled itinerary items; requires plan_id and candidate fields. Side effect: writes a recommendation.",
    "update_recommendation": "Use to edit a saved recommendation. Requires plan_id/recommendation_id; side effect: persists changes.",
    "add_budget_item": "Use to persist one budget expense. Do not use for estimating a whole trip; requires plan_id and expense fields. Side effect: writes budget data.",
    "audit_travel_plan": "Use to check schedule, pace, conflicts, and budget. Do not use to mutate; requires plan_id and is read-only.",
    "read_travel_preference_profile": "Use to read long-term preferences. Do not use for trip-specific fields; read-only.",
    "update_travel_preference_profile": "Use only when the user explicitly asks to remember a lasting preference. Side effect: writes persistent memory; do not use for one-trip constraints.",
    "verify_last_tool_result": "Use internally to verify completion claims after a tool call. Do not call as a substitute for the underlying operation; read-only.",
    "update_itinerary_routes": "Use to persist already-calculated route records. Do not use to calculate routes; requires valid item IDs and route objects. Side effect: writes route data.",
}


def tool_metadata() -> dict[str, dict[str, Any]]:
    """汇总受信工具模块的声明，模型文本不能修改工具能力和权限。"""
    return {
        name: {
            "domains": domains,
            "risk": getattr(module, "TOOL_RISKS", {}).get(name, "low"),
            "guidance": TOOL_GUIDANCE.get(name, ""),
            "default_visible": name
            in {
                "get_travel_plan",
                "list_travel_plans",
                "summarize_travel_plan",
                "read_travel_preference_profile",
            },
        }
        for module in modules()
        for name, domains in module.TOOL_DOMAINS.items()
    }
