import unittest
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from service.storage.travel_db import row_to_plan
from service.travel import planning
from service.travel.guide import analyze_guide, recognize_guide
from service.travel import guide
from service.travel.itinerary_audit import audit_plan
from service.mcp import server as mcp_server
from agent.prompts import build_system_prompt


FORM = {
    "destination": "杭州",
    "departure_city": "上海",
    "home_location": "上海的家",
    "start_date": "2026-11-06",
    "end_date": "2026-11-08",
    "travelers": 2,
    "budget": 5000,
    "currency": "CNY",
    "interests": ["博物馆", "美食"],
    "pace": "moderate",
}


class GuidedPlanningTests(unittest.TestCase):
    def test_form_rejects_invalid_dates_and_budget(self):
        with self.assertRaises(ValueError):
            planning.create_draft({**FORM, "end_date": "2026-11-05"}, "trip-1")
        with self.assertRaises(ValueError):
            planning.create_draft({**FORM, "budget": -1}, "trip-1")
        with self.assertRaises(ValueError):
            planning.create_draft({**FORM, "currency": "USD"}, "trip-1")

    def test_form_can_derive_return_date_from_trip_days(self):
        form = {key: value for key, value in FORM.items() if key != "end_date"}
        plan = planning.create_draft({**form, "trip_days": 3}, "trip-1")
        self.assertEqual(plan["end_date"], "2026-11-08")
        with self.assertRaises(ValueError):
            planning.create_draft({**form, "trip_days": 0}, "trip-2")

    def test_simulated_candidates_are_stable_and_labeled(self):
        plan = planning.create_draft(FORM, "trip-1")
        first = planning.build_candidates(plan)
        second = planning.build_candidates(plan)
        self.assertEqual(first["planning"]["transport_candidates"], second["planning"]["transport_candidates"])
        self.assertEqual(first["planning"]["hotel_candidates"], second["planning"]["hotel_candidates"])
        all_candidates = first["planning"]["transport_candidates"] + first["planning"]["hotel_candidates"]
        self.assertTrue(all(item["price_source"] == "simulation" for item in all_candidates if item.get("kind") != "drive"))
        self.assertTrue(all(item["price_source"] == "unknown" and item["cost_unknown"] for item in all_candidates if item.get("kind") == "drive"))

    def test_candidate_identity_changes_when_trip_dates_change(self):
        first = planning.build_candidates(planning.create_draft(FORM, "trip-1"))
        changed = planning.build_candidates(planning.create_draft({**FORM, "start_date": "2026-11-07", "end_date": "2026-11-09"}, "trip-2"))
        self.assertNotEqual(first["planning"]["transport_candidates"][0]["id"], changed["planning"]["transport_candidates"][0]["id"])
        self.assertNotEqual(first["planning"]["hotel_candidates"][0]["id"], changed["planning"]["hotel_candidates"][0]["id"])

    def test_hotel_identity_and_price_follow_place_when_results_reorder(self):
        plan = planning.create_draft(FORM, "trip-1")
        places = [{"place_id": "a", "name": "酒店 A", "address": "西湖"},
                  {"place_id": "b", "name": "酒店 B", "address": "市区"}]
        first = planning.build_candidates(plan, places)["planning"]["hotel_candidates"]
        changed = planning.build_candidates(plan, list(reversed(places)))["planning"]["hotel_candidates"]
        a1 = next(x for x in first if x["place_id"] == "a")
        a2 = next(x for x in changed if x["place_id"] == "a")
        self.assertEqual((a1["id"], a1["estimated_total"]), (a2["id"], a2["estimated_total"]))

    def test_day_trip_needs_no_hotel_nights(self):
        plan = planning.build_candidates(planning.create_draft({**FORM, "end_date": "2026-11-06"}, "day-trip"))
        self.assertEqual(plan["planning"]["hotel_candidates"], [])
        candidates = plan["planning"]["transport_candidates"]
        selected = planning.select_candidates(plan, {
            "outbound": next(x["id"] for x in candidates if x["direction"] == "outbound"),
            "return": next(x["id"] for x in candidates if x["direction"] == "return"),
            "hotel": None,
        })
        self.assertIsNone(selected["planning"]["selected_hotel"])

    def test_selection_accepts_only_returned_candidates(self):
        plan = planning.build_candidates(planning.create_draft(FORM, "trip-1"))
        candidates = plan["planning"]["transport_candidates"]
        hotel = plan["planning"]["hotel_candidates"][0]
        selected = planning.select_candidates(plan, {
            "outbound": next(item["id"] for item in candidates if item["direction"] == "outbound"),
            "return": next(item["id"] for item in candidates if item["direction"] == "return"),
            "hotel": hotel["id"],
        })
        self.assertEqual(selected["planning"]["selected_hotel"], hotel["id"])
        with self.assertRaises(ValueError):
            planning.select_candidates(plan, {"outbound": "fake", "return": "fake", "hotel": "fake"})

    def test_legacy_plan_loads_without_planning_column(self):
        row = {
            "plan_id": "old", "destination": "杭州", "start_date": "2026-11-06",
            "end_date": "2026-11-08", "departure_city": "上海", "travelers": 2,
            "budget": 5000, "currency": "CNY", "travel_style": "balanced",
            "interests": "[]", "constraints_json": "[]", "pace": "moderate", "notes": "",
            "created_at": "2026-10-28 00:00:00", "updated_at": "2026-10-28 00:00:00",
            "itinerary": "[]", "routes": "[]", "recommendations": "[]", "budget_items": "[]",
        }
        self.assertEqual(row_to_plan(row)["planning"], {})

    def test_guide_analysis_preserves_source_text_for_review(self):
        with patch("service.travel.guide._extract_with_model", return_value=json.dumps({"places": [
            {"name": "西湖", "note": "建议上午游玩"}, {"name": "雷峰塔", "note": "门票 40 元"}
        ]}, ensure_ascii=False)):
            result = analyze_guide("西湖：建议上午游玩\n雷峰塔：门票 40 元")
        self.assertEqual([item["name"] for item in result["places"]], ["西湖", "雷峰塔"])
        self.assertIn("门票 40 元", result["places"][1]["note"])

    def test_guide_analysis_splits_route_and_keeps_price_as_note(self):
        with patch("service.travel.guide._extract_with_model", return_value=json.dumps({"places": [
            {"name": "西湖", "note": ""}, {"name": "灵隐寺", "note": ""},
            {"name": "河坊街", "note": "门票：45元；建议提前预约"}
        ]}, ensure_ascii=False)):
            result = analyze_guide("D1 西湖 → 灵隐寺 → 河坊街\n门票：45元\n建议提前预约")
        self.assertEqual([item["name"] for item in result["places"]], ["西湖", "灵隐寺", "河坊街"])
        self.assertIn("门票：45元", result["places"][-1]["note"])
        self.assertNotIn("建议提前预约", [item["name"] for item in result["places"]])

    def test_guide_place_identity_is_stable_for_purchases(self):
        with patch("service.travel.guide._extract_with_model", side_effect=[
            '{"places":[{"name":"西湖","note":"上午游玩"}]}',
            '{"places":[{"name":"灵隐寺","note":"先去"},{"name":"西湖","note":"上午游玩"}]}',
        ]):
            west = analyze_guide("西湖：上午游玩")["places"][0]
            shifted = analyze_guide("灵隐寺：先去\n西湖：上午游玩")["places"]
        self.assertEqual(west["id"], shifted[1]["id"])
        self.assertNotEqual(west["id"], shifted[0]["id"])

    def test_guide_model_output_is_validated_and_never_replaced_by_rules(self):
        with patch("service.travel.guide._extract_with_model", return_value='not json'):
            with self.assertRaisesRegex(ValueError, "model"):
                analyze_guide("西湖：上午游玩")
        with patch("service.travel.guide._extract_with_model", side_effect=RuntimeError("offline")):
            with self.assertRaisesRegex(ValueError, "model"):
                analyze_guide("西湖：上午游玩")

    def test_guide_extractor_uses_configured_model_and_skill(self):
        client = SimpleNamespace(chat=AsyncMock(return_value=SimpleNamespace(
            message=SimpleNamespace(content='{"places":[{"name":"西湖","note":"上午"}]}'))))
        with patch("service.utils.model_client.create_model_client", return_value=client), patch(
            "service.utils.model_client.get_model", return_value="guide-test-model"
        ):
            result = analyze_guide("西湖：上午")
        self.assertEqual(result["places"][0]["name"], "西湖")
        call = client.chat.await_args.kwargs
        self.assertEqual(call["model"], "guide-test-model")
        self.assertEqual(call["format"], "json")
        self.assertIn("不要补充", call["messages"][0]["content"])
        self.assertIn("西湖：上午", call["messages"][1]["content"])
        with patch("service.travel.guide._extract_with_model") as extract:
            self.assertEqual(guide.analyze_guide("")["places"], [])
        extract.assert_not_called()

    def test_guide_model_deduplicates_and_rejects_bad_records(self):
        output = {"places": [{"name": "西湖", "note": "上午"},
                             {"name": "西湖", "note": "重复"},
                             {"name": "", "note": "空"}]}
        with patch("service.travel.guide._extract_with_model", return_value=json.dumps(output, ensure_ascii=False)):
            result = analyze_guide("西湖")
        self.assertEqual(len(result["places"]), 1)

    def test_guide_model_visit_metadata_reaches_activity(self):
        output = {"places": [{"name": "灵隐寺", "note": "游玩约两小时，需门票",
                              "duration_minutes": 120, "ticket_required": True,
                              "visit_tips": "提前预约"}]}
        plan = planning.create_draft(FORM, "trip-1")
        with patch("service.travel.guide._extract_with_model", return_value=json.dumps(output, ensure_ascii=False)):
            plan = planning.build_activities(plan, "灵隐寺：游玩约两小时，需门票，提前预约")
        activity = plan["planning"]["activities"][0]
        self.assertEqual((activity["duration_minutes"], activity["ticket_required"], activity["visit_tips"]),
                         (120, True, "提前预约"))

    def test_ocr_rejects_non_image_bytes(self):
        with self.assertRaises(ValueError):
            recognize_guide(b"not an image")

    def test_activity_selection_keeps_only_valid_choices(self):
        plan = planning.build_candidates(planning.create_draft(FORM, "trip-1"))
        with patch("service.travel.guide._extract_with_model", return_value='{"places":[{"name":"西湖","note":"建议上午游玩"}]}'):
            activity_plan = planning.build_activities(plan, "西湖：建议上午游玩", [
                {"place_id": "p1", "name": "西湖", "address": "杭州西湖区"},
                {"place_id": "p2", "name": "浙江省博物馆", "address": "杭州"},
            ])
        self.assertEqual(len(activity_plan["planning"]["activities"]), 2)
        guide_id = activity_plan["planning"]["activities"][0]["id"]
        chosen = planning.select_activities(activity_plan, {guide_id: "must", "place-p2": "optional"})
        self.assertEqual(chosen["planning"]["activities"][0]["choice"], "must")
        with self.assertRaises(ValueError):
            planning.select_activities(activity_plan, {"fake": "must"})

    def prepared_plan(self):
        plan = planning.build_candidates(planning.create_draft(FORM, "trip-1"))
        candidates = plan["planning"]["transport_candidates"]
        plan = planning.select_candidates(plan, {
            "outbound": next(item["id"] for item in candidates if item["direction"] == "outbound"),
            "return": next(item["id"] for item in candidates if item["direction"] == "return"),
            "hotel": plan["planning"]["hotel_candidates"][0]["id"],
        })
        with patch("service.travel.guide._extract_with_model", return_value='{"places":[{"name":"西湖","note":"上午游玩"},{"name":"雷峰塔","note":"看日落"}]}'):
            plan = planning.build_activities(plan, "西湖：上午游玩\n雷峰塔：看日落", [])
        ids = [item["id"] for item in plan["planning"]["activities"]]
        return planning.select_activities(plan, {ids[0]: "must", ids[1]: "optional"})

    def test_route_has_explicit_home_and_hotel_day_anchors(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        by_day = {day: sorted([i for i in plan["itinerary"] if i["day"] == day], key=lambda i: i["time_of_day"]) for day in range(1, 4)}
        self.assertEqual(by_day[1][0]["category"], "home_anchor")
        self.assertEqual(by_day[1][-1]["category"], "hotel_anchor")
        self.assertEqual(by_day[2][0]["category"], "hotel_anchor")
        self.assertEqual(by_day[2][-1]["category"], "hotel_anchor")
        self.assertEqual(by_day[3][0]["category"], "hotel_anchor")
        self.assertEqual(by_day[3][-1]["category"], "home_anchor")
        self.assertFalse({i["type"] for i in audit_plan(plan)["issues"]} & {"missing_start_anchor", "missing_end_anchor"})

    def test_route_spreads_selected_activities_across_days(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        activity_days = [item["day"] for item in plan["itinerary"] if item["category"] == "attraction"]
        self.assertEqual(activity_days, [1, 2])

    def test_review_can_move_activity_between_days_and_rebuild_routes(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        moved = next(x for x in plan["itinerary"] if x["category"] == "attraction" and x["day"] == 1)
        changed = planning.move_activity(plan, moved["item_id"], 2)
        moved_after = next(x for x in changed["itinerary"] if x.get("reference_id") == moved["reference_id"])
        self.assertEqual(moved_after["day"], 2)
        self.assertTrue(any(x["destination_item_id"] == moved_after["item_id"] for x in changed["routes"]))
        self.assertNotIn("missing_route", {x["type"] for x in audit_plan(changed)["issues"]})
        with self.assertRaises(ValueError):
            planning.move_activity(plan, "guided-1-start", 2)

    def test_review_reorder_reassigns_start_times(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        first, second = [x for x in plan["itinerary"] if x["category"] == "attraction"]
        together = planning.move_activity(plan, second["item_id"], 1)
        second_today = next(x for x in together["itinerary"] if x.get("reference_id") == second["reference_id"])
        first_today = next(x for x in together["itinerary"] if x.get("reference_id") == first["reference_id"])
        reordered = planning.move_activity(together, second_today["item_id"], 1, first_today["item_id"])
        by_id = {x.get("reference_id"): x for x in reordered["itinerary"] if x["category"] == "attraction"}
        self.assertLess(by_id[second["reference_id"]]["time_of_day"], by_id[first["reference_id"]]["time_of_day"])
        self.assertNotEqual(by_id[first["reference_id"]]["time_of_day"], first["time_of_day"])

    def test_review_activity_details_flow_into_itinerary_and_budget(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        activity = next(x for x in plan["planning"]["activities"] if x["choice"] == "must")
        changed = planning.update_activity_details(plan, activity["id"], {
            "duration_minutes": 150, "ticket_required": True,
            "estimated_cost": 80, "visit_tips": "提前预约",
        })
        item = next(x for x in changed["itinerary"] if x.get("reference_id") == activity["id"])
        self.assertEqual(item["duration_minutes"], 150)
        self.assertEqual(item["visit_tips"], "提前预约")
        self.assertEqual(planning.budget_summary(changed)["estimated_total"], planning.budget_summary(plan)["estimated_total"] + 80)
        free = planning.update_activity_details(changed, activity["id"], {
            "duration_minutes": 120, "ticket_required": False,
            "estimated_cost": 0, "visit_tips": "无需预约",
        })
        self.assertFalse(any(x["reference_id"] == activity["id"] for x in planning.budget_summary(free)["items"]))

    def test_audit_flags_missing_anchor_and_guided_budget_overrun(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        plan["itinerary"] = [i for i in plan["itinerary"] if not (i["day"] == 2 and i["category"] == "hotel_anchor" and i["anchor_role"] == "start")]
        plan["budget"] = 100
        kinds = {item["type"] for item in audit_plan(plan)["issues"]}
        self.assertIn("missing_start_anchor", kinds)
        self.assertIn("budget_overrun", kinds)

    def test_audit_flags_insufficient_transfer_buffer(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        first_day = sorted((i for i in plan["itinerary"] if i["day"] == 1), key=lambda i: i["time_of_day"])
        first_day[1]["time_of_day"] = first_day[0]["time_of_day"]
        self.assertIn("transport_buffer", {item["type"] for item in audit_plan(plan)["issues"]})

    def test_audit_flags_wrong_anchor_location_and_stale_route(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        plan["itinerary"][0]["location"] = "错误的出发地"
        kinds = {item["type"] for item in audit_plan(plan)["issues"]}
        self.assertIn("missing_start_anchor", kinds)
        self.assertIn("stale_route", kinds)

    def test_actual_purchase_replaces_estimate_and_survives_replan(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        outbound = plan["planning"]["selected_transport"]["outbound"]
        estimate = planning.budget_summary(plan)["estimated_total"]
        expected = next(item["estimated_total"] for item in plan["planning"]["transport_candidates"] if item["id"] == outbound)
        bought = planning.record_purchase(plan, {"reference_id": outbound, "actual_total": expected + 100})
        self.assertEqual(planning.budget_summary(bought)["estimated_total"], estimate + 100)
        replanned = planning.draft_itinerary(bought)
        self.assertEqual(replanned["planning"]["purchases"], bought["planning"]["purchases"])
        self.assertEqual(planning.itinerary_sheet(replanned)["purchases"][0]["actual_total"], expected + 100)

    def test_budget_includes_daily_food_and_local_transit_allowances(self):
        plan = self.prepared_plan()
        summary = planning.budget_summary(plan)
        categories = {item["category"]: item for item in summary["items"]}
        self.assertEqual(categories["food"]["estimate"], 3 * 2 * 100)
        self.assertEqual(categories["local_transit"]["estimate"], 3 * 2 * 30)
        with self.assertRaises(ValueError):
            planning.record_purchase(plan, {"reference_id": "allowance-food", "actual_total": 200})

    def test_guided_budget_includes_chat_budget_items(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        before = planning.budget_summary(plan)["estimated_total"]
        plan["budget_items"].append({"budget_item_id": "budget-1", "category": "other",
                                     "description": "额外费用", "amount": 125})
        summary = planning.budget_summary(plan)
        self.assertEqual(summary["estimated_total"], before + 125)
        self.assertTrue(summary["has_possible_duplicates"])
        self.assertIn("budget_item_overlap", {item["type"] for item in audit_plan(plan)["issues"]})

    def test_purchase_rejects_unselected_reference(self):
        with self.assertRaises(ValueError):
            planning.record_purchase(self.prepared_plan(), {"reference_id": "fake", "actual_total": 100})

    def test_chat_prompt_uses_guided_anchors_and_purchase_state(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        outbound = plan["planning"]["selected_transport"]["outbound"]
        plan = planning.record_purchase(plan, {"reference_id": outbound, "actual_total": 600})
        prompt = build_system_prompt(plan)
        self.assertIn("上海的家", prompt)
        self.assertIn("600", prompt)
        self.assertIn("guided planning", prompt.lower())

    def test_chat_date_edit_invalidates_candidates_without_losing_purchase(self):
        plan = planning.draft_itinerary(self.prepared_plan())
        outbound = plan["planning"]["selected_transport"]["outbound"]
        plan = planning.record_purchase(plan, {"reference_id": outbound, "actual_total": 600})
        with patch.object(mcp_server, "get_plan", return_value=(plan, None)), patch.object(mcp_server, "save_plan") as save:
            result = mcp_server.update_travel_profile(plan["plan_id"], {"start_date": "2026-11-07"})
        self.assertTrue(result["success"])
        saved = save.call_args.args[0]
        self.assertEqual(saved["planning"]["stage"], "requirements")
        self.assertEqual(saved["planning"]["purchases"][0]["actual_total"], 600)


if __name__ == "__main__":
    unittest.main()
