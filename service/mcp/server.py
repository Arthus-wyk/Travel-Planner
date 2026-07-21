from __future__ import annotations

"""Mature travel operations retained during domain-by-domain migration.

This module no longer creates an MCP instance or registers tools at import
time. Transport composition belongs exclusively to :mod:`mcp_server`.
"""

import sys
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from service.integrations.travel_google_maps import (
    build_places_payload,
    build_route_payload,
    compute_route,
    google_maps_error,
    place_summary,
    search_places,
)
from service.agent.itinerary_audit import audit_plan
from service.agent.preference_profile import (
    PROFILE_SECTIONS,
    read_preference_profile,
    update_preference_profile,
)
from service.agent.recommendation_ranker import rank_candidates
from service.agent.tool_verifier import verify_tool_result
from service.storage.travel_db import (
    database_error,
    delete_plan,
    get_plan,
    list_plan_summaries,
    now,
    plan_exists,
    save_plan,
)
from service.validation import (
    VALID_ITINERARY_FIELDS,
    VALID_PRIORITIES,
    VALID_PROFILE_FIELDS,
    VALID_RECOMMENDATION_FIELDS,
    next_id,
    validate_date_range,
    validate_list,
    validate_non_negative_number,
    validate_time_of_day,
    time_minutes,
)


def read_travel_preference_profile() -> dict[str, Any]:
    """Read the long-term travel preference profile stored as Markdown.

    Use this before personalized recommendations or when the user asks what
    preferences are remembered.
    """
    return read_preference_profile()


def update_travel_preference_profile(
    updates: dict[str, Any],
    note: str = "",
) -> dict[str, Any]:
    """Update long-term travel preferences in a Markdown profile.

    Valid update categories are travel_interests, budget, trip_length, hotel,
    departure_city, pace, restaurant, constraints, and notes.
    """
    if not isinstance(updates, dict):
        return {
            "success": False,
            "message": "updates must be an object keyed by preference category.",
            "valid_categories": PROFILE_SECTIONS,
        }
    return update_preference_profile(updates, note=note)


def audit_travel_plan(plan_id: str) -> dict[str, Any]:
    """Check a travel plan for consistency, budget, pacing, date, and booking risks."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error
    return audit_plan(plan)


def rank_recommendations(
    candidates: list[dict[str, Any]],
    category: str = "",
    max_results: int = 5,
    preferences: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Rank candidate places with scores, reasons, and trade-offs.

    candidates can be raw Google Places summaries or saved recommendation-like
    objects. preferences can be read from read_travel_preference_profile.
    """
    if not isinstance(candidates, list):
        return {
            "success": False,
            "message": "candidates must be a list of objects.",
        }
    return rank_candidates(
        candidates,
        preferences=preferences or {},
        category=category,
        max_results=max_results,
    )


def verify_last_tool_result(
    tool_name: str,
    arguments: dict[str, Any],
    result_text: str,
) -> dict[str, Any]:
    """Verify whether a previous tool result actually supports claiming completion."""
    if not isinstance(arguments, dict):
        return {
            "success": False,
            "message": "arguments must be an object.",
        }
    return {"success": True, "verification": verify_tool_result(tool_name, arguments, result_text)}


def google_maps_search_places(
    query: str,
    max_results: int = 5,
    language_code: str = "zh-CN",
    region_code: str | None = None,
    location_bias_latitude: float | None = None,
    location_bias_longitude: float | None = None,
    location_bias_radius_meters: float = 5000,
) -> dict[str, Any]:
    """Search Google Places by text query.

    Use this to find hotels, restaurants, attractions, stations, or addresses.
    Provide a natural language query such as "family hotels near Tokyo Station".
    Optionally add a circular location bias with latitude, longitude, and radius.
    """
    payload, validation_error = build_places_payload(
        query,
        max_results,
        language_code,
        region_code,
        location_bias_latitude,
        location_bias_longitude,
        location_bias_radius_meters,
    )
    if validation_error is not None:
        return validation_error

    try:
        places = search_places(payload or {})
    except Exception as exc:
        return google_maps_error(exc)

    return {
        "success": True,
        "query": query,
        "places": [place_summary(place) for place in places],
        "raw_places": places,
    }


def google_maps_search_hotels(
    destination: str,
    query: str = "",
    max_results: int = 5,
    language_code: str = "zh-CN",
    region_code: str | None = None,
) -> dict[str, Any]:
    """Search hotel candidates with Google Places.

    The destination should be a city, area, landmark, or address. Use query for
    constraints such as budget, family friendly, near subway, or ocean view.
    """
    hotel_query = f"hotels in {destination}"
    if query:
        hotel_query = f"{query} hotels in {destination}"
    return google_maps_search_places(
        query=hotel_query,
        max_results=max_results,
        language_code=language_code,
        region_code=region_code,
    )


def google_maps_compute_route(
    origin: str,
    destination: str,
    travel_mode: str = "DRIVE",
    language_code: str = "zh-CN",
    units: str = "METRIC",
    avoid_tolls: bool = False,
    avoid_highways: bool = False,
    avoid_ferries: bool = False,
) -> dict[str, Any]:
    """Compute distance and travel time between two addresses with Google Routes.

    travel_mode can be DRIVE, WALK, BICYCLE, TWO_WHEELER, or TRANSIT.
    """
    payload, validation_error, normalized_travel_mode, _ = build_route_payload(
        origin,
        destination,
        travel_mode,
        language_code,
        units,
        avoid_tolls,
        avoid_highways,
        avoid_ferries,
    )
    if validation_error is not None:
        return validation_error

    try:
        routes = compute_route(payload or {})
    except Exception as exc:
        return google_maps_error(exc)

    route = routes[0] if routes else {}
    return {
        "success": True,
        "origin": origin,
        "destination": destination,
        "travel_mode": normalized_travel_mode,
        "distance_meters": route.get("distanceMeters"),
        "duration": route.get("duration"),
        "static_duration": route.get("staticDuration"),
        "localized_values": route.get("localizedValues"),
        "legs": route.get("legs", []),
        "raw_routes": routes,
    }


def create_travel_plan(
    plan_id: str,
    destination: str,
    start_date: str,
    end_date: str,
    travelers: int,
    budget: float,
    currency: str = "CNY",
    departure_city: str | None = None,
    travel_style: str = "balanced",
    interests: list[str] | None = None,
    constraints: list[str] | None = None,
    pace: str = "moderate",
    notes: str = "",
    title: str | None = None,
) -> dict[str, Any]:
    """Create a custom travel planning workspace.

    Use this first when a user starts a new trip. Dates must use YYYY-MM-DD.
    Interests are things the traveler wants, such as food, museums, nature,
    shopping, family activities, luxury hotels, or photography. Constraints
    are hard requirements such as allergies, mobility limits, visa concerns,
    must-see places, excluded activities, or preferred hotel areas.
    """
    try:
        if plan_exists(plan_id):
            return {
                "success": False,
                "message": "Travel plan already exists.",
            }
    except Exception as exc:
        return database_error(exc)

    date_ok, date_error = validate_date_range(start_date, end_date)
    if not date_ok:
        return {
            "success": False,
            "message": date_error,
        }

    if isinstance(travelers, bool) or not isinstance(travelers, int) or travelers < 1:
        return {
            "success": False,
            "message": "travelers must be a positive integer.",
        }

    budget_ok, budget_error = validate_non_negative_number(budget, "budget")
    if not budget_ok:
        return {
            "success": False,
            "message": budget_error,
        }

    interests = interests or []
    constraints = constraints or []

    interests_ok, interests_error = validate_list(interests, "interests")
    if not interests_ok:
        return {
            "success": False,
            "message": interests_error,
        }

    constraints_ok, constraints_error = validate_list(constraints, "constraints")
    if not constraints_ok:
        return {
            "success": False,
            "message": constraints_error,
        }

    created_at = now()
    plan = {
        "plan_id": plan_id,
        "title": title or f"{destination} 琛岀▼",
        "destination": destination,
        "start_date": start_date,
        "end_date": end_date,
        "departure_city": departure_city,
        "travelers": travelers,
        "budget": budget,
        "currency": currency,
        "travel_style": travel_style,
        "interests": interests,
        "constraints": constraints,
        "pace": pace,
        "notes": notes,
        "created_at": created_at,
        "updated_at": created_at,
        "itinerary": [],
        "routes": [],
        "recommendations": [],
        "budget_items": [],
    }

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "plan": plan,
    }


def update_travel_profile(
    plan_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Update traveler preferences or high-level trip requirements.

    Supported fields are destination, start_date, end_date, departure_city,
    travelers, budget, currency, travel_style, interests, constraints, pace,
    and notes. Only include fields that should change.
    """
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    if not updates:
        return {
            "success": False,
            "message": "No updates provided.",
        }

    invalid_fields = set(updates) - VALID_PROFILE_FIELDS
    if invalid_fields:
        return {
            "success": False,
            "message": f"Invalid profile fields: {', '.join(sorted(invalid_fields))}.",
        }

    start_date = updates.get("start_date", plan["start_date"])
    end_date = updates.get("end_date", plan["end_date"])
    date_ok, date_error = validate_date_range(start_date, end_date)
    if not date_ok:
        return {
            "success": False,
            "message": date_error,
        }

    if "travelers" in updates:
        travelers = updates["travelers"]
        if isinstance(travelers, bool) or not isinstance(travelers, int) or travelers < 1:
            return {
                "success": False,
                "message": "travelers must be a positive integer.",
            }

    if "budget" in updates:
        budget_ok, budget_error = validate_non_negative_number(
            updates["budget"],
            "budget",
        )
        if not budget_ok:
            return {
                "success": False,
                "message": budget_error,
            }

    for field_name in ("interests", "constraints"):
        if field_name in updates:
            valid, message = validate_list(updates[field_name], field_name)
            if not valid:
                return {
                    "success": False,
                    "message": message,
                }

    for field, value in updates.items():
        plan[field] = value

    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "plan": plan,
    }


def add_itinerary_item(
    plan_id: str,
    day: int,
    time_of_day: str,
    title: str,
    location: str,
    category: str,
    duration_minutes: int | None = None,
    estimated_cost: float = 0,
    booking_required: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    """Add one scheduled item to a custom trip itinerary.

    The day field is 1-based relative to the trip start date. time_of_day can
    be a concrete time like 09:30 or a bucket such as morning, afternoon,
    evening, or flexible.
    """
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    if isinstance(day, bool) or not isinstance(day, int) or day < 1:
        return {
            "success": False,
            "message": "day must be a positive integer.",
        }

    time_ok, time_error = validate_time_of_day(time_of_day)
    if not time_ok:
        return {"success": False, "message": time_error}

    if duration_minutes is not None:
        if (
            isinstance(duration_minutes, bool)
            or not isinstance(duration_minutes, int)
            or duration_minutes < 1
        ):
            return {
                "success": False,
                "message": "duration_minutes must be a positive integer.",
            }

    cost_ok, cost_error = validate_non_negative_number(
        estimated_cost,
        "estimated_cost",
    )
    if not cost_ok:
        return {
            "success": False,
            "message": cost_error,
        }

    item = {
        "item_id": next_id(plan["itinerary"], "itinerary"),
        "day": day,
        "time_of_day": time_of_day,
        "title": title,
        "location": location,
        "category": category,
        "duration_minutes": duration_minutes,
        "estimated_cost": estimated_cost,
        "booking_required": booking_required,
        "notes": notes,
    }

    start = time_minutes(time_of_day)
    if duration_minutes is not None and start is not None:
        end = start + duration_minutes
        for existing in plan["itinerary"]:
            if existing.get("day") != day:
                continue
            existing_start = time_minutes(existing.get("time_of_day"))
            existing_duration = existing.get("duration_minutes")
            if existing_start is None or not isinstance(existing_duration, int):
                continue
            existing_end = existing_start + existing_duration
            if start < existing_end and existing_start < end:
                return {
                    "success": False,
                    "message": (
                        f"time overlaps with itinerary item {existing.get('item_id')} "
                        f"({existing.get('time_of_day')})."
                    ),
                }

    existing_day_item_ids = {
        entry.get("item_id")
        for entry in plan["itinerary"]
        if entry.get("day") == day and entry.get("item_id")
    }
    plan["itinerary"].append(item)
    _remove_routes_for_items(plan, existing_day_item_ids)
    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "item": item,
    }


def update_itinerary_item(
    plan_id: str,
    item_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Update an itinerary item by id.

    Supported fields are day, time_of_day, title, location, category,
    duration_minutes, estimated_cost, booking_required, and notes.
    """
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    if not updates:
        return {
            "success": False,
            "message": "No updates provided.",
        }

    invalid_fields = set(updates) - VALID_ITINERARY_FIELDS
    if invalid_fields:
        return {
            "success": False,
            "message": f"Invalid itinerary fields: {', '.join(sorted(invalid_fields))}.",
        }

    item = next(
        (entry for entry in plan["itinerary"] if entry["item_id"] == item_id),
        None,
    )
    if item is None:
        return {
            "success": False,
            "message": "Itinerary item not found.",
        }

    if "day" in updates:
        day = updates["day"]
        if isinstance(day, bool) or not isinstance(day, int) or day < 1:
            return {
                "success": False,
                "message": "day must be a positive integer.",
            }

    if "time_of_day" in updates:
        time_ok, time_error = validate_time_of_day(updates["time_of_day"])
        if not time_ok:
            return {"success": False, "message": time_error}

    if "duration_minutes" in updates and updates["duration_minutes"] is not None:
        duration_minutes = updates["duration_minutes"]
        if (
            isinstance(duration_minutes, bool)
            or not isinstance(duration_minutes, int)
            or duration_minutes < 1
        ):
            return {
                "success": False,
                "message": "duration_minutes must be a positive integer.",
            }

    if "estimated_cost" in updates:
        cost_ok, cost_error = validate_non_negative_number(
            updates["estimated_cost"],
            "estimated_cost",
        )
        if not cost_ok:
            return {
                "success": False,
                "message": cost_error,
            }

    candidate = dict(item)
    candidate.update(updates)
    candidate_start = time_minutes(candidate.get("time_of_day"))
    candidate_duration = candidate.get("duration_minutes")
    if candidate_start is not None and isinstance(candidate_duration, int):
        candidate_end = candidate_start + candidate_duration
        for existing in plan["itinerary"]:
            if existing is item or existing.get("day") != candidate.get("day"):
                continue
            existing_start = time_minutes(existing.get("time_of_day"))
            existing_duration = existing.get("duration_minutes")
            if existing_start is None or not isinstance(existing_duration, int):
                continue
            if candidate_start < existing_start + existing_duration and existing_start < candidate_end:
                return {
                    "success": False,
                    "message": f"time overlaps with itinerary item {existing.get('item_id')}.",
                }

    for field, value in updates.items():
        item[field] = value

    _remove_routes_for_items(plan, {item_id})
    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "item": item,
    }


def _same_day_items(plan: dict[str, Any], day: int) -> list[dict[str, Any]]:
    return sorted(
        [item for item in plan.get("itinerary", []) if item.get("day") == day],
        key=lambda item: (
            time_minutes(item.get("time_of_day"))
            if time_minutes(item.get("time_of_day")) is not None
            else 1440,
            item.get("item_id", ""),
        ),
    )


def _conflicting_item(
    plan: dict[str, Any],
    candidate: dict[str, Any],
    *,
    exclude_item_id: str | None = None,
) -> dict[str, Any] | None:
    start = time_minutes(candidate.get("time_of_day"))
    duration = candidate.get("duration_minutes")
    if start is None or not isinstance(duration, int):
        return None
    end = start + duration
    for existing in plan.get("itinerary", []):
        if existing.get("item_id") == exclude_item_id:
            continue
        if existing.get("day") != candidate.get("day"):
            continue
        existing_start = time_minutes(existing.get("time_of_day"))
        existing_duration = existing.get("duration_minutes")
        if existing_start is None or not isinstance(existing_duration, int):
            continue
        if start < existing_start + existing_duration and existing_start < end:
            return existing
    return None


def _route_id(
    origin_item_id: str,
    destination_item_id: str,
    travel_mode: str,
) -> str:
    return f"route:{origin_item_id}:{destination_item_id}:{travel_mode.lower()}"


def _remove_routes_for_items(
    plan: dict[str, Any],
    item_ids: set[str],
) -> None:
    plan["routes"] = [
        route for route in plan.get("routes", [])
        if route.get("origin_item_id") not in item_ids
        and route.get("destination_item_id") not in item_ids
    ]


def delete_itinerary_item(plan_id: str, item_id: str) -> dict[str, Any]:
    """Delete one itinerary item and invalidate routes touching it."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    item = next(
        (entry for entry in plan.get("itinerary", []) if entry.get("item_id") == item_id),
        None,
    )
    if item is None:
        return {"success": False, "message": "Itinerary item not found."}

    affected_day = item.get("day")
    plan["itinerary"] = [
        entry for entry in plan["itinerary"] if entry.get("item_id") != item_id
    ]
    _remove_routes_for_items(plan, {item_id})
    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    affected_items = [
        entry.get("item_id")
        for entry in _same_day_items(plan, affected_day)
        if entry.get("item_id")
    ] if isinstance(affected_day, int) else []
    return {
        "success": True,
        "deleted_item_id": item_id,
        "affected_days": [affected_day] if affected_day is not None else [],
        "affected_item_ids": affected_items,
    }


def move_itinerary_item(
    plan_id: str,
    item_id: str,
    target_day: int,
    target_time_of_day: str,
) -> dict[str, Any]:
    """Move an itinerary item after validating the target time slot."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error
    if isinstance(target_day, bool) or not isinstance(target_day, int) or target_day < 1:
        return {"success": False, "message": "target_day must be a positive integer."}
    time_ok, time_error = validate_time_of_day(target_time_of_day)
    if not time_ok:
        return {"success": False, "message": time_error}

    item = next(
        (entry for entry in plan.get("itinerary", []) if entry.get("item_id") == item_id),
        None,
    )
    if item is None:
        return {"success": False, "message": "Itinerary item not found."}

    old_day = item.get("day")
    candidate = dict(item, day=target_day, time_of_day=target_time_of_day)
    conflict = _conflicting_item(plan, candidate, exclude_item_id=item_id)
    if conflict is not None:
        return {
            "success": False,
            "message": f"time overlaps with itinerary item {conflict.get('item_id')}.",
            "conflicting_item_id": conflict.get("item_id"),
        }

    item["day"] = target_day
    item["time_of_day"] = target_time_of_day
    _remove_routes_for_items(plan, {item_id})
    plan["updated_at"] = now()
    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    affected_days = list(dict.fromkeys(day for day in (old_day, target_day) if isinstance(day, int)))
    affected_item_ids = [
        entry.get("item_id")
        for day in affected_days
        for entry in _same_day_items(plan, day)
        if entry.get("item_id")
    ]
    return {
        "success": True,
        "item": item,
        "affected_days": affected_days,
        "affected_item_ids": list(dict.fromkeys(affected_item_ids)),
    }


def update_itinerary_routes(
    plan_id: str,
    routes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upsert calculated route records into a travel plan."""
    if not isinstance(routes, list):
        return {"success": False, "message": "routes must be a list of objects."}
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    existing = {
        route.get("route_id"): route
        for route in plan.get("routes", [])
        if route.get("route_id")
    }
    itinerary_ids = {
        item.get("item_id") for item in plan.get("itinerary", [])
    }
    saved_routes = []
    for route in routes:
        if not isinstance(route, dict):
            return {"success": False, "message": "Each route must be an object."}
        required = {"origin_item_id", "destination_item_id", "travel_mode"}
        missing = required - set(route)
        if missing:
            return {
                "success": False,
                "message": f"Route is missing fields: {', '.join(sorted(missing))}.",
            }
        if (
            route["origin_item_id"] not in itinerary_ids
            or route["destination_item_id"] not in itinerary_ids
        ):
            return {
                "success": False,
                "message": "Route item references must point to itinerary items in this plan.",
            }
        if route["origin_item_id"] == route["destination_item_id"]:
            return {"success": False, "message": "Route endpoints must be different items."}
        normalized_mode = str(route["travel_mode"]).upper()
        if normalized_mode not in {"DRIVE", "WALK", "BICYCLE", "TWO_WHEELER", "TRANSIT"}:
            return {"success": False, "message": "Unsupported route travel_mode."}
        record = dict(route)
        record["travel_mode"] = normalized_mode
        record["route_id"] = record.get("route_id") or _route_id(
            str(record["origin_item_id"]),
            str(record["destination_item_id"]),
            str(record["travel_mode"]),
        )
        record["status"] = record.get("status", "valid")
        record["updated_at"] = now()
        existing[record["route_id"]] = record
        saved_routes.append(record)

    plan["routes"] = list(existing.values())
    plan["updated_at"] = now()
    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)
    return {"success": True, "routes": saved_routes}


def _route_record(
    origin: dict[str, Any],
    destination: dict[str, Any],
    route: dict[str, Any],
    travel_mode: str,
) -> dict[str, Any]:
    return {
        "route_id": _route_id(origin["item_id"], destination["item_id"], travel_mode),
        "origin_item_id": origin["item_id"],
        "destination_item_id": destination["item_id"],
        "origin": origin.get("location"),
        "destination": destination.get("location"),
        "travel_mode": travel_mode,
        "distance_meters": route.get("distanceMeters"),
        "duration": route.get("duration"),
        "static_duration": route.get("staticDuration"),
        "localized_values": route.get("localizedValues"),
        "status": "valid",
    }


def reconcile_travel_plan(
    plan_id: str,
    affected_days: list[int] | None = None,
    affected_item_ids: list[str] | None = None,
    travel_mode: str = "DRIVE",
) -> dict[str, Any]:
    """Recompute affected itinerary routes and return consistency suggestions."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error
    if not isinstance(travel_mode, str) or travel_mode.upper() not in {
        "DRIVE", "WALK", "BICYCLE", "TWO_WHEELER", "TRANSIT",
    }:
        return {"success": False, "message": "Unsupported travel_mode."}
    travel_mode = travel_mode.upper()

    item_ids = set(affected_item_ids or [])
    days = set(affected_days or [])
    if item_ids:
        days.update(
            item.get("day") for item in plan.get("itinerary", [])
            if item.get("item_id") in item_ids and isinstance(item.get("day"), int)
        )
    if not days:
        days = {
            item.get("day") for item in plan.get("itinerary", [])
            if isinstance(item.get("day"), int)
        }

    refreshed_routes: list[dict[str, Any]] = []
    route_errors: list[dict[str, Any]] = []
    affected_route_item_ids: set[str] = set()
    for day in sorted(days):
        items = _same_day_items(plan, day)
        day_item_ids = {item["item_id"] for item in items}
        _remove_routes_for_items(plan, day_item_ids)
        if len(items) < 2:
            continue
        for origin, destination in zip(items, items[1:]):
            try:
                payload, validation_error, _, _ = build_route_payload(
                    origin.get("location", ""),
                    destination.get("location", ""),
                    travel_mode,
                    "zh-CN",
                    "METRIC",
                    False,
                    False,
                    False,
                )
                if validation_error is not None:
                    route_errors.append(validation_error)
                    continue
                routes = compute_route(payload or {})
                if not routes:
                    route_errors.append({
                        "message": f"No route found from {origin.get('location')} to {destination.get('location')}."
                    })
                    continue
                record = _route_record(origin, destination, routes[0], travel_mode)
                plan["routes"].append(record)
                refreshed_routes.append(record)
                affected_route_item_ids.update((origin["item_id"], destination["item_id"]))
            except Exception as exc:
                route_errors.append(google_maps_error(exc))

    audit = audit_plan(plan)
    suggestions = list(audit.get("suggestions", []))
    for route in refreshed_routes:
        duration = route.get("duration")
        match = re.fullmatch(r"(\d+)s", str(duration or ""))
        if not match:
            continue
        origin = next((item for item in plan["itinerary"] if item.get("item_id") == route["origin_item_id"]), None)
        destination = next((item for item in plan["itinerary"] if item.get("item_id") == route["destination_item_id"]), None)
        if not origin or not destination:
            continue
        origin_end = time_minutes(origin.get("time_of_day"))
        if origin_end is not None and isinstance(origin.get("duration_minutes"), int):
            origin_end += origin["duration_minutes"]
        destination_start = time_minutes(destination.get("time_of_day"))
        if origin_end is not None and destination_start is not None and origin_end + int(match.group(1)) // 60 > destination_start:
            suggestions.append(
                f"{origin.get('title')} 到 {destination.get('title')} 的交通时间可能不足，建议调整 {destination.get('title')} 的开始时间。"
            )

    plan["updated_at"] = now()
    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)
    return {
        "success": True,
        "plan_id": plan_id,
        "affected_days": sorted(days),
        "affected_item_ids": sorted(affected_route_item_ids or item_ids),
        "routes": refreshed_routes,
        "route_errors": route_errors,
        "audit": audit,
        "suggestions": suggestions,
    }


def list_itinerary(
    plan_id: str,
    day: int | None = None,
) -> dict[str, Any]:
    """Return itinerary items for a travel plan, optionally filtered by day."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    itinerary = plan["itinerary"]
    if day is not None:
        itinerary = [item for item in itinerary if item["day"] == day]

    return {
        "success": True,
        "itinerary": sorted(
            itinerary,
            key=lambda item: (
                item["day"],
                time_minutes(item.get("time_of_day"))
                if time_minutes(item.get("time_of_day")) is not None
                else 1440,
                item.get("item_id", ""),
            ),
        ),
    }


def add_recommendation(
    plan_id: str,
    name: str,
    category: str,
    location: str,
    why: str,
    tags: list[str] | None = None,
    estimated_cost: float = 0,
    priority: str = "medium",
    notes: str = "",
) -> dict[str, Any]:
    """Add a candidate hotel, restaurant, attraction, route, or local experience.

    Use recommendations for options that have not yet been placed into the
    day-by-day itinerary. priority must be low, medium, or high.
    """
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    tags = tags or []
    tags_ok, tags_error = validate_list(tags, "tags")
    if not tags_ok:
        return {
            "success": False,
            "message": tags_error,
        }

    cost_ok, cost_error = validate_non_negative_number(
        estimated_cost,
        "estimated_cost",
    )
    if not cost_ok:
        return {
            "success": False,
            "message": cost_error,
        }

    if priority not in VALID_PRIORITIES:
        return {
            "success": False,
            "message": "priority must be low, medium, or high.",
        }

    recommendation = {
        "recommendation_id": next_id(plan["recommendations"], "rec"),
        "name": name,
        "category": category,
        "location": location,
        "why": why,
        "tags": tags,
        "estimated_cost": estimated_cost,
        "priority": priority,
        "notes": notes,
    }

    plan["recommendations"].append(recommendation)
    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "recommendation": recommendation,
    }


def update_recommendation(
    plan_id: str,
    recommendation_id: str,
    updates: dict[str, Any],
) -> dict[str, Any]:
    """Update a candidate recommendation by id."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    if not updates:
        return {
            "success": False,
            "message": "No updates provided.",
        }

    invalid_fields = set(updates) - VALID_RECOMMENDATION_FIELDS
    if invalid_fields:
        return {
            "success": False,
            "message": f"Invalid recommendation fields: {', '.join(sorted(invalid_fields))}.",
        }

    recommendation = next(
        (
            entry
            for entry in plan["recommendations"]
            if entry["recommendation_id"] == recommendation_id
        ),
        None,
    )
    if recommendation is None:
        return {
            "success": False,
            "message": "Recommendation not found.",
        }

    if "tags" in updates:
        tags_ok, tags_error = validate_list(updates["tags"], "tags")
        if not tags_ok:
            return {
                "success": False,
                "message": tags_error,
            }

    if "estimated_cost" in updates:
        cost_ok, cost_error = validate_non_negative_number(
            updates["estimated_cost"],
            "estimated_cost",
        )
        if not cost_ok:
            return {
                "success": False,
                "message": cost_error,
            }

    if "priority" in updates and updates["priority"] not in VALID_PRIORITIES:
        return {
            "success": False,
            "message": "priority must be low, medium, or high.",
        }

    for field, value in updates.items():
        recommendation[field] = value

    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "recommendation": recommendation,
    }


def add_budget_item(
    plan_id: str,
    category: str,
    amount: float,
    description: str,
) -> dict[str, Any]:
    """Add a budget line item such as flights, hotels, food, tickets, or transit."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    amount_ok, amount_error = validate_non_negative_number(amount, "amount")
    if not amount_ok:
        return {
            "success": False,
            "message": amount_error,
        }

    item = {
        "budget_item_id": next_id(plan["budget_items"], "budget"),
        "category": category,
        "amount": amount,
        "description": description,
    }

    plan["budget_items"].append(item)
    plan["updated_at"] = now()

    try:
        save_plan(plan)
    except Exception as exc:
        return database_error(exc)

    return {
        "success": True,
        "budget_item": item,
    }


def get_travel_plan(plan_id: str) -> dict[str, Any]:
    """Return a full travel plan with profile, itinerary, recommendations, and budget."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    return {
        "success": True,
        "plan": plan,
    }


def summarize_travel_plan(plan_id: str) -> dict[str, Any]:
    """Return a compact planning summary with budget totals and coverage by day."""
    plan, error = get_plan(plan_id)
    if error is not None:
        return error

    itinerary_cost = sum(item["estimated_cost"] for item in plan["itinerary"])
    recommendation_cost = sum(
        item["estimated_cost"] for item in plan["recommendations"]
    )
    budget_total = sum(item["amount"] for item in plan["budget_items"])
    days = sorted({item["day"] for item in plan["itinerary"]})

    return {
        "success": True,
        "summary": {
            "plan_id": plan["plan_id"],
            "destination": plan["destination"],
            "dates": f"{plan['start_date']} to {plan['end_date']}",
            "travelers": plan["travelers"],
            "budget": plan["budget"],
            "currency": plan["currency"],
            "interests": plan["interests"],
            "constraints": plan["constraints"],
            "scheduled_days": days,
            "itinerary_item_count": len(plan["itinerary"]),
            "recommendation_count": len(plan["recommendations"]),
            "budget_item_count": len(plan["budget_items"]),
            "estimated_itinerary_cost": itinerary_cost,
            "estimated_recommendation_cost": recommendation_cost,
            "budget_item_total": budget_total,
            "remaining_budget_after_budget_items": plan["budget"] - budget_total,
        },
    }


def list_travel_plans() -> dict[str, Any]:
    """Return compact summaries of all travel planning workspaces."""
    plans, error = list_plan_summaries()
    if error is not None:
        return error

    return {
        "success": True,
        "plans": plans,
    }


def delete_travel_plan(plan_id: str) -> dict[str, Any]:
    """Delete a travel planning workspace by id."""
    deleted, error = delete_plan(plan_id)
    if error is not None:
        return error

    if deleted == 0:
        return {
            "success": False,
            "message": "Travel plan not found.",
        }

    return {
        "success": True,
        "message": f"Travel plan {plan_id} deleted.",
    }


def main() -> None:
    """Compatibility entry point; MCP composition lives in ``mcp_server``."""
    from mcp_server import main as run_mcp_server

    run_mcp_server()

if __name__ == "__main__":
    main()

