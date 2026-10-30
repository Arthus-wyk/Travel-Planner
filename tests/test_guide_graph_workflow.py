"""End-to-end graph boundaries with fake models and saved POI locations."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent.planning_graph import run_planning_action
from service.travel import planning
from test_guide_workflow import prepared, POINT


def ready():
    plan = planning.add_manual_activity(prepared(), "知味观", category="food")
    plan["planning"]["activities"][-1].update(place=deepcopy(POINT), place_status="manual_confirmed")
    selected = plan["planning"]["selected_hotel"]
    next(x for x in plan["planning"]["hotel_candidates"] if x["id"] == selected)["place"] = deepcopy(POINT)
    return plan


def proposal(plan, feedback=None):
    return {"stops": [{"activity_id": x["id"], "day": 2, "time_of_day": "12:00", "duration_minutes": 60, "reason": "根据攻略安排午餐"}
                      for x in plan["planning"]["activities"] if x.get("category") != "hotel" and x.get("choice") != "exclude"],
            "unscheduled": [], "assumptions": [], "constraint_results": []}


class GuideGraphWorkflowTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("agent.planning_graph.AMAP_API_KEY", "")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_draft_reuses_parsed_locations_without_searching_again(self):
        plan = ready()
        with patch("agent.planning_graph.generate_itinerary_proposal", side_effect=proposal) as model, patch(
            "service.travel.guide._extract_with_model"
        ) as parse, patch("service.travel.map_planning.amap_search_places") as search:
            result = run_planning_action(plan, "draft", {"text": "", "choices": {}})
        parse.assert_not_called()
        search.assert_not_called()
        self.assertIn("itinerary_agent", result["trace"])
        self.assertEqual(model.call_args.args[0]["planning"]["activities"][-1]["place"], POINT)
        self.assertTrue(result["plan"]["planning"]["generation"]["input_hash"])
        self.assertEqual(next(x for x in result["plan"]["itinerary"] if x["category"] == "food")["place"], POINT)

    def test_draft_submits_latest_text_and_preserves_selected_poi(self):
        plan = ready()
        text = "第二天中午12点去知味观"
        parsed = {"places": [{"name": "知味观", "category": "food", "day": 2, "time_window": "12:00-12:00"}]}
        with patch("service.travel.guide._extract_with_model", return_value=json.dumps(parsed, ensure_ascii=False)), patch(
            "agent.planning_graph.generate_itinerary_proposal", side_effect=proposal
        ) as model, patch("service.travel.map_planning.amap_search_places") as search:
            result = run_planning_action(plan, "draft", {"text": text, "choices": {"manual-1": "must"}})
        self.assertEqual(model.call_args.args[0]["planning"]["guide_text"], text)
        search.assert_not_called()
        self.assertEqual(result["plan"]["planning"]["activities"][0]["id"], "manual-1")
        self.assertEqual(result["plan"]["planning"]["activities"][0]["place_status"], "manual_confirmed")

    def test_missing_required_location_fails_before_model_and_preserves_input(self):
        plan = ready()
        plan["planning"]["activities"][-1].pop("place")
        before = deepcopy(plan)
        with patch("agent.planning_graph.generate_itinerary_proposal") as model:
            with self.assertRaisesRegex(ValueError, "位置"):
                run_planning_action(plan, "draft", {})
        model.assert_not_called()
        self.assertEqual(plan, before)

    def test_model_format_retry_is_bounded_and_does_not_mutate_old_plan(self):
        plan = ready()
        before = deepcopy(plan)
        with patch("agent.planning_graph.generate_itinerary_proposal", side_effect=ValueError("invalid JSON")) as model:
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                run_planning_action(plan, "draft", {})
        self.assertEqual(model.call_count, 3)
        self.assertEqual(plan, before)

    def test_hard_guide_day_constraint_is_checked_by_program(self):
        plan = ready()
        plan["planning"]["activities"][-1]["day"] = 3
        with patch("agent.planning_graph.generate_itinerary_proposal", side_effect=proposal) as model:
            result = run_planning_action(plan, "draft", {})
        self.assertEqual(model.call_count, 3)
        self.assertIn("guide_constraint", {x["type"] for x in result["audit"]["issues"]})
        self.assertTrue(any(x["severity"] == "high" for x in result["audit"]["issues"] if x["type"] == "guide_constraint"))

    def test_transport_candidates_do_not_search_hotels_until_step_two(self):
        plan = planning.create_draft({"destination": "杭州", "departure_city": "上海", "start_date": "2026-11-06", "trip_days": 2, "travelers": 1, "budget": 5000}, "steps")
        with patch("agent.planning_graph._hotel_places", return_value=[]) as hotels, patch("agent.planning_graph._activity_places", return_value=[]):
            result = run_planning_action(plan, "candidates", {})
            hotels.assert_not_called()
            transports = result["plan"]["planning"]["transport_candidates"]
            selected = run_planning_action(result["plan"], "choose_candidates", {
                d: next(x["id"] for x in transports if x["direction"] == d) for d in ("outbound", "return")})
        self.assertEqual(hotels.call_count, 1)
        self.assertTrue(any(x.get("category") == "hotel" for x in selected["plan"]["planning"]["activities"]))


if __name__ == "__main__":
    unittest.main()
