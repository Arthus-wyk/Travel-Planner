"""Planning graph topology and validated node execution."""
import json
from copy import deepcopy
from jsonschema import ValidationError
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, default_retry_on
from agent.planning.schemas.state import PlanningState
from agent.planning.schemas.outputs import NODE_OUTPUT_VALIDATORS
from agent.planning.runtime.context import generation_job_id
from agent.planning.implementations.coordinator import coordinator, after_coordinator
from agent.planning.implementations.candidates import (
    transport_agent,
    hotel_agent,
    guide_agent,
    activity_agent,
    place_agent,
    generate_guide,
    search_places,
    default_choices,
)
from agent.planning.implementations.itinerary_nodes import prepare_draft, itinerary_agent, after_itinerary
from agent.planning.implementations.routes import route_agent
from agent.planning.implementations.schedule import schedule_agent, audit_agent, after_audit

def _logged_node(name, node):
    validator = NODE_OUTPUT_VALIDATORS[name]

    def run(state):
        from service.utils.request_log import context, log_error
        token = context.set({**context.get(), "stage": name, "job_id": generation_job_id.get(),
                             "plan_id": state.get("plan", {}).get("plan_id")})
        try:
            update = node(deepcopy(state))
            error = next(validator.iter_errors(update), None)
            if error is not None:
                field = ".".join(map(str, error.absolute_path)) or "$"
                raise ValidationError(f"{name} 输出不符合 JSON Schema：{field} ({error.validator})")
            # Reject Python values that cannot be safely saved in a JSON checkpoint.
            try:
                json.dumps(update, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError) as exc:
                raise ValidationError("节点返回值无法序列化为 JSON") from exc
            return update
        except Exception as exc:
            log_error(exc)
            raise
        finally:
            context.reset(token)

    return run


def build_planning_graph():
    graph = StateGraph(PlanningState)
    retry = RetryPolicy(max_attempts=3, retry_on=lambda exc: isinstance(
        exc, (ValidationError, TimeoutError, RuntimeError)) or default_retry_on(exc))
    for name, node in {
        "generate_guide": generate_guide, "search_places": search_places, "default_choices": default_choices,
        "coordinator": coordinator, "transport_agent": transport_agent,
        "hotel_agent": hotel_agent, "guide_agent": guide_agent,
        "activity_agent": activity_agent, "route_agent": route_agent, "schedule_agent": schedule_agent,
        "audit_agent": audit_agent,
        "place_agent": place_agent, "prepare_draft": prepare_draft, "itinerary_agent": itinerary_agent,
    }.items():
        graph.add_node(name, _logged_node(name, node), retry_policy=retry)
    graph.add_conditional_edges(START, lambda state: state.get("resume_stage", "coordinator"),
        {name: name for name in ("coordinator", "generate_guide", "search_places", "default_choices", "prepare_draft", "itinerary_agent", "route_agent", "schedule_agent", "audit_agent")})
    graph.add_conditional_edges("coordinator", after_coordinator, {
        "transport_agent": "transport_agent", "guide_agent": "guide_agent",
        "activity_agent": "activity_agent", "route_agent": "route_agent",
        "audit_agent": "audit_agent", "done": END,
        "schedule_agent": "schedule_agent",
        "generate_guide": "generate_guide", "hotel_agent": "hotel_agent", "prepare_draft": "prepare_draft", "place_agent": "place_agent",
    })
    graph.add_edge("generate_guide", "search_places")
    graph.add_edge("search_places", "default_choices")
    graph.add_edge("default_choices", "prepare_draft")
    graph.add_edge("transport_agent", END)
    graph.add_edge("hotel_agent", "activity_agent")
    graph.add_edge("guide_agent", "activity_agent")
    graph.add_edge("activity_agent", "place_agent")
    graph.add_edge("place_agent", END)
    graph.add_edge("prepare_draft", "itinerary_agent")
    graph.add_conditional_edges("itinerary_agent", after_itinerary, {"itinerary_agent": "itinerary_agent", "route_agent": "route_agent", "audit_agent": "audit_agent"})
    graph.add_edge("route_agent", "schedule_agent")
    graph.add_edge("schedule_agent", "audit_agent")
    graph.add_conditional_edges("audit_agent", after_audit, {"itinerary_agent": "itinerary_agent", "done": END})
    return graph.compile()


GRAPH = build_planning_graph()
