"""Register typed memory tools with their existing domain handlers."""
from agent.tools.implementations.memory import service
from agent.tools.schemas import memory as schemas
from agent.tools.schemas.inputs import ToolSpec

TOOL_NAMES = ("read_travel_preference_profile", "update_travel_preference_profile", "verify_last_tool_result")
TOOL_DOMAINS = {"read_travel_preference_profile": {"memory"}, "update_travel_preference_profile": {"memory"}, "verify_last_tool_result": set()}

TOOL_RISKS = {}


def specs() -> list[ToolSpec]:
    contracts = (
        ("read_travel_preference_profile", schemas.ReadTravelPreferenceProfileInput, schemas.ReadTravelPreferenceProfileOutput, True),
        ("update_travel_preference_profile", schemas.UpdateTravelPreferenceProfileInput, schemas.UpdateTravelPreferenceProfileOutput, False),
        ("verify_last_tool_result", schemas.VerifyLastToolResultInput, schemas.VerifyLastToolResultOutput, True),
    )
    return [
        ToolSpec(name, handler.__doc__ or name, inputs, handler, frozenset(TOOL_DOMAINS[name]),
                 TOOL_RISKS.get(name, "low"), output_model=outputs, replay_safe=replay_safe)
        for name, inputs, outputs, replay_safe in contracts
        for handler in [getattr(service, name)]
    ]
