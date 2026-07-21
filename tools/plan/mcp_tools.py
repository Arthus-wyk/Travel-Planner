from tools.plan import service

TOOL_NAMES = ("create_travel_plan", "update_travel_profile", "get_travel_plan", "summarize_travel_plan", "list_travel_plans", "delete_travel_plan")
TOOL_DOMAINS = {name: {"plan"} for name in TOOL_NAMES}
TOOL_RISKS = {"delete_travel_plan": "destructive"}


def register(mcp) -> None:
    for name in TOOL_NAMES:
        mcp.tool()(getattr(service, name))
