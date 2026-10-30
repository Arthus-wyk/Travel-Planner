import unittest
from unittest.mock import patch

from agent.planning_graph import _activity_places, run_planning_action
from service.travel.planning import create_draft
from planning_fixtures import located_plan, mock_itinerary_proposal


FORM = {"destination": "杭州", "departure_city": "上海", "home_location": "上海的家",
        "start_date": "2026-11-06", "end_date": "2026-11-08", "travelers": 2,
        "budget": 5000, "interests": ["博物馆"]}


class PlanningGraphTests(unittest.TestCase):
    def setUp(self):
        key = patch("agent.planning_graph.AMAP_API_KEY", "")
        key.start()
        self.addCleanup(key.stop)
        model = patch("agent.planning_graph.generate_itinerary_proposal", side_effect=mock_itinerary_proposal)
        self.model = model.start()
        self.addCleanup(model.stop)
        hotels = patch("agent.planning_graph._hotel_places", return_value=[])
        hotels.start()
        self.addCleanup(hotels.stop)

    def test_activity_agent_has_labeled_fallback_suggestions(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph.AMAP_API_KEY", ""):
            places = _activity_places(plan)
        self.assertTrue(places)
        self.assertTrue(all(place["source"] == "simulated_suggestion" for place in places))

    def test_unresolved_theme_suggestion_blocks_confirmation(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph.AMAP_API_KEY", ""):
            plan = run_planning_action(plan, "candidates", {})["plan"]
            transports = plan["planning"]["transport_candidates"]
            plan = run_planning_action(plan, "choose_candidates", {
                "outbound": next(x["id"] for x in transports if x["direction"] == "outbound"),
                "return": next(x["id"] for x in transports if x["direction"] == "return"),
                "hotel": plan["planning"]["hotel_candidates"][0]["id"],
            })["plan"]
        suggestion = next(x for x in plan["planning"]["activities"] if x.get("source") == "simulated_suggestion")
        self.assertEqual(suggestion["choice"], "exclude")
        plan = run_planning_action(plan, "choose_activities", {
            "choices": {suggestion["id"]: "must"}
        })["plan"]
        with self.assertRaisesRegex(ValueError, suggestion["name"]):
            run_planning_action(located_plan(plan), "draft", {})
        self.model.assert_not_called()
        with self.assertRaises(ValueError):
            run_planning_action(plan, "confirm", {})

    def test_review_edit_actions_return_fresh_audit(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph.AMAP_API_KEY", ""):
            plan = run_planning_action(plan, "candidates", {})["plan"]
            transport = plan["planning"]["transport_candidates"]
            plan = run_planning_action(plan, "choose_candidates", {
                "outbound": next(x["id"] for x in transport if x["direction"] == "outbound"),
                "return": next(x["id"] for x in transport if x["direction"] == "return"),
                "hotel": plan["planning"]["hotel_candidates"][0]["id"],
            })["plan"]
        plan = run_planning_action(plan, "add_activity", {"name": "西湖"})["plan"]
        plan = run_planning_action(located_plan(plan), "draft", {})["plan"]
        item = next(x for x in plan["itinerary"] if x["category"] == "attraction")
        result = run_planning_action(plan, "update_activity", {
            "activity_id": item["reference_id"], "duration_minutes": 120,
            "ticket_required": False, "estimated_cost": 0, "visit_tips": "带水",
        })
        self.assertEqual(result["trace"], ["coordinator", "route_agent", "audit_agent"])
        self.assertEqual(next(x for x in result["plan"]["itinerary"] if x.get("reference_id") == item["reference_id"])["visit_tips"], "带水")
        moved = run_planning_action(result["plan"], "move_activity", {
            "item_id": item["item_id"], "target_day": 2,
        })
        self.assertTrue(moved["audit"]["success"])

    def test_coordinator_dispatches_scoped_specialists(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph._hotel_places", return_value=[]), patch("agent.planning_graph._activity_places", return_value=[]):
            result = run_planning_action(plan, "candidates", {})
            self.assertEqual(result["trace"], ["coordinator", "transport_agent"])
            plan = result["plan"]
            transport = plan["planning"]["transport_candidates"]
            hotel = plan["planning"]["hotel_candidates"][0]
            plan = run_planning_action(plan, "choose_candidates", {
                "outbound": next(x["id"] for x in transport if x["direction"] == "outbound"),
                "return": next(x["id"] for x in transport if x["direction"] == "return"),
                "hotel": hotel["id"],
            })["plan"]
            with patch("service.travel.guide._extract_with_model", return_value='{"places":[{"name":"西湖","note":"上午游玩"}]}'):
                result = run_planning_action(plan, "guide", {"text": "西湖：上午游玩"})
            self.assertEqual(result["trace"], ["coordinator", "guide_agent", "activity_agent", "place_agent"])
            plan = result["plan"]
            result = run_planning_action(located_plan(plan), "draft", {})
            self.assertEqual(result["trace"], ["coordinator", "prepare_draft", "itinerary_agent", "route_agent", "audit_agent"])
            self.assertTrue(result["plan"]["itinerary"])

    def test_selecting_candidates_immediately_recommends_activities(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph._hotel_places", return_value=[]):
            plan = run_planning_action(plan, "candidates", {})["plan"]
        transports = plan["planning"]["transport_candidates"]
        with patch("agent.planning_graph._activity_places", return_value=[
            {"place_id": "west-lake", "name": "西湖", "address": "杭州西湖区"}
        ]):
            result = run_planning_action(plan, "choose_candidates", {
                "outbound": next(x["id"] for x in transports if x["direction"] == "outbound"),
                "return": next(x["id"] for x in transports if x["direction"] == "return"),
            })
        self.assertEqual(result["trace"], ["coordinator", "hotel_agent", "activity_agent", "place_agent"])
        self.assertIsNone(result["plan"]["planning"]["selected_hotel"])
        self.assertEqual(next(x for x in result["plan"]["planning"]["activities"] if x["category"] == "attraction")["name"], "西湖")

    def test_confirmation_requires_audited_high_issues_to_be_cleared(self):
        plan = create_draft({**FORM, "budget": 1}, "trip-1")
        with patch("agent.planning_graph._hotel_places", return_value=[]):
            plan = run_planning_action(plan, "candidates", {})["plan"]
        transport = plan["planning"]["transport_candidates"]
        plan = run_planning_action(plan, "choose_candidates", {
            "outbound": next(x["id"] for x in transport if x["direction"] == "outbound"),
            "return": next(x["id"] for x in transport if x["direction"] == "return"),
            "hotel": plan["planning"]["hotel_candidates"][0]["id"],
        })["plan"]
        result = run_planning_action(located_plan(plan), "draft", {})
        plan = result["plan"]
        self.assertEqual(result["trace"], ["coordinator", "prepare_draft"] + ["itinerary_agent", "route_agent", "audit_agent"] * 3)
        with self.assertRaises(ValueError):
            run_planning_action(plan, "confirm", {})

    def test_route_agent_uses_available_map_duration_for_audit(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph._hotel_places", return_value=[]):
            plan = run_planning_action(plan, "candidates", {})["plan"]
        transport = plan["planning"]["transport_candidates"]
        plan = run_planning_action(plan, "choose_candidates", {
            "outbound": next(x["id"] for x in transport if x["direction"] == "outbound"),
            "return": next(x["id"] for x in transport if x["direction"] == "return"),
            "hotel": plan["planning"]["hotel_candidates"][0]["id"],
        })["plan"]
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch(
            "agent.planning_graph.amap_compute_route",
            return_value={"success": True, "duration": "50000s", "distance_meters": 1000},
        ):
            result = run_planning_action(located_plan(plan), "draft", {})
        self.assertTrue(any(x["duration_minutes"] == 834 and x["source"] == "高德地图" for x in result["plan"]["routes"]))
        self.assertIn("transport_buffer", {x["type"] for x in result["audit"]["issues"]})

    def test_guide_agent_rejects_invalid_base64_as_user_error(self):
        plan = create_draft(FORM, "trip-1")
        with patch("agent.planning_graph._hotel_places", return_value=[]), patch("agent.planning_graph._activity_places", return_value=[]):
            plan = run_planning_action(plan, "candidates", {})["plan"]
            transports = plan["planning"]["transport_candidates"]
            plan = run_planning_action(plan, "choose_candidates", {
                "outbound": next(x["id"] for x in transports if x["direction"] == "outbound"),
                "return": next(x["id"] for x in transports if x["direction"] == "return"),
                "hotel": plan["planning"]["hotel_candidates"][0]["id"],
            })["plan"]
        with self.assertRaises(ValueError):
            run_planning_action(plan, "guide", {"image_base64": "!"})


if __name__ == "__main__":
    unittest.main()
