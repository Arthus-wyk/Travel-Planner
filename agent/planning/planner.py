"""Public entry point for form-led planning actions."""
from copy import deepcopy
from typing import Any
from agent.planning.schemas.state import PlanningState
from agent.planning.runtime.agent_loop import GRAPH
from service.utils.model_client import get_model, selected_model, validate_model

def run_planning_action(plan: dict[str, Any], action: str, payload: dict[str, Any], progress=None, resume=None) -> PlanningState:
    initial = deepcopy(resume) if resume else {"plan": deepcopy(plan), "action": action, "payload": payload, "trace": []}
    if initial.get("resume_stage") == "done":
        return initial
    model = validate_model(payload["model"]) if "model" in payload else get_model()
    token = selected_model.set(model)
    try:
        if progress is None:
            return GRAPH.invoke(initial)
        outcome = initial
        for outcome in GRAPH.stream(initial, stream_mode="values"):
            if outcome.get("trace") and (not resume or outcome["trace"] != resume.get("trace")):
                progress(outcome["trace"][-1], outcome)
        return outcome
    finally:
        selected_model.reset(token)


