from tools.itinerary import service

TOOL_NAMES = ("add_itinerary_item", "update_itinerary_item", "delete_itinerary_item", "move_itinerary_item", "list_itinerary", "update_itinerary_routes", "reconcile_travel_plan", "audit_travel_plan")
TOOL_DOMAINS = {name: {"itinerary"} for name in TOOL_NAMES}
TOOL_DOMAINS.update({"update_itinerary_routes": {"route"}, "reconcile_travel_plan": {"itinerary", "route", "budget"}, "audit_travel_plan": {"itinerary", "route", "budget"}})
TOOL_RISKS = {"delete_itinerary_item": "destructive", "move_itinerary_item": "medium"}


def register(mcp) -> None:
    for name in TOOL_NAMES:
        mcp.tool()(getattr(service, name))
