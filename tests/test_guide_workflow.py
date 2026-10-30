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
    def test_invalid_window_is_explained_to_model_on_retry(self):
        bad = json.dumps({"places": [{"name": "西湖", "time_window": "上午"}], "constraints": []}, ensure_ascii=False)
        good = json.dumps({"places": [{"name": "西湖", "time_window": "09:00-12:00"}], "constraints": []}, ensure_ascii=False)
        with patch.object(guide, "_extract_with_model", side_effect=[bad, good]) as extract:
            result = guide.analyze_guide("上午去西湖")
        self.assertEqual(result["places"][0]["time_window"], "09:00-12:00")
        self.assertIn("上午", extract.call_args_list[1].args[1])

    def test_three_invalid_extractions_require_manual_input(self):
        bad = json.dumps({"places": [{"name": "西湖", "time_window": "上午"}], "constraints": []}, ensure_ascii=False)
        with patch.object(guide, "_extract_with_model", return_value=bad) as extract:
            with self.assertRaises(guide.GuideNeedsInput):
                guide.analyze_guide("上午去西湖")
        self.assertEqual(extract.call_count, 3)

    def test_guide_schema_only_requests_cross_place_constraints(self):
        from jsonschema import ValidationError, validate
        place = {"name": "西湖", "category": "attraction", "address": "", "day": 1,
                 "time_window": "", "duration_minutes": None, "ticket_required": None,
                 "note": ""}
        data = {"places": [place], "constraints": [{"type": "order", "places": ["西湖"],
                                                       "source": "先去西湖"}]}
        validate(data, guide.GUIDE_SCHEMA)
        with self.assertRaises(ValidationError):
            validate({**data, "places": [{**place, "visit_tips": "重复提示"}]}, guide.GUIDE_SCHEMA)
        with self.assertRaises(ValidationError):
            validate({**data, "constraints": [{"type": "day", "places": ["西湖"],
                                                "source": "第一天去", "day": 1}]}, guide.GUIDE_SCHEMA)

    def test_extraction_uses_schema_and_rejects_extra_identity(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        chat = AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"places": [], "constraints": []}), tool_calls=[])))
        with patch("service.utils.model_client.create_model_client", return_value=SimpleNamespace(chat=chat)), patch("service.utils.model_client.get_model", return_value="test"):
            self.assertEqual(guide.analyze_guide("攻略")["places"], [])
        request = chat.call_args.kwargs
        self.assertEqual(request["tools"], [])
        self.assertEqual(request["format"], guide.GUIDE_SCHEMA)
        for data in ({"places": [], "constraints": [], "id": "injected"},
                     {"places": [{"name": "西湖", "place_id": "injected"}]},
                     {"places": [], "constraints": [{"type": "order", "places": [], "activity_id": "injected"}]}):
            with self.subTest(data=data), patch.object(guide, "_extract_with_model", return_value=json.dumps(data)):
                with self.assertRaises(ValueError):
                    guide.analyze_guide("西湖")

    def test_provider_schema_response_is_validated_locally(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        chat = AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(
            content='{"places":[{"name":"西湖","id":"fake"}],"constraints":[]}', tool_calls=[])))
        with patch("service.utils.model_client.create_model_client", return_value=SimpleNamespace(chat=chat)), patch("service.utils.model_client.get_model", return_value="test"):
            with self.assertRaises(ValueError):
                guide.analyze_guide("西湖")

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

    def test_constraint_place_names_allow_incidental_spacing(self):
        data = {"places": [{"name": "西湖", "category": "attraction"}],
                "constraints": [{"type": "day", "places": [" 西湖 "], "day": 1,
                                 "source": "第一天去西湖", "hard": True}]}
        with patch.object(guide, "_extract_with_model", return_value=json.dumps(data, ensure_ascii=False)):
            result = guide.analyze_guide("第一天去西湖")
        self.assertEqual(result["constraints"][0]["activity_ids"], [result["places"][0]["id"]])

    def test_unknown_constraint_place_identifies_reference(self):
        data = {"places": [{"name": "西湖", "category": "attraction"}],
                "constraints": [{"type": "day", "places": ["雷峰塔"], "day": 1,
                                 "source": "第一天去雷峰塔", "hard": True}]}
        with patch.object(guide, "_extract_with_model", return_value=json.dumps(data, ensure_ascii=False)):
            with self.assertRaisesRegex(ValueError, "雷峰塔"):
                guide.analyze_guide("第一天去雷峰塔")

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
