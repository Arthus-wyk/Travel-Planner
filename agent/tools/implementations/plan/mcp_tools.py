"""Register typed plan tools with their existing domain handlers."""
from agent.tools.implementations.plan import service
from agent.tools.schemas import plan as schemas
from agent.tools.schemas.inputs import ToolSpec

TOOL_NAMES = ("create_travel_plan", "update_travel_profile", "get_travel_plan", "summarize_travel_plan", "list_travel_plans", "delete_travel_plan")
TOOL_DOMAINS = {name: {"plan"} for name in TOOL_NAMES}
TOOL_RISKS = {"delete_travel_plan": "destructive"}



def specs() -> list[ToolSpec]:
    contracts = (
        ("create_travel_plan", schemas.CreateTravelPlanInput, schemas.CreateTravelPlanOutput, False),
        ("update_travel_profile", schemas.UpdateTravelProfileInput, schemas.UpdateTravelProfileOutput, False),
        ("get_travel_plan", schemas.GetTravelPlanInput, schemas.GetTravelPlanOutput, True),
        ("summarize_travel_plan", schemas.SummarizeTravelPlanInput, schemas.SummarizeTravelPlanOutput, True),
        ("list_travel_plans", schemas.ListTravelPlansInput, schemas.ListTravelPlansOutput, True),
        ("delete_travel_plan", schemas.DeleteTravelPlanInput, schemas.DeleteTravelPlanOutput, False),
    )
    return [
        ToolSpec(name, handler.__doc__ or name, inputs, handler, frozenset(TOOL_DOMAINS[name]),
                 TOOL_RISKS.get(name, "low"), output_model=outputs, replay_safe=replay_safe)
        for name, inputs, outputs, replay_safe in contracts
        for handler in [getattr(service, name)]
    ]
