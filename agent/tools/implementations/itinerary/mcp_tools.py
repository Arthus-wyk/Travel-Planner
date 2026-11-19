"""Register typed itinerary tools with their existing domain handlers."""
from agent.tools.implementations.itinerary import service
from agent.tools.schemas import itinerary as schemas
from agent.tools.schemas.inputs import ToolSpec

TOOL_NAMES = ("add_itinerary_item", "update_itinerary_item", "delete_itinerary_item", "move_itinerary_item", "list_itinerary", "update_itinerary_routes", "reconcile_travel_plan", "audit_travel_plan")
TOOL_DOMAINS = {name: {"itinerary"} for name in TOOL_NAMES}
TOOL_DOMAINS.update({"update_itinerary_routes": {"route"}, "reconcile_travel_plan": {"itinerary", "route", "budget"}, "audit_travel_plan": {"itinerary", "route", "budget"}})
TOOL_RISKS = {"delete_itinerary_item": "destructive", "move_itinerary_item": "medium"}



def specs() -> list[ToolSpec]:
    contracts = (
        ("add_itinerary_item", schemas.AddItineraryItemInput, schemas.AddItineraryItemOutput, False),
        ("update_itinerary_item", schemas.UpdateItineraryItemInput, schemas.UpdateItineraryItemOutput, False),
        ("delete_itinerary_item", schemas.DeleteItineraryItemInput, schemas.DeleteItineraryItemOutput, False),
        ("move_itinerary_item", schemas.MoveItineraryItemInput, schemas.MoveItineraryItemOutput, False),
        ("list_itinerary", schemas.ListItineraryInput, schemas.ListItineraryOutput, True),
        ("update_itinerary_routes", schemas.UpdateItineraryRoutesInput, schemas.UpdateItineraryRoutesOutput, False),
        ("reconcile_travel_plan", schemas.ReconcileTravelPlanInput, schemas.ReconcileTravelPlanOutput, False),
        ("audit_travel_plan", schemas.AuditTravelPlanInput, schemas.AuditTravelPlanOutput, True),
    )
    return [
        ToolSpec(name, handler.__doc__ or name, inputs, handler, frozenset(TOOL_DOMAINS[name]),
                 TOOL_RISKS.get(name, "low"), output_model=outputs, replay_safe=replay_safe,
                 allow_partial=name == "reconcile_travel_plan")
        for name, inputs, outputs, replay_safe in contracts
        for handler in [getattr(service, name)]
    ]
