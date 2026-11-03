import unittest
from agent.itinerary import _validate_proposal


class RouteGroundedProposalTests(unittest.TestCase):
    def test_route_agent_chooses_fastest_valid_mode_and_keeps_fare(self):
        from unittest.mock import patch
        from agent.planning_graph import route_agent
        plan = {"itinerary": [{"item_id": "a", "location": "a", "category": "attraction"},
                              {"item_id": "b", "location": "b", "category": "attraction"}],
                "routes": [{"origin_item_id": "a", "destination_item_id": "b"}]}
        def route(origin, destination, travel_mode):
            values = {"WALK": ("600s", None), "TRANSIT": ("360s", 3), "DRIVE": ("240s", 18)}
            duration, fare = values[travel_mode]
            return {"success": True, "duration": duration, "distance_meters": 1000, "legs": [], "fare": fare}
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route", side_effect=route) as api:
            result = route_agent({"plan": plan, "action": "draft", "trace": []})["plan"]["routes"][0]
        self.assertEqual({call.kwargs["travel_mode"] for call in api.call_args_list}, {"WALK", "TRANSIT", "DRIVE"})
        self.assertEqual((result["travel_mode"], result["duration_minutes"], result["fare"]), ("DRIVE", 4, 18))

    def test_route_agent_marks_all_failed_modes_unverified(self):
        from unittest.mock import patch
        from agent.planning_graph import route_agent
        plan = {"itinerary": [{"item_id": "a", "location": "a", "category": "attraction"},
                              {"item_id": "b", "location": "b", "category": "attraction"}],
                "routes": [{"origin_item_id": "a", "destination_item_id": "b", "fare": 12}]}
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route", return_value={"success": False, "message": "不可用"}):
            route = route_agent({"plan": plan, "action": "draft", "trace": []})["plan"]["routes"][0]
        self.assertEqual(route["source"], "provisional_estimate")
        self.assertNotIn("fare", route)
        self.assertIn("不可用", route["error"])

    def test_user_selected_route_mode_is_respected(self):
        from unittest.mock import patch
        from agent.planning_graph import route_agent
        plan = {"itinerary": [{"item_id": "a", "location": "a", "category": "attraction"},
                              {"item_id": "b", "location": "b", "category": "attraction"}],
                "routes": [{"origin_item_id": "a", "destination_item_id": "b", "travel_mode": "WALK", "mode_locked": True}]}
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route", return_value={"success": True, "duration": "600s"}) as api:
            route = route_agent({"plan": plan, "action": "draft", "trace": []})["plan"]["routes"][0]
        self.assertEqual(route["travel_mode"], "WALK")
        self.assertEqual(api.call_count, 3)

    def test_days_order_binds_to_saved_ids_and_covers_every_place(self):
        data = {"days": [{"day": 1, "visits": []}, {"day": 2, "visits": [
            {"position": 2}, {"position": 1}]}, {"day": 3, "visits": []}]}
        proposal = _validate_proposal(data, [{"id": "temple"}, {"id": "food"}], 3)
        self.assertEqual([x["activity_id"] for x in proposal["days"][1]["visits"]], ["food", "temple"])
        self.assertNotIn("unscheduled", proposal)

    def test_duplicate_position_is_rejected(self):
        data = {"days": [{"day": 1, "visits": [{"position": 1},
            {"position": 1}]}]}
        with self.assertRaises(ValueError):
            _validate_proposal(data, [{"id": "a"}], 1)

    def test_omitted_visits_are_generation_issues_not_pending(self):
        from service.travel.planning import draft_itinerary
        from test_guide_graph_workflow import ready
        plan = ready()
        selected = [x for x in plan["planning"]["activities"] if x.get("category") != "hotel" and x.get("choice") != "exclude"]
        proposal = {"days": [{"day": day, "visits": []}
                             for day in range(1, 4)], "unscheduled": []}
        result = draft_itinerary(plan, proposal)
        self.assertEqual(result["planning"]["unscheduled_items"], [])
        self.assertNotIn("unscheduled", result["planning"]["generation"])
        self.assertTrue(result["planning"]["generation_issues"])

    def test_graph_inserts_fixed_anchors_and_pending_places(self):
        from unittest.mock import patch
        from agent.planning_graph import run_planning_action
        from test_guide_graph_workflow import ready
        plan = ready()
        visits = [{"activity_id": item["id"]}
                  for item in plan["planning"]["activities"] if item.get("category") != "hotel"]
        proposal = {"days": [{"day": day, "visits": visits if day == 2 else []}
                             for day in range(1, 4)], "unscheduled": [],
                    "assumptions": [], "constraint_results": []}
        with patch("agent.planning_graph.generate_itinerary_proposal", return_value=proposal), patch(
                "agent.planning_graph.AMAP_API_KEY", ""):
            result = run_planning_action(plan, "draft", {})
        self.assertEqual(result["plan"]["itinerary"][0]["category"], "home_anchor")
        self.assertEqual(result["plan"]["itinerary"][-1]["category"], "home_anchor")

    def test_route_duration_moves_later_visit(self):
        from service.travel.planning import schedule_itinerary
        plan = {"itinerary": [
            {"day": 1, "item_id": "a", "time_of_day": "09:00", "duration_minutes": 60, "category": "attraction"},
            {"day": 1, "item_id": "b", "time_of_day": "10:00", "duration_minutes": 30, "category": "attraction"}],
            "routes": [{"origin_item_id": "a", "destination_item_id": "b", "duration_minutes": 45}]}
        self.assertEqual(schedule_itinerary(plan)["itinerary"][1]["time_of_day"], "10:45")
