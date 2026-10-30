"""Pure, form-led travel planning operations shared by the guided API and graph."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from hashlib import sha256
from math import ceil, isfinite
from typing import Any

from service.storage.travel_db import now
from service.travel.guide import analyze_guide
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
        raise ValueError("Guided simulated prices currently use CNY only")
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


def _simulated_price(*parts: str, base: int, spread: int) -> int:
    token = "|".join(parts).encode("utf-8")
    return base + int.from_bytes(sha256(token).digest()[:4], "big") % spread


def _candidate_id(prefix: str, *parts: str) -> str:
    return prefix + "-" + sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]


def build_candidates(plan: dict[str, Any], hotel_places: list[dict] | None = None) -> dict[str, Any]:
    updated = deepcopy(plan)
    planning = updated.setdefault("planning", {})
    people = updated["travelers"]
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
                price = _simulated_price(start, end, day, direction, mode, str(index), base=base, spread=spread)
                candidates.append({
                    "id": _candidate_id(f"{direction}-{mode}", start, end, day, str(index)), "kind": mode,
                    "direction": direction, "origin": start, "destination": end,
                    "date": day, "depart_time": depart, "arrive_time": arrive,
                    "price_per_person": price, "estimated_total": price * people,
                    "price_source": "simulation",
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
    nights = (date.fromisoformat(updated["end_date"]) - date.fromisoformat(updated["start_date"])).days
    places = (hotel_places or [])[:3]
    if not places and nights:
        places = [{"name": f"{destination}住宿方案 {index}", "address": f"{destination}市区", "place_id": None} for index in range(1, 4)]
    hotels = []
    for index, place in enumerate(places, 1):
        hotel_key = str(place.get("place_id") or place.get("name") or f"住宿方案 {index}")
        price = _simulated_price(destination, updated["start_date"], updated["end_date"], hotel_key, base=220, spread=380)
        hotels.append({
            "id": _candidate_id("hotel", destination, updated["start_date"], updated["end_date"], hotel_key),
            "name": place.get("name") or f"{destination}住宿方案 {index}",
            "location": place.get("address") or destination,
            "place_id": place.get("place_id"), "nights": nights,
            "place": place if place.get("latitude") is not None else None,
            "price_per_night": price, "estimated_total": price * nights,
            "price_source": "simulation",
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
                               "choice": "optional", "note": "住宿候选，价格为模拟估算", "duration_minutes": 90,
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
        price = _simulated_price(plan["destination"], plan["start_date"], plan["end_date"], activity["id"], base=220, spread=380)
        hotel = hotels.setdefault(activity["id"], {"id": activity["id"], "price_per_night": price,
                                                 "estimated_total": price * nights, "price_source": "simulation"})
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
    planning["guide_places"] = guide["places"]
    planning["guide_constraints"] = guide.get("constraints", [])
    previous = {item["name"].casefold(): item for item in planning.get("activities") or []}
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
    for activity in activities:
        old = next((x for x in planning.get("activities", []) if x["id"] == activity["id"] or x["name"].casefold() == activity["name"].casefold()), {})
        if not old:
            old = next((x for x in planning.get("activities", []) if (x.get("source_name") or "").casefold() == activity["name"].casefold()), {})
        if old:
            id_mapping[activity["id"]] = old["id"]
            activity["id"] = old["id"]
            activity.update({key: old[key] for key in ("choice", "estimated_cost", "duration_minutes", "ticket_required", "visit_tips") if key in old})
            if old.get("category_manual"):
                activity.update(category=old.get("category", "attraction"), category_manual=True)
        hint_changed = old.get("location_hint", "") != activity.get("location_hint", "")
        if old.get("place") and (not hint_changed or old.get("place_status") != "auto_confirmed"):
            activity.update({key: old[key] for key in ("name", "location", "source", "place", "place_status") if key in old})
            if hint_changed:
                activity["place_error"] = "原文地址已变化，已保留手动位置，请核对"
        activity.setdefault("category", "attraction")
        activity.setdefault("place_status", "unresolved")
    included = {x["id"] for x in activities}
    activities += [item for item in planning.get("activities") or []
                   if (item.get("source") == "manual" or item.get("place") or item.get("category") == "hotel" or item.get("choice") == "exclude") and item["id"] not in included and item["name"].casefold() not in names]
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
                       "location": location.strip() or name, "source": "manual",
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
    value = max(0, min(value, 1439))
    return f"{value // 60:02d}:{value % 60:02d}"


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
        "duration_minutes": duration, "estimated_cost": 0, **extra,
    }


def draft_itinerary(plan: dict[str, Any], proposal: dict | None = None) -> dict[str, Any]:
    updated = deepcopy(plan)
    planning = updated.get("planning") or {}
    outbound, returning, hotel = _selected(updated)
    days = (date.fromisoformat(updated["end_date"]) - date.fromisoformat(updated["start_date"])).days + 1
    home = planning.get("home_location") or updated["departure_city"]
    lodging = hotel["location"] if hotel else None
    activities = sorted(
        [x for x in planning.get("activities", []) if x.get("choice") != "exclude" and x.get("category", "attraction") != "hotel"],
        key=lambda x: x.get("choice") != "must",
    )
    pace_limit = {"relaxed": 2, "moderate": 3, "active": 4}.get(updated.get("pace"), 3)
    daily_target = min(pace_limit, ceil(len(activities) / days)) if activities else 0
    itinerary: list[dict[str, Any]] = []
    routes: list[dict[str, Any]] = []
    unplaced = []
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
                                       "hotel_arrival", place=hotel.get("place")))
        elif hotel:
            itinerary.append(_item(day, "start", 8 * 60, "从酒店出发", lodging,
                                   "hotel_anchor", anchor_role="start", place=hotel.get("place")))

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
                                        reason="按每日顺序安排"))
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
        elif hotel:
            itinerary.append(_item(day, "end", max(slot, 20 * 60), "返回酒店", lodging,
                                   "hotel_anchor", anchor_role="end", place=hotel.get("place")))

    unscheduled = [x for x in activities[next_activity:]] if proposal is None else [activity_by_id[x["activity_id"]] for x in proposal["unscheduled"]]
    unplaced = [x["id"] for x in unscheduled]
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
    planning["unplaced_activities"] = unplaced
    planning["unscheduled_items"] = [
        _item(0, x["id"], 0, x["name"], x.get("location") or x["name"], x.get("category", "attraction"),
              int(x.get("duration_minutes") or 90), reference_id=x["id"], place=x.get("place"),
              ticket_required=x.get("ticket_required"), estimated_cost=x.get("estimated_cost", 0),
              visit_tips=x.get("visit_tips", "")) for x in unscheduled
    ]
    if proposal is not None:
        planning["generation"] = deepcopy(proposal)
    planning["stage"] = "review"
    planning["needs_replan"] = False
    return updated


def _reflow_review(plan: dict[str, Any]) -> dict[str, Any]:
    """Keep transport and home/hotel anchors fixed while rebuilding local legs."""
    outbound, _, _ = _selected(plan)
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    routes = []
    existing = {(x["origin_item_id"], x["destination_item_id"]): x for x in plan.get("routes", [])}
    for day in range(1, days + 1):
        attractions = [x for x in plan["itinerary"] if x["day"] == day and x["category"] in MOVABLE_CATEGORIES]
        slot = max(9 * 60, _minute(outbound["arrive_time"]) + 90) if day == 1 else 9 * 60 + 30
        for item in attractions:
            item["time_of_day"] = item.get("requested_time") or _clock(slot)
            item["item_id"] = f"guided-{day}-{item['reference_id']}"
            slot += item["duration_minutes"] + 30
        if day < days:
            end = next((x for x in plan["itinerary"] if x["day"] == day and x.get("anchor_role") == "end"), None)
            if end:
                end["time_of_day"] = _clock(max(slot, 20 * 60))
        ordered = sorted((x for x in plan["itinerary"] if x["day"] == day), key=lambda x: x["time_of_day"])
        for origin, destination in zip(ordered, ordered[1:]):
            old = existing.get((origin["item_id"], destination["item_id"]), {})
            routes.append({"origin_item_id": origin["item_id"], "destination_item_id": destination["item_id"],
                           "origin": origin.get("arrival_location", origin["location"]), "destination": destination["location"],
                           "duration_minutes": 30, "source": "provisional_estimate",
                           "travel_mode": old.get("travel_mode", "TRANSIT")})
    plan["itinerary"].sort(key=lambda x: (x["day"], x["time_of_day"]))
    plan["routes"] = routes
    return plan


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
    moving.pop("requested_time", None)
    updated["planning"]["unplaced_activities"] = [x["reference_id"] for x in pending]
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
    if "time_of_day" in details:
        value = details["time_of_day"]
        if not isinstance(value, str) or len(value) != 5:
            raise ValueError("Start time must use HH:MM")
        try:
            datetime.strptime(value, "%H:%M")
        except ValueError as exc:
            raise ValueError("Start time must use HH:MM") from exc
        item["requested_time"] = value
    return _reflow_review(updated)


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
    route.update(source="provisional_estimate", duration_minutes=30, legs=[])
    route.pop("distance_meters", None)
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
        ordered.sort(key=lambda x: x["time_of_day"])
        for previous, item in zip(ordered, ordered[1:]):
            route = routes.get((previous["item_id"], item["item_id"]), {})
            earliest = _minute(previous["time_of_day"]) + previous.get("duration_minutes", 0) + route.get("duration_minutes", 30)
            if item["category"] in MOVABLE_CATEGORIES:
                item["time_of_day"] = item.get("requested_time") or _clock(max(_minute(item["time_of_day"]), earliest))
            elif item.get("anchor_role") == "end" and item["category"] == "hotel_anchor":
                item["time_of_day"] = _clock(max(20 * 60, earliest))
    return plan


def schedule_itinerary(plan: dict) -> dict:
    """按路线耗时顺排活动；固定车次不移动，未知路线留给审查处理。"""
    routes = {(r["origin_item_id"], r["destination_item_id"]): r for r in plan["routes"]}
    for day in sorted({item["day"] for item in plan["itinerary"]}):
        items = [item for item in plan["itinerary"] if item["day"] == day]
        for previous, item in zip(items, items[1:]):
            if item["category"] not in MOVABLE_CATEGORIES and not (item["category"] == "hotel_anchor" and item.get("anchor_role") == "end"):
                continue
            route = routes.get((previous["item_id"], item["item_id"]))
            duration = route.get("duration_minutes", 0) if route else 0
            earliest = _minute(previous["time_of_day"]) + previous.get("duration_minutes", 0) + duration
            item["time_of_day"] = _clock(max(_minute(item["time_of_day"]), earliest))
    return plan


def _billable_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    planning = plan.get("planning") or {}
    selected = set((planning.get("selected_transport") or {}).values())
    if planning.get("selected_hotel"):
        selected.add(planning["selected_hotel"])
    hotel_ids = {x["id"] for x in planning.get("hotel_candidates", [])}
    items = [
        {"reference_id": item["id"], "label": item.get("name") or item.get("kind", "交通"),
         "category": "hotel" if item["id"] in hotel_ids else "transport",
         "estimate": item["estimated_total"], "cost_unknown": item.get("cost_unknown", False)}
        for item in (planning.get("transport_candidates") or []) + (planning.get("hotel_candidates") or [])
        if item["id"] in selected
    ]
    items += [
        {"reference_id": item["id"], "label": item["name"], "category": "ticket" if item.get("category", "attraction") == "attraction" else item["category"],
         "estimate": _positive_money(item.get("estimated_cost") or 0, "estimated_cost")}
        for item in planning.get("activities") or []
        if item.get("choice") != "exclude" and item.get("category", "attraction") != "hotel"
        and (item.get("category", "attraction") == "attraction" and item.get("ticket_required") is not False
             or item.get("category") in {"food", "other"} and item.get("estimated_cost", 0) > 0)
    ]
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
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    for reference_id, label, category, amount in (
        ("allowance-food", "餐饮预算", "food", planning.get("daily_food_per_person", 100)),
        ("allowance-local-transit", "市内交通预算", "local_transit", planning.get("daily_local_transit_per_person", 30)),
    ):
        estimate = round(_positive_money(amount, category) * plan["travelers"] * days, 2)
        if category == "food":
            # A priced meal replaces half a day's allowance, up to two meals a day.
            meals = min(days * 2, sum(x["category"] == "food" and x["counted_total"] > 0 for x in items))
            estimate = round(max(0, estimate - meals * _positive_money(amount, category) * plan["travelers"] / 2), 2)
        items.append({"reference_id": reference_id, "label": label, "category": category,
                      "estimate": estimate, "actual_total": None,
                      "counted_total": estimate, "purchased": False, "bookable": False})
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
        "has_unknown_ticket_costs": any(x["category"] == "ticket" and x["estimate"] == 0 and not x["purchased"] for x in items),
        "has_unknown_transport_costs": any(x.get("cost_unknown") and not x["purchased"] for x in items),
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

    outbound, returning, hotel = _selected(plan)
    days = sorted({item["day"] for item in plan.get("itinerary") or []})
    return {
        "title": plan["title"], "destination": plan["destination"],
        "dates": [plan["start_date"], plan["end_date"]], "travelers": plan["travelers"],
        "budget_limit": plan["budget"], "currency": plan["currency"],
        "transport": {"outbound": outbound, "return": returning}, "hotel": hotel,
        "days": [{"day": day, "items": sorted([x for x in plan["itinerary"] if x["day"] == day], key=lambda x: x["time_of_day"])} for day in days],
        "routes": plan.get("routes") or [], "budget": budget_summary(plan),
        "purchases": plan.get("planning", {}).get("purchases") or [],
        "audit": audit_plan(plan),
    }
