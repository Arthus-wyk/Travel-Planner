"""Pure, form-led travel planning operations shared by the guided API and graph."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256
from math import ceil, isfinite
import re
from typing import Any

from service.storage.travel_db import now
from service.travel.guide import analyze_guide, split_guide_days
from service.mcp.server import amap_compute_route


PLACE_CATEGORIES = {"attraction", "food", "hotel", "other"}
MOVABLE_CATEGORIES = {"attraction", "food", "other"}
TRANSPORT_LABELS = {"train": "火车", "flight": "飞机", "drive": "自驾", "bus": "大巴"}
_UNSET = object()


def _positive_money(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a non-negative number")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a non-negative number") from exc
    if not isfinite(amount) or amount < 0:
        raise ValueError(f"{field} must be a non-negative number")
    return amount


def create_draft(form: dict[str, Any], plan_id: str) -> dict[str, Any]:
    for field in ("destination", "departure_city", "start_date"):
        if not isinstance(form.get(field), str) or not form[field].strip():
            raise ValueError(f"{field} is required")
    trip_days = form.get("trip_days")
    if trip_days is not None and (isinstance(trip_days, bool) or not isinstance(trip_days, int) or not 1 <= trip_days <= 365):
        raise ValueError("trip_days must be an integer from 1 to 365")
    try:
        start = date.fromisoformat(form["start_date"])
        if trip_days is not None:
            end = start + timedelta(days=trip_days - 1)
            given_end = date.fromisoformat(form["end_date"]) if form.get("end_date") else None
        else:
            end = date.fromisoformat(form["end_date"])
            given_end = None
    except KeyError as exc:
        raise ValueError("end_date or trip_days is required") from exc
    except ValueError as exc:
        raise ValueError("Dates must use YYYY-MM-DD") from exc
    if given_end is not None and given_end != end:
        raise ValueError("end_date and trip_days disagree")
    if end < start:
        raise ValueError("end_date must not precede start_date")
    travelers = form.get("travelers")
    if isinstance(travelers, bool) or not isinstance(travelers, int) or travelers < 1:
        raise ValueError("travelers must be a positive integer")
    budget = _positive_money(form.get("budget"), "budget")
    if form.get("currency", "CNY") != "CNY":
        raise ValueError("Guided planning currently uses CNY only")
    daily_food = _positive_money(form.get("daily_food_per_person", 100), "daily_food_per_person")
    daily_transit = _positive_money(form.get("daily_local_transit_per_person", 30), "daily_local_transit_per_person")
    interests = form.get("interests") or []
    constraints = form.get("constraints") or []
    if any(not isinstance(values, list) or any(not isinstance(x, str) for x in values)
           for values in (interests, constraints)):
        raise ValueError("interests and constraints must be text lists")
    timestamp = now()
    return {
        "plan_id": plan_id,
        "title": str(form.get("title") or f"{form['destination'].strip()} 行程"),
        "destination": form["destination"].strip(),
        "departure_city": form["departure_city"].strip(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "travelers": travelers,
        "budget": budget,
        "currency": "CNY",
        "travel_style": str(form.get("travel_style") or "balanced"),
        "interests": interests,
        "constraints": constraints,
        "pace": str(form.get("pace") or "moderate"),
        "notes": str(form.get("notes") or ""),
        "created_at": timestamp,
        "updated_at": timestamp,
        "itinerary": [], "routes": [], "recommendations": [], "budget_items": [],
        "planning": {
            "stage": "requirements",
            "home_location": str(form.get("home_location") or form["departure_city"]).strip(),
            "daily_food_per_person": daily_food,
            "daily_local_transit_per_person": daily_transit,
            "transport_candidates": [], "hotel_candidates": [], "activities": [],
            "selected_transport": {}, "selected_hotel": None, "purchases": [],
            "guide_text": "", "guide_places": [],
        },
    }


def _candidate_id(prefix: str, *parts: str) -> str:
    return prefix + "-" + sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def build_candidates(plan: dict[str, Any], hotel_places: list[dict] | None = None) -> dict[str, Any]:
    updated = deepcopy(plan)
    planning = updated.setdefault("planning", {})
    origin, destination = updated["departure_city"], updated["destination"]
    candidates = []
    for direction, day, start, end in (
        ("outbound", updated["start_date"], origin, destination),
        ("return", updated["end_date"], destination, origin),
    ):
        for mode, times, base, spread in (
            ("train", (("07:30", "10:30"), ("09:00", "12:00")) if direction == "outbound" else (("17:00", "20:00"), ("19:00", "22:00")), 120, 280),
            ("flight", (("07:00", "09:00"), ("10:00", "12:00")) if direction == "outbound" else (("17:30", "19:30"), ("19:30", "21:30")), 420, 680),
            ("bus", (("07:00", "11:00"),) if direction == "outbound" else (("17:00", "21:00"),), 70, 100),
        ):
            for index, (depart, arrive) in enumerate(times, 1):
                candidates.append({
                    "id": _candidate_id(f"{direction}-{mode}", start, end, day, str(index)), "kind": mode,
                    "direction": direction, "origin": start, "destination": end,
                    "date": day, "depart_time": depart, "arrive_time": arrive,
                    "price_per_person": None, "estimated_total": None,
                    "price_source": "unknown", "cost_unknown": True,
                })
        candidates.append({
            "id": _candidate_id(f"{direction}-drive", start, end, day), "kind": "drive",
            "direction": direction, "origin": start, "destination": end, "date": day,
            "depart_time": "07:00" if direction == "outbound" else "17:00",
            "arrive_time": None, "estimated_total": 0, "price_source": "unknown",
            "cost_unknown": True, "vehicles": 1,
        })
    old_transport = {x["id"]: x for x in planning.get("transport_candidates", [])}
    for candidate in candidates:
        if candidate["id"] in old_transport:
            candidate.update(old_transport[candidate["id"]])
        if candidate.get("price_source") == "simulation":
            candidate.update(price_per_person=None, estimated_total=None, price_source="unknown", cost_unknown=True)
    nights = (date.fromisoformat(updated["end_date"]) - date.fromisoformat(updated["start_date"])).days
    places = (hotel_places or [])[:3]
    if not places and nights:
        places = [{"name": f"{destination}住宿方案 {index}", "address": f"{destination}市区", "place_id": None} for index in range(1, 4)]
    hotels = []
    for index, place in enumerate(places, 1):
        hotel_key = str(place.get("place_id") or place.get("name") or f"住宿方案 {index}")
        hotels.append({
            "id": _candidate_id("hotel", destination, updated["start_date"], updated["end_date"], hotel_key),
            "name": place.get("name") or f"{destination}住宿方案 {index}",
            "location": place.get("address") or destination,
            "place_id": place.get("place_id"), "nights": nights,
            "place": place if place.get("latitude") is not None else None,
            "price_per_night": None, "estimated_total": None,
            "price_source": "unknown", "cost_unknown": True,
            "listing_source": "高德 POI" if place.get("place_id") else "simulation",
        })
    planning["transport_candidates"] = candidates
    planning["hotel_candidates"] = hotels if hotel_places is not None or not planning.get("hotel_candidates") else planning["hotel_candidates"]
    planning["stage"] = "candidates"
    planning["needs_replan"] = bool(updated.get("itinerary"))
    return updated


def select_candidates(plan: dict[str, Any], selection: dict[str, str]) -> dict[str, Any]:
    updated = deepcopy(plan)
    planning = updated.get("planning") or {}
    transports = planning.get("transport_candidates") or []
    hotels = planning.get("hotel_candidates") or []
    for direction in ("outbound", "return"):
        if not any(item["id"] == selection.get(direction) and item["direction"] == direction for item in transports):
            raise ValueError(f"Invalid {direction} candidate")
    chosen = [next(x for x in transports if x["id"] == selection[d]) for d in ("outbound", "return")]
    if any(x["kind"] == "drive" for x in chosen):
        if not all(x["kind"] == "drive" for x in chosen):
            raise ValueError("自驾需要往返均选择自驾，以便车辆返回")
        vehicles = selection.get("vehicles", 1)
        departures = selection.get("drive_departures", {})
        if isinstance(vehicles, bool) or not isinstance(vehicles, int) or not 1 <= vehicles <= 100 or not isinstance(departures, dict):
            raise ValueError("车辆数必须为 1–100 的整数")
        for item in chosen:
            depart = departures.get(item["direction"], item["depart_time"])
            _validate_clock(depart)
            route = amap_compute_route(item["origin"], item["destination"], travel_mode="DRIVE")
            try:
                seconds = float(str(route.get("duration", "")).removesuffix("s"))
                if not route.get("success") or not isfinite(seconds) or seconds <= 0:
                    raise ValueError
            except (TypeError, ValueError) as exc:
                raise ValueError("无法获取自驾时间，请检查高德配置后重试") from exc
            duration = ceil(seconds / 60)
            arrival = _minute(depart) + duration
            if arrival >= 1440:
                raise ValueError("当前不支持跨日自驾，请提前出发或选择其他交通")
            item.update(depart_time=depart, arrive_time=_clock(arrival), duration_minutes=duration,
                        distance_meters=route.get("distance_meters"), vehicles=vehicles,
                        departure_location=item["origin"], arrival_location=item["destination"])
    if selection.get("hotel") and hotels:
        if not any(item["id"] == selection.get("hotel") for item in hotels):
            raise ValueError("Invalid hotel candidate")
    elif selection.get("hotel"):
        raise ValueError("A day trip has no hotel candidate")
    planning["selected_transport"] = {direction: selection[direction] for direction in ("outbound", "return")}
    if "hotel" in selection:
        planning["selected_hotel"] = selection.get("hotel") or None
    planning["stage"] = "activities"
    planning["needs_replan"] = bool(updated.get("itinerary"))
    return updated


def build_hotel_activities(plan: dict, hotel_places: list[dict] | None = None) -> dict:
    """Populate step two without replacing transport or clearing guide edits."""
    updated = deepcopy(plan)
    state = updated["planning"]
    hotels = build_candidates(updated, hotel_places)["planning"]["hotel_candidates"]
    previous_hotels = {x["id"]: x for x in state.get("hotel_candidates", [])}
    selected = state.get("selected_hotel")
    if selected in previous_hotels and selected not in {x["id"] for x in hotels}:
        hotels.append(previous_hotels[selected])
    state["hotel_candidates"] = hotels
    activities = state.setdefault("activities", [])
    existing = {x["id"]: x for x in activities}
    for hotel in hotels:
        if hotel["id"] not in existing:
            activities.append({"id": hotel["id"], "name": hotel["name"], "location": hotel["location"],
                               "category": "hotel", "source": hotel.get("listing_source", "simulation"),
                               "choice": "optional", "note": "住宿候选，价格待查询", "duration_minutes": 90,
                               "ticket_required": False, "estimated_cost": 0, "visit_tips": "",
                               "place": hotel.get("place"), "place_status": "unresolved"})
    return _sync_hotels(updated)


def _sync_hotels(plan: dict) -> dict:
    state = plan["planning"]
    nights = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days
    hotels = {x["id"]: x for x in state.get("hotel_candidates", [])}
    for activity in state.get("activities", []):
        if activity.get("category", "attraction") != "hotel":
            hotels.pop(activity["id"], None)
            continue
        hotel = hotels.setdefault(activity["id"], {"id": activity["id"], "price_per_night": None,
                                                 "estimated_total": None, "price_source": "unknown", "cost_unknown": True})
        if hotel.get("price_source") == "simulation":
            hotel.update(price_per_night=None, estimated_total=None, price_source="unknown", cost_unknown=True)
        hotel.update(name=activity["name"], location=activity.get("location") or activity["name"],
                     nights=nights, place=activity.get("place"), place_id=(activity.get("place") or {}).get("place_id"))
    state["hotel_candidates"] = list(hotels.values()) if nights else []
    if state.get("selected_hotel") not in hotels or not nights:
        state["selected_hotel"] = None
    return plan


def build_activities(
    plan: dict[str, Any], guide_text: str = "", recommended_places: list[dict] | None = None
) -> dict[str, Any]:
    updated = deepcopy(plan)
    planning = updated.setdefault("planning", {})
    guide = analyze_guide(guide_text)
    planning["guide_text"] = guide["text"]
    planning["guide_days"], planning["guide_reserve"] = split_guide_days(guide["text"])
    planning["guide_places"] = guide["places"]
    planning["lodging_areas"] = guide.get("lodging_areas", [])
    planning["guide_constraints"] = guide.get("constraints", [])
    previous = {(item.get("source_name") or item["name"]).casefold(): item for item in planning.get("activities") or []}
    activities = [
        {**item, "location": item.get("location_hint") or item["name"], "source_name": item["name"],
         "choice": previous.get(item["name"].casefold(), {}).get("choice", "optional"),
         "estimated_cost": previous.get(item["name"].casefold(), {}).get("estimated_cost", 0),
         "duration_minutes": previous.get(item["name"].casefold(), {}).get("duration_minutes", item.get("duration_minutes") or 90),
         "ticket_required": previous.get(item["name"].casefold(), {}).get("ticket_required", item.get("ticket_required")),
         "visit_tips": previous.get(item["name"].casefold(), {}).get("visit_tips", item.get("visit_tips") or item.get("note", ""))}
        for item in guide["places"]
    ]
    names = {item["name"].casefold() for item in activities}
    for place in recommended_places or []:
        name = str(place.get("name") or "").strip()
        if not name or name.casefold() in names:
            continue
        names.add(name.casefold())
        activities.append({
            "id": f"place-{place.get('place_id')}" if place.get("place_id") else _candidate_id("place", name.casefold()),
            "name": name, "location": place.get("address") or name,
            "source": "高德 POI" if place.get("place_id") else place.get("source", "suggestion"),
            "note": place.get("note", ""),
            "category": place.get("category", "attraction"),
            "place": deepcopy(place) if place.get("longitude") is not None and place.get("latitude") is not None else None,
            "choice": previous.get(name.casefold(), {}).get(
                "choice", "exclude" if place.get("source") == "simulated_suggestion" else "optional"
            ),
            "estimated_cost": previous.get(name.casefold(), {}).get("estimated_cost", 0),
            "duration_minutes": previous.get(name.casefold(), {}).get("duration_minutes", 90),
            "ticket_required": previous.get(name.casefold(), {}).get("ticket_required"),
            "visit_tips": previous.get(name.casefold(), {}).get("visit_tips", place.get("note", "")),
        })
    id_mapping = {}
    matched, used = {}, set()
    # Reserve stable identities before matching mutable map display names.
    for field in ("id", "source_name", "name"):
        for index, activity in enumerate(activities):
            if index in matched:
                continue
            old = next((x for x in planning.get("activities", [])
                        if x["id"] not in used and
                        (x["id"] == activity["id"] if field == "id" else
                         (x.get(field) or "").casefold() == activity["name"].casefold())), None)
            if old is not None:
                matched[index] = old
                used.add(old["id"])
    for index, activity in enumerate(activities):
        old = matched.get(index, {})
        if old:
            id_mapping[activity["id"]] = old["id"]
            activity["id"] = old["id"]
            activity.update({key: old[key] for key in ("choice", "estimated_cost", "duration_minutes", "ticket_required", "visit_tips", "manual_added") if key in old})
            if old.get("source") == "manual" or old["id"].startswith("manual-"):
                activity["manual_added"] = True
            if old.get("category_manual"):
                activity.update(category=old.get("category", "attraction"), category_manual=True)
        hint_changed = old.get("location_hint", "") != activity.get("location_hint", "")
        if old.get("place") and (not hint_changed or old.get("place_status") == "manual_confirmed" or old.get("manual_added") or old.get("source") == "manual"):
            activity.update({key: old[key] for key in ("name", "location", "source", "place", "place_status") if key in old})
            if hint_changed:
                activity["place_error"] = "原文地址已变化，已保留手动位置，请核对"
        activity.setdefault("category", "attraction")
        activity.setdefault("place_status", "unresolved")
    included = {x["id"] for x in activities}
    pending_ids = {x.get("reference_id") for x in planning.get("unscheduled_items", []) if x.get("manual_pending")}
    activities += [item for item in planning.get("activities") or []
                   if (item.get("manual_added") or item.get("source") == "manual" or item["id"].startswith("manual-") or item["id"] in pending_ids or
                       item.get("category") == "hotel" and not planning["lodging_areas"] and (not guide_text or item["id"] == planning.get("selected_hotel")))
                   and item["id"] not in included and item["name"].casefold() not in names]
    active_ids = {x["id"] for x in activities}
    planning["hotel_candidates"] = [x for x in planning.get("hotel_candidates", []) if x["id"] in active_ids or
        x["id"] == planning.get("selected_hotel") and not planning["lodging_areas"]]
    for constraint in planning["guide_constraints"]:
        constraint["activity_ids"] = [id_mapping.get(x, x) for x in constraint.get("activity_ids", [])]
    planning["activities"] = activities
    planning["stage"] = "activities"
    return _sync_hotels(updated)


def add_manual_activity(plan: dict[str, Any], name: str, location: str = "", category: str = "attraction") -> dict[str, Any]:
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 100:
        raise ValueError("Activity name must be 1-100 characters")
    if category not in PLACE_CATEGORIES or not isinstance(location, str) or len(location) > 300:
        raise ValueError("Invalid place category or address")
    updated = deepcopy(plan)
    activities = updated.setdefault("planning", {}).setdefault("activities", [])
    if any(item["name"].casefold() == name.casefold() for item in activities):
        raise ValueError("Activity already exists")
    number = 1
    while any(item["id"] == f"manual-{number}" for item in activities):
        number += 1
    activities.append({"id": f"manual-{number}", "name": name,
                       "location": location.strip() or name, "source": "manual", "manual_added": True,
                       "location_hint": location.strip(), "category": category, "category_manual": True,
                       "place_status": "unresolved",
                       "note": "", "choice": "must", "estimated_cost": 0,
                       "duration_minutes": 90, "ticket_required": None, "visit_tips": ""})
    return _sync_hotels(updated)


def select_activities(plan: dict[str, Any], choices: dict[str, str], categories: dict | None = None,
                      selected_hotel: Any = _UNSET, details: dict | None = None) -> dict[str, Any]:
    updated = deepcopy(plan)
    activities = updated.get("planning", {}).get("activities") or []
    known = {item["id"] for item in activities}
    categories = categories if categories is not None else {}
    if not isinstance(choices, dict) or not isinstance(categories, dict):
        raise ValueError("Invalid place choices or categories")
    if set(choices) - known or any(value not in {"must", "optional", "exclude"} for value in choices.values()):
        raise ValueError("Invalid activity choice")
    if set(categories) - known or any(value not in PLACE_CATEGORIES for value in categories.values()):
        raise ValueError("Invalid place category")
    details = details if details is not None else {}
    if not isinstance(details, dict) or set(details) - known:
        raise ValueError("Invalid place details")
    for item in activities:
        if item["id"] in choices:
            item["choice"] = choices[item["id"]]
        if item["id"] in categories and categories[item["id"]] != item.get("category", "attraction"):
            item.update(category=categories[item["id"]], category_manual=True)
        if item["id"] in details:
            fields = details[item["id"]]
            if not isinstance(fields, dict) or set(fields) - {"duration_minutes", "visit_tips"}:
                raise ValueError("Invalid place details")
            if "duration_minutes" in fields and item.get("choice") != "exclude" and item.get("category") != "hotel":
                duration = fields["duration_minutes"]
                if isinstance(duration, bool) or not isinstance(duration, int) or not 30 <= duration <= 600:
                    raise ValueError("停留时长必须为30–600分钟整数")
                item["duration_minutes"] = duration
            if "visit_tips" in fields:
                tips = fields["visit_tips"]
                if not isinstance(tips, str) or len(tips) > 500:
                    raise ValueError("备注最长500字")
                item["visit_tips"] = tips.strip()
    _sync_hotels(updated)
    if selected_hotel is not _UNSET:
        hotels = updated["planning"]["hotel_candidates"]
        if selected_hotel is not None and not any(x["id"] == selected_hotel for x in hotels):
            raise ValueError("请选择酒店分组中的住宿地点")
        if any(x["id"] == selected_hotel and x.get("choice") == "exclude" for x in activities):
            raise ValueError("住宿酒店不能同时选择不去")
        updated["planning"]["selected_hotel"] = selected_hotel
    if updated.get("itinerary") and (choices or categories or details or selected_hotel is not _UNSET):
        updated["planning"]["needs_replan"] = True
    return updated


def _selected(plan: dict[str, Any]) -> tuple[dict, dict, dict | None]:
    planning = plan.get("planning") or {}
    transports = planning.get("transport_candidates") or []
    hotels = planning.get("hotel_candidates") or []
    chosen = planning.get("selected_transport") or {}
    outbound = next((x for x in transports if x["id"] == chosen.get("outbound")), None)
    returning = next((x for x in transports if x["id"] == chosen.get("return")), None)
    hotel = next((x for x in hotels if x["id"] == planning.get("selected_hotel")), None)
    if outbound is None or returning is None or (plan["start_date"] != plan["end_date"] and hotel is None and not planning.get("guide_auto")):
        raise ValueError("Select outbound, return and hotel candidates before planning")
    return outbound, returning, hotel


def _minute(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _clock(value: int) -> str:
    value = max(0, value) % 1440
    return f"{value // 60:02d}:{value % 60:02d}"


def _set_start(item: dict, minutes: int) -> None:
    item.update(time_of_day=_clock(minutes), day_offset=max(0, minutes) // 1440)


def _start_minutes(item: dict) -> int:
    return _minute(item["time_of_day"]) + item.get("day_offset", 0) * 1440


def _validate_clock(value: str) -> None:
    if not isinstance(value, str) or len(value) != 5:
        raise ValueError("时间必须为 HH:MM")
    try:
        datetime.strptime(value, "%H:%M")
    except ValueError as exc:
        raise ValueError("时间必须为 HH:MM") from exc


def _item(day: int, key: str, time: int, title: str, location: str,
          category: str, duration: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "item_id": f"guided-{day}-{key}", "day": day, "time_of_day": _clock(time),
        "title": title, "location": location, "category": category,
        "duration_minutes": duration, "estimated_cost": 0, "day_offset": max(0, time) // 1440, **extra,
    }


def draft_itinerary(plan: dict[str, Any], proposal: dict | None = None) -> dict[str, Any]:
    updated = deepcopy(plan)
    planning = updated.get("planning") or {}
    outbound, returning, hotel = _selected(updated)
    days = (date.fromisoformat(updated["end_date"]) - date.fromisoformat(updated["start_date"])).days + 1
    home = planning.get("home_location") or updated["departure_city"]
    lodging = hotel["location"] if hotel else "待选择住宿"
    lodging_fields = {"place": hotel.get("place"), "hotel_name": hotel["name"], "reference_id": hotel["id"]} if hotel else {"lodging_pending": True}
    pending = [deepcopy(x) for x in planning.get("unscheduled_items", []) if x.get("manual_pending")]
    pending_ids = {x["reference_id"] for x in pending}
    activities = sorted(
        [x for x in planning.get("activities", []) if x.get("choice") != "exclude" and x.get("category", "attraction") != "hotel" and x["id"] not in pending_ids],
        key=lambda x: x.get("choice") != "must",
    )
    pace_limit = {"relaxed": 2, "moderate": 3, "active": 4}.get(updated.get("pace"), 3)
    daily_target = min(pace_limit, ceil(len(activities) / days)) if activities else 0
    itinerary: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    next_activity = 0
    activity_by_id = {x["id"]: x for x in activities}
    for day in range(1, days + 1):
        first, last = day == 1, day == days
        arrivals = _minute(outbound["arrive_time"])
        departures = _minute(returning["depart_time"])
        window_start = max(9 * 60, arrivals + 90) if first else 9 * 60 + 30
        window_end = min(18 * 60, departures - 120) if last else 18 * 60
        if first:
            itinerary.append(_item(day, "start", _minute(outbound["depart_time"]) - 90,
                                   "从家出发", home, "home_anchor", anchor_role="start"))
            itinerary.append(_item(day, "outbound", _minute(outbound["depart_time"]),
                                   f"{TRANSPORT_LABELS[outbound['kind']]}前往{updated['destination']}",
                                   updated["departure_city"], "intercity_transport",
                                   arrivals - _minute(outbound["depart_time"]), reference_id=outbound["id"],
                                   arrival_location=outbound.get("arrival_location") or updated["destination"],
                                   station_confirmed=bool(outbound.get("departure_location") and outbound.get("arrival_location"))))
            itinerary[-1]["location"] = outbound.get("departure_location") or updated["departure_city"]
            if not last and hotel:
                itinerary.append(_item(day, "arrival", arrivals + 60, "抵达酒店", lodging,
                                       "hotel_arrival", **lodging_fields))
        else:
            itinerary.append(_item(day, "start", 8 * 60, "从酒店出发", lodging,
                                       "hotel_anchor", anchor_role="start", **lodging_fields))

        slot = window_start
        planned_today = 0
        while proposal is None and next_activity < len(activities) and planned_today < daily_target and slot + int(activities[next_activity].get("duration_minutes") or 90) <= window_end and slot <= 17 * 60:
            activity = activities[next_activity]
            duration = int(activity.get("duration_minutes") or 90)
            itinerary.append(_item(day, activity["id"], slot, activity["name"],
                                   activity.get("location") or activity["name"], activity.get("category", "attraction"), duration,
                                   estimated_cost=activity.get("estimated_cost") or 0,
                                   reference_id=activity["id"],
                                   place=activity.get("place"),
                                   ticket_required=activity.get("ticket_required"),
                                   visit_tips=activity.get("visit_tips") or activity.get("note", "")))
            next_activity += 1
            planned_today += 1
            slot += duration + 30

        if proposal is not None:
            day_visits = (next((x["visits"] for x in proposal["days"] if x["day"] == day), [])
                          if "days" in proposal else sorted(
                              (x for x in proposal["stops"] if x["day"] == day), key=lambda x: x["time_of_day"]))
            for stop in day_visits:
                activity = activity_by_id[stop["activity_id"]]
                start = _minute(stop["time_of_day"]) if "time_of_day" in stop else slot
                itinerary.append(_item(day, activity["id"], start, activity["name"],
                                       activity.get("location") or activity["name"], activity.get("category", "attraction"),
                                        activity.get("duration_minutes") or 90, reference_id=activity["id"], place=activity.get("place"),
                                       ticket_required=activity.get("ticket_required"), estimated_cost=activity.get("estimated_cost", 0),
                                       visit_tips=activity.get("visit_tips") or activity.get("note", ""),
                                        ))
                itinerary[-1]["suggested_travel_mode"] = stop.get("travel_mode")
                slot = max(slot, start + int(activity.get("duration_minutes") or 90) + 30)

        if last:
            itinerary.append(_item(day, "return", departures,
                                   f"{TRANSPORT_LABELS[returning['kind']]}返回{updated['departure_city']}",
                                   updated["destination"], "intercity_transport",
                                   _minute(returning["arrive_time"]) - departures,
                                   reference_id=returning["id"], arrival_location=returning.get("arrival_location") or updated["departure_city"],
                                   station_confirmed=bool(returning.get("departure_location") and returning.get("arrival_location"))))
            itinerary[-1]["location"] = returning.get("departure_location") or updated["destination"]
            itinerary.append(_item(day, "end", _minute(returning["arrive_time"]) + 60,
                                   "回到家", home, "home_anchor", anchor_role="end"))
        else:
            itinerary.append(_item(day, "end", max(slot, 20 * 60), "返回酒店", lodging,
                                   "hotel_anchor", anchor_role="end", **lodging_fields))

    scheduled_ids = ({stop["activity_id"] for day in proposal["days"] for stop in day["visits"]}
                     if "days" in proposal else {stop["activity_id"] for stop in proposal["stops"]}) if proposal is not None else None
    unscheduled = [x for x in activities[next_activity:]] if scheduled_ids is None else [x for x in activities if x["id"] not in scheduled_ids]
    for day in range(1, days + 1):
        items = [x for x in itinerary if x["day"] == day]
        for origin, destination in zip(items, items[1:]):
            routes.append({
                "origin_item_id": origin["item_id"], "destination_item_id": destination["item_id"],
                "origin": origin.get("arrival_location", origin["location"]), "destination": destination["location"],
                "duration_minutes": 30, "source": "provisional_estimate",
                "travel_mode": destination.get("suggested_travel_mode") or ("DRIVE" if outbound["kind"] == "drive" else "TRANSIT"),
            })
    updated["itinerary"] = itinerary
    updated["routes"] = routes
    planning.pop("unplaced_activities", None)
    planning["unscheduled_items"] = pending
    planning["generation_issues"] = [{"type": "generation_omission", "severity": "high",
        "message": f"{x['name']}未进入生成结果，请重新生成或调整要求。", "target": x["id"]}
        for x in unscheduled]
    if proposal is not None:
        planning["generation"] = deepcopy(proposal)
        planning["generation"].pop("unscheduled", None)
        planning["generation"]["contract_version"] = 3
    planning["stage"] = "review"
    planning["needs_replan"] = False
    planning.pop("day_departures", None)
    return updated


def edit_review_place(plan: dict, payload: dict) -> dict:
    from service.travel.map_planning import lookup_review_place

    updated = deepcopy(plan)
    planning = updated["planning"]
    items = updated["itinerary"]
    index = next((i for i, item in enumerate(items) if item["item_id"] == payload.get("item_id")), None)
    if planning["stage"] != "review" or index is None:
        raise ValueError("请在审核阶段选择有效的行程卡片")
    anchor = items[index]
    category = payload.get("category")
    replacing = payload.get("replace", False)
    if type(replacing) is not bool or category not in ("attraction", "food", "hotel"):
        raise ValueError("请选择有效的行程类型")
    if replacing and (category != "hotel" or anchor["category"] not in {"hotel", "hotel_anchor", "hotel_arrival"}):
        raise ValueError("请选择住宿卡片")
    if not replacing and (anchor.get("anchor_role") == "end" or anchor["item_id"].endswith("-return")):
        raise ValueError("请在当天结束卡片之前添加行程")
    activity_id = payload.get("activity_id")
    if not isinstance(activity_id, str) or not re.fullmatch(r"manual-[0-9a-f]{32}", activity_id):
        raise ValueError("行程标识无效")
    if any(x["id"] == activity_id for x in planning["activities"] + planning["hotel_candidates"]):
        raise ValueError("行程标识已存在")
    tips = payload.get("visit_tips", "")
    if not isinstance(tips, str) or len(tips) > 500:
        raise ValueError("注意事项最多 500 字")
    duration = payload.get("duration_minutes") if category == "attraction" else 60 if category == "food" else 0
    if type(duration) is not int or (category == "attraction" and not 30 <= duration <= 600):
        raise ValueError("景点停留时间应为 30–600 分钟")
    place = lookup_review_place(payload.get("poi_id"))
    location = place["address"] or place["name"]
    details = {"place": place, "location": location, "visit_tips": tips.strip()}
    activity = {"id": activity_id, "name": place["name"], "category": category,
                "duration_minutes": duration, "estimated_cost": 0, "source": "高德 POI",
                "choice": "optional", "place_status": "manual_confirmed", **details}
    planning["activities"].append(activity)
    if category == "hotel":
        planning["hotel_candidates"].append({"id": activity_id, "name": place["name"],
            "location": location, "place": place, "nights": 0, "price_source": "unknown", "cost_unknown": True})
    if replacing:
        targets = [anchor]
        if anchor.get("lodging_pending"):
            is_end = anchor["anchor_role"] == "end"
            paired_day = anchor["day"] + (1 if is_end else -1)
            targets.extend(x for x in items if x["day"] == paired_day and x.get("lodging_pending")
                           and x["anchor_role"] == ("start" if is_end else "end"))
        for item in targets:
            item.update(reference_id=activity_id, hotel_name=place["name"], **details)
            if item["category"] == "hotel":
                item.update(title=place["name"], hotel_name=None)
            item.pop("lodging_pending", None)
        anchor["lodging_selected_here"] = True
    else:
        items.insert(index + 1, _item(anchor["day"], activity_id, _start_minutes(anchor),
                     place["name"], location, category, duration, reference_id=activity_id,
                     place=place, visit_tips=tips.strip()))
        targets = []
    changed_ids = {item["item_id"] for item in targets}
    updated["routes"] = [route for route in updated["routes"]
                         if not changed_ids.intersection((route["origin_item_id"], route["destination_item_id"]))]
    return _reflow_review(updated)


def complete_review_lodging(plan: dict, activity_id: str) -> dict:
    updated = deepcopy(plan)
    items = updated["itinerary"]
    lodging_categories = {"hotel", "hotel_anchor", "hotel_arrival"}
    source = next((item for item in items if item["category"] in lodging_categories
                   and item.get("reference_id") == activity_id and not item.get("lodging_pending")), None)
    days = (date.fromisoformat(updated["end_date"]) - date.fromisoformat(updated["start_date"])).days + 1
    if not isinstance(activity_id, str) or not activity_id or updated["planning"]["stage"] != "review" or not source or days < 2:
        raise ValueError("请选择当前多日行程中的有效住宿")
    hotel_name = source.get("hotel_name") or source["title"]
    poi_id = (source.get("place") or {}).get("place_id")
    details = {"place": source.get("place"), "location": source["location"],
               "reference_id": activity_id, "hotel_name": hotel_name}

    def same_hotel(item):
        other_poi = (item.get("place") or {}).get("place_id")
        return poi_id == other_poi if poi_id and other_poi else item["location"] == source["location"]

    changed = set()
    itinerary = []
    for day in range(1, days + 1):
        stops = [item for item in items if item["day"] == day]
        slots = [("arrival", "hotel_arrival", None, "抵达酒店")] if day == 1 else [
            ("start", "hotel_anchor", "start", "从酒店出发")]
        if day < days:
            slots.append(("end", "hotel_anchor", "end", "返回酒店"))
        else:
            slots.append(("checkout", "hotel", "checkout", "返回酒店"))
        # Reuse a single hotel stop as the arrival or departure before placing a return.
        for key, category, role, title in slots:
            if key == "checkout":
                position = next((i for i, item in enumerate(stops) if item["category"] == "intercity_transport"
                                 or item["category"] == "home_anchor"), len(stops))
            else:
                position = (len(stops) if key == "end" else 0 if key == "start" else next(
                    (i for i, item in enumerate(stops) if item["category"] in MOVABLE_CATEGORIES
                     or item["category"] == "hotel_anchor" and item.get("anchor_role") == "end"), len(stops)))
            target = next((item for item in stops if item["category"] == category and item.get("anchor_role") == role), None)
            edge = position if key == "start" else position - 1
            if 0 <= edge < len(stops) and stops[edge] is target:
                edge += 1 if key == "start" else -1
            standalone = stops[edge] if 0 <= edge < len(stops) else None
            if not standalone or standalone.get("anchor_role") or standalone["category"] != "hotel" or not same_hotel(standalone):
                standalone = None
            if not standalone and key in {"start", "arrival"}:
                last_visit = next((item for item in reversed(stops) if item.get("anchor_role") != "end"), None) if day < days else None
                standalone = next((item for item in stops if item is not last_visit and item["category"] == "hotel"
                                   and not item.get("anchor_role") and same_hotel(item)), None)
            before = stops[position] if position < len(stops) else None
            if standalone:
                if target:
                    stops.remove(target)
                target = standalone
            elif not target:
                time = (8 * 60 if role == "start" else _start_minutes(stops[position - 1]) if position else 20 * 60)
                target = _item(day, f"lodging-{key}", time, title, source["location"], category)
            if before is not target:
                if target in stops:
                    stops.remove(target)
                position = 0 if key == "start" else stops.index(before) if before in stops else len(stops)
                stops.insert(position, target)
            previous = target.copy()
            if not same_hotel(target) or target.get("lodging_pending") or "visit_tips" not in target:
                target["visit_tips"] = source.get("visit_tips", "")
            if target.get("reference_id") != activity_id:
                target.pop("lodging_selected_here", None)
            target.update(**details, category=category, title=title)
            if role:
                target["anchor_role"] = role
            target.pop("lodging_pending", None)
            if target != previous:
                changed.add(target["item_id"])
        itinerary.extend(stops)
    updated["itinerary"] = itinerary
    updated["routes"] = [route for route in updated["routes"]
                         if not changed.intersection((route["origin_item_id"], route["destination_item_id"]))]
    return _reflow_review(updated)


def _reflow_review(plan: dict[str, Any]) -> dict[str, Any]:
    """Keep transport and home/hotel anchors fixed while rebuilding local legs."""
    outbound, _, _ = _selected(plan)
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    routes, itinerary = [], []
    existing = {(x["origin_item_id"], x["destination_item_id"]): x for x in plan.get("routes", [])}
    explicit_departures = "day_departures" in plan["planning"]
    for day in range(1, days + 1):
        day_items = [x for x in plan["itinerary"] if x["day"] == day]
        attractions = [x for x in plan["itinerary"] if x["day"] == day and x["category"] in MOVABLE_CATEGORIES]
        slot = max(9 * 60, _minute(outbound["arrive_time"]) + 90) if day == 1 else 9 * 60 + 30
        for item in attractions:
            _set_start(item, _minute(item["requested_time"]) if item.get("requested_time") else slot)
            item["item_id"] = f"guided-{day}-{item['reference_id']}"
            slot += item["duration_minutes"] + 30
        if day < days:
            end = next((x for x in plan["itinerary"] if x["day"] == day and x.get("anchor_role") == "end"), None)
            if end:
                _set_start(end, max(slot, 20 * 60))
        anchors = [x for x in plan["itinerary"] if x["day"] == day and x["category"] not in MOVABLE_CATEGORIES]
        ordered = day_items if explicit_departures else sorted(anchors + attractions, key=_start_minutes)
        itinerary.extend(ordered)
        for origin, destination in zip(ordered, ordered[1:]):
            old = existing.get((origin["item_id"], destination["item_id"]), {})
            start = origin.get("arrival_location", origin["location"])
            end = destination["location"]
            if old.get("origin") == start and old.get("destination") == end:
                routes.append(old)
            else:
                routes.append({"origin_item_id": origin["item_id"], "destination_item_id": destination["item_id"],
                               "origin": start, "destination": end,
                               "duration_minutes": 30, "source": "provisional_estimate",
                               "travel_mode": old.get("travel_mode", "TRANSIT")})
    plan["itinerary"] = itinerary
    plan["routes"] = routes
    return plan


def update_day_departure(plan: dict, day: int, time: str) -> dict:
    if plan["planning"]["stage"] != "review" or type(day) is not int:
        raise ValueError("请在审核阶段选择有效的行程日期")
    _validate_clock(time)
    first = next((x for x in plan["itinerary"] if x["day"] == day), None)
    if first is None or not (first.get("anchor_role") == "start" or first["category"] in MOVABLE_CATEGORIES):
        raise ValueError("当天出发时刻由固定交通决定或没有可调整的地点")
    planning = plan["planning"]
    departures = {**planning.get("day_departures", {}), str(day): time}
    if "requested_time" in first:
        itinerary = [{key: value for key, value in item.items() if key != "requested_time"} if item is first else item
                     for item in plan["itinerary"]]
        plan = {**plan, "itinerary": itinerary}
    return {**plan, "planning": {**planning, "day_departures": departures}}


def schedule_review(plan: dict) -> dict:
    """Derive the review timetable from ordered stops, departures and selected legs."""
    departures = plan["planning"]["day_departures"]
    routes = {(r["origin_item_id"], r["destination_item_id"]): r for r in plan["routes"]}
    days = {}
    for item in plan["itinerary"]:
        days.setdefault(item["day"], []).append(item)
    for day, items in days.items():
        first = items[0]
        if first.get("anchor_role") == "start" or first["category"] in MOVABLE_CATEGORIES:
            if str(day) not in departures:
                departures[str(day)] = first.get("time_of_day") or "09:30"
            _set_start(first, _minute(first.get("requested_time") or departures[str(day)]))
        for previous, item in zip(items, items[1:]):
            route = routes.get((previous["item_id"], item["item_id"]), {})
            earliest = _start_minutes(previous) + previous.get("duration_minutes", 0) + route.get("duration_minutes", 30)
            if item["category"] in MOVABLE_CATEGORIES:
                _set_start(item, _minute(item["requested_time"]) if item.get("requested_time") else earliest)
            elif item["category"] != "intercity_transport":
                _set_start(item, _minute(item["requested_time"]) if item.get("requested_time") else earliest)
    return plan


def itinerary_for_storage(plan: dict) -> list[dict]:
    planning = plan.get("planning") or {}
    if "day_departures" not in planning or planning.get("optimized_timetable"):
        return plan["itinerary"]
    return [
        item if item["category"] == "intercity_transport" else
        {key: value for key, value in item.items() if key not in {"time_of_day", "day_offset"}}
        for item in plan["itinerary"]
    ]


def move_activity(plan: dict[str, Any], item_id: str, target_day: int | None,
                  before_item_id: str | None = None) -> dict[str, Any]:
    if (plan.get("planning") or {}).get("stage") != "review":
        raise ValueError("Activities can only be moved during itinerary review")
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    if target_day is not None and (isinstance(target_day, bool) or not isinstance(target_day, int) or not 1 <= target_day <= days):
        raise ValueError("Invalid target day")
    updated = deepcopy(plan)
    pending = updated["planning"].setdefault("unscheduled_items", [])
    moving = next((x for x in updated["itinerary"] + pending if x["item_id"] == item_id and x["category"] in MOVABLE_CATEGORIES), None)
    if moving is None:
        raise ValueError("Only visit, meal or other stops can be moved")
    if moving and updated["planning"].get("guide_auto") and not moving.get("place") and target_day is not None:
        raise ValueError("请先确认该地点的位置，再重新生成行程")
    by_day = {day: [x for x in updated["itinerary"] if x["day"] == day and x["category"] in MOVABLE_CATEGORIES]
              for day in range(1, days + 1)}
    if moving in pending:
        pending.remove(moving)
    else:
        by_day[moving["day"]].remove(moving)
    target = pending if target_day is None else by_day[target_day]
    if before_item_id:
        position = next((index for index, x in enumerate(target) if x["item_id"] == before_item_id), None)
        if position is None:
            raise ValueError("Invalid drop target")
        target.insert(position, moving)
    else:
        target.append(moving)
    moving["day"] = target_day
    moving["day_offset"] = 0
    moving["manual_pending"] = target_day is None
    moving.pop("requested_time", None)
    updated["planning"].pop("unplaced_activities", None)
    if "day_departures" in updated["planning"]:
        updated["itinerary"] = [x for x in updated["itinerary"] if x is not moving]
        if target_day is not None:
            position = next((i for i, x in enumerate(updated["itinerary"])
                             if x["day"] > target_day or x["day"] == target_day and
                             (x["item_id"] == before_item_id or not before_item_id and
                              (x.get("anchor_role") == "end" or x["item_id"].endswith("-return")))),
                            len(updated["itinerary"]))
            updated["itinerary"].insert(position, moving)
    else:
        anchors = [x for x in updated["itinerary"] if x["category"] not in MOVABLE_CATEGORIES]
        updated["itinerary"] = anchors + [x for day in range(1, days + 1) for x in by_day[day]]
    return _reflow_review(updated)


def update_activity_details(plan: dict[str, Any], activity_id: str, details: dict[str, Any]) -> dict[str, Any]:
    if (plan.get("planning") or {}).get("stage") != "review":
        raise ValueError("Activity details can only be edited during itinerary review")
    updated = deepcopy(plan)
    activity = next((x for x in updated["planning"]["activities"] if x["id"] == activity_id), None)
    item = next((x for x in updated["itinerary"] if x.get("reference_id") == activity_id and x["category"] in MOVABLE_CATEGORIES), None)
    if activity is None or item is None:
        raise ValueError("Activity is not in the itinerary")
    duration = details.get("duration_minutes")
    ticket = details.get("ticket_required")
    tips = details.get("visit_tips")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 30 <= duration <= 600:
        raise ValueError("Visit duration must be 30-600 minutes")
    if ticket is not None and not isinstance(ticket, bool):
        raise ValueError("ticket_required must be true, false or null")
    if not isinstance(tips, str) or len(tips) > 500:
        raise ValueError("Visit tips must be at most 500 characters")
    cost = _positive_money(details.get("estimated_cost"), "estimated_cost")
    for record in (activity, item):
        record.update(duration_minutes=duration, ticket_required=ticket,
                      visit_tips=tips.strip(), estimated_cost=cost)
    optimized = updated["planning"].get("optimized_timetable")
    if "time_of_day" in details:
        value = details["time_of_day"]
        if not isinstance(value, str) or len(value) != 5:
            raise ValueError("Start time must use HH:MM")
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("Start time must use HH:MM") from exc
        item["requested_time"] = value
        if optimized:
            _set_start(item, _minute(value))
    elif "day_departures" in updated["planning"] and not optimized:
        item.pop("requested_time", None)
    return updated if optimized else _reflow_review(updated)


def update_route_mode(plan: dict[str, Any], origin_id: str, destination_id: str, mode: str) -> dict:
    if plan.get("planning", {}).get("stage") != "review":
        raise ValueError("Routes can only be changed during review")
    if mode not in {"WALK", "TRANSIT", "DRIVE"}:
        raise ValueError("Choose WALK, TRANSIT or DRIVE")
    updated = deepcopy(plan)
    route = next((r for r in updated["routes"] if r["origin_item_id"] == origin_id and r["destination_item_id"] == destination_id), None)
    if route is None:
        raise ValueError("Local route not found")
    route["travel_mode"] = mode
    route.update(source="provisional_estimate", duration_minutes=30, legs=[], mode_locked=True)
    route.pop("distance_meters", None)
    route.pop("fare", None)
    option = (route.get("route_options") or {}).get(mode) or {}
    if option.get("success"):
        for field in ("duration_minutes", "distance_meters", "fare", "legs"):
            if field in option:
                route[field] = option[field]
    if updated["planning"].get("optimized_timetable"):
        return updated
    return _reflow_review(updated)


def update_transport_stops(plan: dict, item_id: str, departure: str, arrival: str) -> dict:
    if plan.get("planning", {}).get("stage") != "review":
        raise ValueError("Transport stops can only be changed during review")
    if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 200 for value in (departure, arrival)):
        raise ValueError("请填写出发站和到达站的完整名称或地址")
    updated = deepcopy(plan)
    item = next((x for x in updated["itinerary"] if x["item_id"] == item_id and x["category"] == "intercity_transport"), None)
    if item is None:
        raise ValueError("Transport item not found")
    item.update(location=departure.strip(), arrival_location=arrival.strip(), station_confirmed=True)
    candidate = next(x for x in updated["planning"]["transport_candidates"] if x["id"] == item["reference_id"])
    candidate.update(departure_location=departure.strip(), arrival_location=arrival.strip())
    return _reflow_review(updated)


def apply_route_times(plan: dict) -> dict:
    """Reflow visits using calculated travel times; keep selected train/flight times."""
    routes = {(r["origin_item_id"], r["destination_item_id"]): r for r in plan["routes"]}
    for day in sorted({x["day"] for x in plan["itinerary"]}):
        ordered = [x for x in plan["itinerary"] if x["day"] == day]
        ordered.sort(key=_start_minutes)
        for previous, item in zip(ordered, ordered[1:]):
            route = routes.get((previous["item_id"], item["item_id"]), {})
            earliest = _start_minutes(previous) + previous.get("duration_minutes", 0) + route.get("duration_minutes", 30)
            if item["category"] in MOVABLE_CATEGORIES:
                _set_start(item, _minute(item["requested_time"]) if item.get("requested_time") else max(_start_minutes(item), earliest))
            elif item.get("anchor_role") == "end" and item["category"] == "hotel_anchor":
                _set_start(item, max(20 * 60, earliest))
    return plan


def schedule_itinerary(plan: dict) -> dict:
    """按路线耗时顺排活动；固定车次不移动，未知路线留给审查处理。"""
    routes = {(r["origin_item_id"], r["destination_item_id"]): r for r in plan["routes"]}
    for day in sorted({item["day"] for item in plan["itinerary"]}):
        items = [item for item in plan["itinerary"] if item["day"] == day]
        first_visit = True
        for previous, item in zip(items, items[1:]):
            if item["category"] not in MOVABLE_CATEGORIES and not (item["category"] == "hotel_anchor" and item.get("anchor_role") == "end"):
                continue
            route = routes.get((previous["item_id"], item["item_id"]))
            duration = route.get("duration_minutes", 0) if route else 0
            earliest = _start_minutes(previous) + previous.get("duration_minutes", 0) + duration
            if item["category"] in MOVABLE_CATEGORIES:
                start = _minute(item["requested_time"]) if item.get("requested_time") else max(earliest, _start_minutes(item)) if first_visit else earliest
                _set_start(item, start)
                first_visit = False
            else:
                _set_start(item, max(20 * 60, earliest))
    return plan


def _review_hotel_nights(plan: dict) -> dict:
    selected = plan.get("planning", {}).get("selected_hotel")
    lodging_nights = {}
    has_overnight = False
    for stop in plan.get("itinerary", []):
        overnight = stop.get("category") == "hotel_anchor" and stop.get("anchor_role") == "end"
        has_overnight |= overnight
        if stop.get("category") == "hotel" or overnight:
            hotel_id = stop.get("reference_id") or selected
            if hotel_id and not stop.get("lodging_pending"):
                lodging_nights[hotel_id] = lodging_nights.get(hotel_id, 0) + int(overnight)
    if not has_overnight and selected:
        hotel = next((x for x in plan["planning"].get("hotel_candidates", []) if x["id"] == selected), {})
        lodging_nights.setdefault(selected, hotel.get("nights", 0))
    return lodging_nights


def _billable_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    planning = plan.get("planning") or {}
    visits = {}
    for stop in plan.get("itinerary", []):
        if stop.get("category") in MOVABLE_CATEGORIES and stop.get("reference_id"):
            visits[stop["reference_id"]] = visits.get(stop["reference_id"], 0) + 1
    lodging_nights = _review_hotel_nights(plan)
    selected = set((planning.get("selected_transport") or {}).values()) | lodging_nights.keys()
    hotel_ids = {x["id"] for x in planning.get("hotel_candidates", [])}
    items = [
        {"reference_id": item["id"], "label": item.get("name") or f"{'去程' if item.get('direction') == 'outbound' else '返程'}{TRANSPORT_LABELS.get(item.get('kind'), '交通')}：{item.get('origin', '')} → {item.get('destination', '')}",
         "category": "hotel" if item["id"] in hotel_ids else "transport",
         "estimate": round(item["estimated_total"] * lodging_nights.get(item["id"], item["nights"]) / item["nights"], 2)
                     if item.get("estimated_total") and item.get("nights") and item["id"] in lodging_nights else item.get("estimated_total") or 0,
         "source": "实际记录" if item.get("price_source") == "actual" else "API" if item.get("price_source") not in {None, "simulation", "unknown"} else "未知",
         "basis": f"{lodging_nights.get(item['id'], item.get('nights', 0))} 晚" if item["id"] in hotel_ids else "往返交通选项",
         "cost_unknown": item.get("price_source") in {"simulation", "unknown"} or item.get("cost_unknown", False)
                         or item.get("estimated_total") is None}
        for item in (planning.get("transport_candidates") or []) + (planning.get("hotel_candidates") or [])
        if item["id"] in selected
    ]
    for item in items:
        if item["cost_unknown"]:
            item["estimate"] = 0
    items += [
        {"reference_id": item["id"], "label": item["name"], "category": "ticket" if item.get("category", "attraction") == "attraction" else item["category"],
         "source": "用户填写" if item.get("estimated_cost") else "高德地图" if (item.get("place") or {}).get("cost_per_person") and item.get("category") == "food" else "未知",
         "basis": f"{plan['travelers']} 人 × {visits.get(item['id'], 0)} 次" if item.get("category") == "food" else f"{visits.get(item['id'], 0)} 次",
         "estimate": visits.get(item["id"], 0) * _positive_money(item.get("estimated_cost") or
                                    ((item.get("place") or {}).get("cost_per_person") or 0) * plan["travelers"]
                                    if item.get("category") == "food" else item.get("estimated_cost") or 0, "estimated_cost"),
         "cost_unknown": not item.get("estimated_cost") and not (item.get("category") == "food" and (item.get("place") or {}).get("cost_per_person"))}
        for item in planning.get("activities") or []
        if item["id"] in visits and item.get("choice") != "exclude" and item.get("category", "attraction") != "hotel"
        and (item.get("category", "attraction") == "attraction" and item.get("ticket_required") is not False
             or item.get("category") == "food" or item.get("category") == "other" and item.get("estimated_cost", 0) > 0)
    ]
    self_drive = next((x for x in planning.get("transport_candidates", [])
                       if x.get("id") == (planning.get("selected_transport") or {}).get("outbound") and x.get("kind") == "drive"), None)
    for index, route in enumerate(plan.get("routes") or []):
        if route.get("source") != "高德地图" or route.get("travel_mode") not in {"TRANSIT", "DRIVE"}:
            continue
        fare = route.get("fare")
        known = isinstance(fare, (int, float)) and not isinstance(fare, bool) and isfinite(fare) and fare >= 0
        if route["travel_mode"] == "DRIVE" and self_drive:
            known = False
        count = plan["travelers"] if route["travel_mode"] == "TRANSIT" else ceil(plan["travelers"] / 4)
        stops = {x["item_id"]: x for x in plan.get("itinerary", []) if x.get("item_id")}
        leg = " → ".join(stops[key].get("title", "地点") for key in (route.get("origin_item_id"), route.get("destination_item_id")) if key in stops)
        items.append({"reference_id": f"route-{index}", "label": ("公交：" if route["travel_mode"] == "TRANSIT" else "打车：") + leg,
                      "category": "local_transit", "estimate": round(fare * count, 2) if known else 0,
                      "source": "高德地图" if known else "未知", "basis": f"{count} {'人' if route['travel_mode'] == 'TRANSIT' else '辆车'}",
                      "cost_unknown": not known, "bookable": False})
    return items


def budget_summary(plan: dict[str, Any]) -> dict[str, Any]:
    planning = plan.get("planning") or {}
    purchases = {item["reference_id"]: item for item in planning.get("purchases") or []}
    items = []
    for item in _billable_items(plan):
        purchase = purchases.pop(item["reference_id"], None)
        items.append({**item, "actual_total": purchase["actual_total"] if purchase else None,
                      "counted_total": purchase["actual_total"] if purchase else item["estimate"],
                      "purchased": purchase is not None})
    # Paid bookings remain real spending even if a later replan no longer selects them.
    items += [{"reference_id": item["reference_id"], "label": item.get("label", "已购项目"),
               "category": item.get("category", "other"), "estimate": 0,
               "actual_total": item["actual_total"], "counted_total": item["actual_total"],
               "purchased": True, "retained_expense": True, "bookable": False,
               "selection_changed": not item.get("change_acknowledged", False)} for item in purchases.values()]
    for index, item in enumerate(plan.get("budget_items") or [], 1):
        amount = _positive_money(item.get("amount"), "budget_item.amount")
        items.append({"reference_id": f"chat-budget-{item.get('budget_item_id') or index}",
                      "label": item.get("description") or item.get("category") or "其他费用",
                      "category": "additional_budget", "estimate": amount,
                      "actual_total": None, "counted_total": amount,
                      "purchased": False, "bookable": False})
    total = round(sum(item["counted_total"] for item in items), 2)
    return {
        "items": items, "estimated_total": total,
        "paid_total": round(sum(item["actual_total"] for item in items if item["purchased"]), 2),
        "remaining": round(float(plan["budget"]) - total, 2),
        "has_possible_duplicates": bool(plan.get("budget_items")),
        "has_unknown_ticket_costs": any(x["category"] == "ticket" and x.get("cost_unknown") and not x["purchased"] for x in items),
        "has_unknown_hotel_costs": any(x["category"] == "hotel" and x.get("cost_unknown") and not x["purchased"] for x in items),
        "has_unknown_transport_costs": any(x["category"] in {"transport", "local_transit"} and x.get("cost_unknown") and not x["purchased"] for x in items),
        "has_unknown_food_costs": any(x.get("category") == "food" and x.get("choice") != "exclude"
                                      and any(stop.get("reference_id") == x["id"] for stop in plan.get("itinerary", []))
                                      and not x.get("estimated_cost") and not (x.get("place") or {}).get("cost_per_person")
                                      for x in planning.get("activities") or []),
    }


def record_purchase(plan: dict[str, Any], purchase: dict[str, Any]) -> dict[str, Any]:
    updated = deepcopy(plan)
    reference_id = purchase.get("reference_id")
    item = next((x for x in _billable_items(updated) if x["reference_id"] == reference_id), None)
    if item is None:
        raise ValueError("Purchase must reference a selected transport, hotel or activity")
    paid = _positive_money(purchase.get("actual_total"), "actual_total")
    records = [x for x in updated["planning"].get("purchases", []) if x["reference_id"] != reference_id]
    records.append({"reference_id": reference_id, "label": item["label"],
                    "category": item["category"], "actual_total": paid,
                    "purchased_at": datetime.now().isoformat(timespec="seconds")})
    updated["planning"]["purchases"] = records
    return updated


def itinerary_sheet(plan: dict[str, Any]) -> dict[str, Any]:
    from service.travel.itinerary_audit import audit_plan

    outbound, returning, _ = _selected(plan)
    nights = _review_hotel_nights(plan)
    hotels = [{**hotel, "nights": nights[hotel["id"]]} for hotel in plan["planning"]["hotel_candidates"] if hotel["id"] in nights]
    days = sorted({item["day"] for item in plan.get("itinerary") or []})
    return {
        "title": plan["title"], "destination": plan["destination"],
        "dates": [plan["start_date"], plan["end_date"]], "travelers": plan["travelers"],
        "budget_limit": plan["budget"], "currency": plan["currency"],
        "transport": {"outbound": outbound, "return": returning}, "hotel": hotels[0] if hotels else None, "hotels": hotels,
        "days": [{"day": day, "items": sorted([x for x in plan["itinerary"] if x["day"] == day], key=lambda x: x["time_of_day"])} for day in days],
        "routes": plan.get("routes") or [], "budget": budget_summary(plan),
        "purchases": plan.get("planning", {}).get("purchases") or [],
        "audit": audit_plan(plan),
    }
