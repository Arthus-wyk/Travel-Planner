from typing import Any, TypedDict

class PlanningState(TypedDict, total=False):
    resume_stage: str
    plan: dict[str, Any]
    action: str
    payload: dict[str, Any]
    guide_text: str
    audit: dict[str, Any]
    trace: list[str]
    attempts: int
    format_attempts: int
    revision_attempts: int
    revision_failed: bool
    feedback: list
    proposal_error: str
    itinerary_retry_history: list[tuple[str | None, str]]
    input_hash: str
    forced_unscheduled: list
    optimization_message: str
    optimization_applied: bool


