"""Located POIs and model proposals for offline planning regression tests."""

from copy import deepcopy
from datetime import date
from math import ceil

from service.travel.planning import draft_itinerary


def located_plan(plan):
    updated = deepcopy(plan)
    state = updated["planning"]
    for index, item in enumerate(state.get("hotel_candidates", []) + state.get("activities", [])):
        if item.get("source") == "simulated_suggestion":
            continue
        if not item.get("place"):
            item["place"] = {
                "place_id": "test-poi-" + item["id"], "name": item["name"],
                "address": item.get("location") or "杭州市西湖区",
                "longitude": 120.1 + index * 0.001, "latitude": 30.2 + index * 0.001,
                "image_cache_key": "a" * 64,
            }
            item["place_status"] = "manual_confirmed"
    hotel_places = {x["id"]: x["place"] for x in state.get("hotel_candidates", [])}
    for item in state.get("activities", []):
        if item.get("category") == "hotel" and item["id"] in hotel_places:
            item["place"] = deepcopy(hotel_places[item["id"]])
    return updated


def mock_itinerary_proposal(plan, feedback=None):
    """Offline model boundary: distribute selected visits within fixed trip days."""
    state = plan["planning"]
    days = (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    activities = [x for x in state.get("activities", [])
                  if x.get("category", "attraction") != "hotel" and x.get("choice") != "exclude"]
    activities.sort(key=lambda x: x.get("choice") != "must")
    per_day = max(1, ceil(len(activities) / days))
    outbound = next(x for x in state["transport_candidates"] if x["id"] == state["selected_transport"]["outbound"])
    hour, minute = map(int, outbound["arrive_time"].split(":"))
    first_start = max(9 * 60, hour * 60 + minute + 90)
    stops = []
    for index, activity in enumerate(activities):
        day = index // per_day + 1
        slot = first_start if day == 1 else 9 * 60 + 30
        previous = activities[(day - 1) * per_day:index]
        slot += sum((x.get("duration_minutes") or 90) + 30 for x in previous)
        stops.append({"activity_id": activity["id"], "day": day,
                      "time_of_day": f"{slot // 60:02d}:{slot % 60:02d}",
                      "duration_minutes": activity.get("duration_minutes") or 90,
                      "reason": "离线模型测试安排"})
    return {"stops": stops, "unscheduled": [], "assumptions": [], "constraint_results": []}


def draft_with_proposal(plan):
    return draft_itinerary(plan, mock_itinerary_proposal(plan))
