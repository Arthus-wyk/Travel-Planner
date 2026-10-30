import unittest
from agent.itinerary import _validate_proposal


class RouteGroundedProposalTests(unittest.TestCase):
    def test_days_order_and_modes_bind_to_saved_ids(self):
        data = {"days": [{"day": 1, "visits": []}, {"day": 2, "visits": [
            {"position": 2, "travel_mode": "TRANSIT"}, {"position": 1, "travel_mode": "WALK"}]},
            {"day": 3, "visits": []}], "unscheduled": []}
        proposal = _validate_proposal(data, [{"id": "temple"}, {"id": "food"}], 3)
        self.assertEqual([x["activity_id"] for x in proposal["days"][1]["visits"]], ["food", "temple"])

    def test_duplicate_position_is_rejected(self):
        data = {"days": [{"day": 1, "visits": [{"position": 1, "travel_mode": "WALK"},
            {"position": 1, "travel_mode": "WALK"}]}], "unscheduled": []}
        with self.assertRaises(ValueError):
            _validate_proposal(data, [{"id": "a"}], 1)

    def test_graph_preserves_suggested_travel_mode(self):
        from unittest.mock import patch
        from agent.planning_graph import run_planning_action
        from test_guide_graph_workflow import ready
        plan = ready()
        visits = [{"activity_id": item["id"], "travel_mode": "WALK"}
                  for item in plan["planning"]["activities"] if item.get("category") != "hotel"]
        proposal = {"days": [{"day": day, "visits": visits if day == 2 else []}
                             for day in range(1, 4)], "unscheduled": [],
                    "assumptions": [], "constraint_results": []}
        with patch("agent.planning_graph.generate_itinerary_proposal", return_value=proposal), patch(
                "agent.planning_graph.AMAP_API_KEY", ""):
            result = run_planning_action(plan, "draft", {})
        self.assertTrue(any(route["travel_mode"] == "WALK" for route in result["plan"]["routes"]))

    def test_route_duration_moves_later_visit(self):
        from service.travel.planning import schedule_itinerary
        plan = {"itinerary": [
            {"day": 1, "item_id": "a", "time_of_day": "09:00", "duration_minutes": 60, "category": "attraction"},
            {"day": 1, "item_id": "b", "time_of_day": "10:00", "duration_minutes": 30, "category": "attraction"}],
            "routes": [{"origin_item_id": "a", "destination_item_id": "b", "duration_minutes": 45}]}
        self.assertEqual(schedule_itinerary(plan)["itinerary"][1]["time_of_day"], "10:45")
