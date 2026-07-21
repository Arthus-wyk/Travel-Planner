from tools.recommendation import service

TOOL_NAMES = ("add_recommendation", "update_recommendation", "rank_recommendations", "add_budget_item")
TOOL_DOMAINS = {"add_recommendation": {"recommendation"}, "update_recommendation": {"recommendation"}, "rank_recommendations": {"recommendation", "hotel", "place", "restaurant"}, "add_budget_item": {"budget"}}


def register(mcp) -> None:
    for name in TOOL_NAMES:
        mcp.tool()(getattr(service, name))
