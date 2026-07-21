from tools.memory import service

TOOL_NAMES = ("read_travel_preference_profile", "update_travel_preference_profile", "verify_last_tool_result")
TOOL_DOMAINS = {"read_travel_preference_profile": {"memory"}, "update_travel_preference_profile": {"memory"}, "verify_last_tool_result": set()}


def register(mcp) -> None:
    for name in TOOL_NAMES:
        mcp.tool()(getattr(service, name))
