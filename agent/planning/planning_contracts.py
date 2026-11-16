"""JSON Schema for partial PlanningState updates emitted by graph nodes."""

from jsonschema import Draft202012Validator


def _object(required=None, optional=None, *, additional=False):
    required = required or {}
    return {
        "type": "object", "properties": {**required, **(optional or {})},
        "required": list(required), "additionalProperties": additional,
    }


def _array(items):
    return {"type": "array", "items": items}


_TEXT = {"type": "string"}
_NONEMPTY = {"type": "string", "minLength": 1}
_COUNT = {"type": "integer", "minimum": 0}
_BOOL = {"type": "boolean"}
_OBJECTS = _array({"type": "object"})
_ACTIVITIES = _array(_object({"id": _NONEMPTY, "name": _NONEMPTY}, additional=True))
_ROUTES = _array(_object({
    "origin_item_id": _NONEMPTY, "destination_item_id": _NONEMPTY,
}, additional=True))
_ISSUES = _array(_object({"type": _NONEMPTY, "severity": _NONEMPTY, "message": _TEXT},
                        additional=True))
_PLAN = _object({
    "destination": _NONEMPTY, "departure_city": _NONEMPTY,
    "start_date": _NONEMPTY, "end_date": _NONEMPTY,
    "planning": _object({
        "stage": _NONEMPTY, "activities": _ACTIVITIES, "hotel_candidates": _OBJECTS,
        "transport_candidates": _OBJECTS, "selected_transport": _object(optional={
            "outbound": _NONEMPTY, "return": _NONEMPTY,
        }),
    }, additional=True),
}, {"itinerary": _OBJECTS, "routes": _ROUTES}, additional=True)
_HISTORY = _array({
    "type": "array", "minItems": 2, "maxItems": 2,
    "prefixItems": [{"type": ["string", "null"]}, _TEXT],
})


def _update(*trace, fields=None):
    return _object({
        "trace": {"type": "array", "items": _NONEMPTY, "minItems": len(trace),
                  "allOf": [{"contains": {"const": label}} for label in trace]},
        **(fields if fields is not None else {"plan": _PLAN}),
    })


_SCHEMAS = {
    **{name: _update(name) for name in (
        "coordinator", "transport_agent", "hotel_agent", "activity_agent", "place_agent",
        "schedule_agent", "default_choices",
    )},
    "guide_agent": _update("guide_agent", fields={"guide_text": _NONEMPTY}),
    "route_agent": {"oneOf": [
        _update("route_agent"),
        _update("route_agent", fields={"plan": _PLAN, "optimization_message": _NONEMPTY, "optimization_applied": _BOOL}),
    ]},
    "generate_guide": _update("guide_agent", "activity_agent"),
    "search_places": _update("place_search_agent", "place_match_agent"),
    "prepare_draft": _update("prepare_draft", fields={
        "plan": _PLAN, "attempts": _COUNT, "format_attempts": _COUNT,
        "revision_attempts": _COUNT, "revision_failed": _BOOL,
        "feedback": _ISSUES, "proposal_error": _TEXT, "itinerary_retry_history": _HISTORY,
        "forced_unscheduled": _array(_object({"activity_id": _NONEMPTY, "reason": _TEXT})),
        "input_hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    }),
    "itinerary_agent": {"oneOf": [
        _update("itinerary_agent", fields={
            "plan": _PLAN, "attempts": _COUNT, "format_attempts": _COUNT,
            "proposal_error": {"const": ""}, "itinerary_retry_history": _HISTORY,
        }),
        _update("itinerary_agent", fields={
            "attempts": _COUNT, "format_attempts": _COUNT, "proposal_error": _NONEMPTY,
            "itinerary_retry_history": _HISTORY,
        }),
        _update("itinerary_agent", fields={
            "plan": _PLAN, "proposal_error": {"const": ""}, "revision_failed": {"const": True},
            "itinerary_retry_history": _HISTORY,
        }),
    ]},
    "audit_agent": _update("audit_agent", fields={
        "plan": _PLAN, "feedback": _ISSUES, "revision_attempts": _COUNT,
        "audit": _object({"issues": _ISSUES, "guide_constraints": _OBJECTS}, additional=True),
    }),
}

NODE_OUTPUT_VALIDATORS = {name: Draft202012Validator(schema) for name, schema in _SCHEMAS.items()}
