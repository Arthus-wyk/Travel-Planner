"""Domain-first routing and concise, risk-aware tool descriptions."""

from __future__ import annotations

from typing import Any, Iterable

from tools.catalog import domain_tools


FALLBACK_TOOLS = {
    "get_travel_plan",
    "list_travel_plans",
    "summarize_travel_plan",
    "read_travel_preference_profile",
}

_LEGACY_GUIDANCE: dict[str, str] = {
    "create_travel_plan": "Use only for an explicitly new trip. Do not use when a current plan_id exists; requires trip fields and persists data. Side effect: creates a plan. Unlike update_travel_profile, it creates the plan itself.",
    "get_travel_plan": "Use to read one plan by plan_id. Do not use to change data; requires plan_id and has no side effect. Unlike summarize_travel_plan, returns detailed state.",
    "list_travel_plans": "Use to discover existing plans when no plan_id is known. Do not use for detailed contents; read-only and returns summaries.",
    "summarize_travel_plan": "Use for a compact overview of one plan. Do not use to edit; requires plan_id and is read-only. Unlike get_travel_plan, omits detail.",
    "update_travel_profile": "Use to update fields on the current travel plan. Do not use to create a new plan; requires plan_id and valid profile fields. Side effect: persists changes.",
    "add_itinerary_item": "Use to add one scheduled activity. Do not use for recommendations that are not scheduled; requires plan_id/day/item fields. Side effect: persists item and may invalidate routes.",
    "update_itinerary_item": "Use to edit an existing scheduled item. Do not use when item_id is unknown; requires plan_id and item_id. Side effect: persists changes and may invalidate routes.",
    "delete_itinerary_item": "Use only when the user asks to remove a scheduled item. Requires plan_id and item_id; destructive side effect: deletes the item and related routes.",
    "move_itinerary_item": "Use to move an existing item between days/times. Requires plan_id/item_id and destination day. Side effect: persists the move and invalidates related routes.",
    "list_itinerary": "Use to inspect scheduled items for a plan. Do not use to modify; requires plan_id and is read-only.",
    "reconcile_travel_plan": "Use after itinerary changes to refresh affected routes and audit pacing/budget. Do not use for a simple read; may call maps and persist route/audit data.",
    "google_maps_search_places": "Use to find attractions/restaurants. Do not use for hotels or route calculation; requires location/query and has external API/network cost, but does not persist the plan.",
    "google_maps_search_hotels": "Use to find hotels. Do not use for attractions or routes; requires destination/query and has external API/network cost, without plan mutation.",
    "google_maps_compute_route": "Use to calculate one route between known locations. Do not use to persist route records; external API/network cost, read-only. For saving routes use update_itinerary_routes or reconcile_travel_plan.",
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

TOOL_GUIDANCE = _LEGACY_GUIDANCE


def route_tool_names(domains: Iterable[str], available: set[str]) -> set[str]:
    """Return the smallest safe model-visible set for detected domains."""
    selected: set[str] = set()
    available_by_domain = domain_tools()
    for domain in domains:
        selected.update(available_by_domain.get(domain, ()))
    return (selected or FALLBACK_TOOLS) & available


def enrich_description(name: str, description: str | None) -> str:
    base = (description or f"Call {name}.").strip()
    guidance = TOOL_GUIDANCE.get(name)
    return f"{base} {guidance}" if guidance else base


def filter_ollama_tools(tools: list[dict[str, Any]], names: set[str]) -> list[dict[str, Any]]:
    return [tool for tool in tools if tool.get("function", {}).get("name") in names]
