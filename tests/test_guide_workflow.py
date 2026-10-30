"""Guide-driven planning contracts, with no model or map network requests."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from service.travel import planning, guide, map_planning


FORM = {"destination": "杭州", "departure_city": "上海", "start_date": "2026-11-06",
        "trip_days": 3, "travelers": 2, "budget": 9000}
POINT = {"place_id": "B1", "name": "西湖", "address": "杭州市西湖区", "longitude": 120.1,
         "latitude": 30.2, "coordinate_system": "GCJ-02", "image_cache_key": "a" * 64}


def prepared():
    plan = planning.build_candidates(planning.create_draft(FORM, "guide-flow"))
    candidates = plan["planning"]["transport_candidates"]
    return planning.select_candidates(plan, {
        "outbound": next(x["id"] for x in candidates if x["direction"] == "outbound"),
        "return": next(x["id"] for x in candidates if x["direction"] == "return"),
        "hotel": plan["planning"]["hotel_candidates"][0]["id"],
    })


class GuideWorkflowTests(unittest.TestCase):
    def test_transport_has_four_modes_and_hotel_is_optional_at_step_one(self):
        plan = planning.build_candidates(planning.create_draft(FORM, "transport"))
        options = plan["planning"]["transport_candidates"]
        self.assertEqual({x["kind"] for x in options}, {"train", "flight", "drive", "bus"})
        chosen = planning.select_candidates(plan, {
            "outbound": next(x["id"] for x in options if x["direction"] == "outbound"),
            "return": next(x["id"] for x in options if x["direction"] == "return"),
        })
        self.assertEqual(chosen["planning"]["stage"], "activities")
        self.assertIsNone(chosen["planning"]["selected_hotel"])

    def test_self_drive_requires_round_trip_and_calculates_actual_duration(self):
        plan = planning.build_candidates(planning.create_draft(FORM, "drive"))
        opts = plan["planning"]["transport_candidates"]
        ids = {d: next(x["id"] for x in opts if x["kind"] == "drive" and x["direction"] == d)
               for d in ("outbound", "return")}
        with patch("service.travel.planning.amap_compute_route", return_value={"success": True, "duration": "7200s", "distance_meters": 180000}):
            result = planning.select_candidates(plan, {**ids, "drive_departures": {"outbound": "08:00", "return": "17:00"}, "vehicles": 2})
            selected = next(x for x in result["planning"]["transport_candidates"] if x["id"] == ids["outbound"])
            self.assertEqual(selected["arrive_time"], "10:00")
            self.assertEqual(selected["vehicles"], 2)
            self.assertTrue(selected["cost_unknown"])
            with self.assertRaises(ValueError):
                planning.select_candidates(plan, {**ids, "vehicles": True})
            mixed = {**ids, "return": next(x["id"] for x in opts if x["kind"] == "flight" and x["direction"] == "return")}
            with self.assertRaises(ValueError):
                planning.select_candidates(plan, mixed)
        with patch("service.travel.planning.amap_compute_route", return_value={"success": True, "duration": "86400s"}):
            with self.assertRaises(ValueError):
                planning.select_candidates(plan, ids)

    def test_guide_classifies_places_and_keeps_structured_source_constraints(self):
        data = {"places": [{"name": "西湖", "category": "attraction", "day": 2},
                            {"name": "知味观", "category": "food", "time_window": "12:00-14:00"},
                            {"name": "湖边酒店", "category": "hotel"},
                            {"name": "集合点", "category": "other"}],
                "constraints": [{"type": "order", "places": ["西湖", "知味观"], "source": "先西湖再午饭"}]}
        with patch.object(guide, "_extract_with_model", return_value=json.dumps(data, ensure_ascii=False)):
            result = guide.analyze_guide("第二天先西湖再午饭，住湖边酒店，集合点集合")
        self.assertEqual([x["category"] for x in result["places"]], ["attraction", "food", "hotel", "other"])
        self.assertEqual(result["places"][0]["day"], 2)
        self.assertEqual(result["constraints"][0]["source"], "先西湖再午饭")

    def test_hotel_group_selects_lodging_without_becoming_a_ticket(self):
        plan = planning.add_manual_activity(prepared(), "攻略酒店", "杭州湖滨", category="hotel")
        hotel = next(x for x in plan["planning"]["activities"] if x["name"] == "攻略酒店")
        plan = planning.select_activities(plan, {}, selected_hotel=hotel["id"])
        selected = next(x for x in plan["planning"]["hotel_candidates"] if x["id"] == hotel["id"])
        self.assertEqual(selected["nights"], 2)
        billed = planning.budget_summary(plan)["items"]
        self.assertEqual(len([x for x in billed if x["reference_id"] == hotel["id"]]), 1)
        self.assertEqual(next(x for x in billed if x["reference_id"] == hotel["id"])["category"], "hotel")

    def test_locating_parsed_places_reuses_confirmed_coordinates_and_handles_ambiguity(self):
        plan = planning.add_manual_activity(prepared(), "西湖")
        item = plan["planning"]["activities"][-1]
        item.update(place=deepcopy(POINT), place_status="manual_confirmed")
        with patch("service.travel.map_planning.amap_search_places") as search:
            result = map_planning.locate_activities(plan)
        search.assert_not_called()
        self.assertEqual(result["planning"]["activities"][-1]["place"], POINT)
        item.pop("place")
        with patch("service.travel.map_planning.amap_search_places", return_value={"success": True, "places": [POINT, {**POINT, "place_id": "B2"}]}):
            result = map_planning.locate_activities(plan)
        self.assertEqual(result["planning"]["activities"][-1]["place_status"], "ambiguous")
        self.assertNotIn("place", result["planning"]["activities"][-1])

    def test_auto_location_keeps_coordinates_when_map_image_fails(self):
        plan = planning.add_manual_activity(prepared(), "西湖")
        with patch("service.travel.map_planning.amap_search_places", return_value={"success": True, "places": [POINT]}), patch(
            "service.travel.map_planning.static_map", side_effect=ValueError("map down")
        ):
            result = map_planning.locate_activities(plan)
        item = result["planning"]["activities"][-1]
        self.assertEqual(item["place"]["longitude"], 120.1)
        self.assertEqual(item["place_status"], "auto_confirmed")

    def test_model_proposal_uses_saved_locations_and_food_can_move(self):
        plan = planning.add_manual_activity(prepared(), "知味观", category="food")
        meal = plan["planning"]["activities"][-1]
        meal["place"] = deepcopy(POINT)
        proposal = {"stops": [{"activity_id": meal["id"], "day": 2, "time_of_day": "12:00", "duration_minutes": 60, "reason": "午餐"}],
                    "unscheduled": [], "assumptions": [], "constraint_results": []}
        drafted = planning.draft_itinerary(plan, proposal)
        item = next(x for x in drafted["itinerary"] if x.get("reference_id") == meal["id"])
        self.assertEqual((item["category"], item["place"], item["time_of_day"]), ("food", POINT, "12:00"))
        moved = planning.move_activity(drafted, item["item_id"], 3)
        self.assertEqual(next(x["day"] for x in moved["itinerary"] if x.get("reference_id") == meal["id"]), 3)

    def test_reparsing_preserves_manual_category_location_and_choice(self):
        plan = planning.add_manual_activity(prepared(), "西湖")
        existing = plan["planning"]["activities"][-1]
        existing.update(category="other", category_manual=True, choice="exclude", place=deepcopy(POINT), place_status="manual_confirmed")
        with patch.object(planning, "analyze_guide", return_value={"text": "西湖", "places": [{"id": existing["id"], "name": "西湖", "category": "attraction"}]}):
            changed = planning.build_activities(plan, "西湖")
        item = next(x for x in changed["planning"]["activities"] if x["id"] == existing["id"])
        self.assertEqual((item["category"], item["choice"], item["place"]), ("other", "exclude", POINT))


if __name__ == "__main__":
    unittest.main()
