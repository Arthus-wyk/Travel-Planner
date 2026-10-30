import unittest
from unittest.mock import patch

from service.mcp import server


def make_plan():
    return {
        "plan_id": "trip-1",
        "destination": "Beijing",
        "start_date": "2026-07-20",
        "end_date": "2026-07-20",
        "budget": 1000,
        "itinerary": [
            {
                "item_id": "itinerary_1",
                "day": 1,
                "time_of_day": "09:00",
                "title": "故宫",
                "location": "故宫，北京",
                "category": "attraction",
                "duration_minutes": 120,
                "estimated_cost": 60,
            },
            {
                "item_id": "itinerary_2",
                "day": 1,
                "time_of_day": "14:00",
                "title": "颐和园",
                "location": "颐和园，北京",
                "category": "attraction",
                "duration_minutes": 120,
                "estimated_cost": 30,
            },
        ],
        "routes": [
            {
                "route_id": "route:itinerary_1:itinerary_2:drive",
                "origin_item_id": "itinerary_1",
                "destination_item_id": "itinerary_2",
                "travel_mode": "DRIVE",
            }
        ],
        "recommendations": [],
        "budget_items": [],
    }


class ItineraryReconciliationTests(unittest.TestCase):
    def test_route_search_without_results_reports_failure(self):
        with patch.object(server, "compute_route", return_value=[]):
            result = server.amap_compute_route("故宫，北京", "颐和园，北京")

        self.assertFalse(result["success"])
        self.assertIn("No route", result["message"])

    def test_delete_removes_item_and_invalidates_related_routes(self):
        plan = make_plan()
        with patch.object(server, "get_plan", return_value=(plan, None)), patch.object(
            server, "save_plan"
        ):
            result = server.delete_itinerary_item("trip-1", "itinerary_1")

        self.assertTrue(result["success"])
        self.assertEqual(result["affected_days"], [1])
        self.assertEqual(plan["itinerary"][0]["item_id"], "itinerary_2")
        self.assertEqual(plan["routes"], [])

    def test_move_rejects_target_overlap(self):
        plan = make_plan()
        with patch.object(server, "get_plan", return_value=(plan, None)), patch.object(
            server, "save_plan"
        ) as save_plan:
            result = server.move_itinerary_item("trip-1", "itinerary_1", 1, "13:00")

        self.assertFalse(result["success"])
        self.assertEqual(result["conflicting_item_id"], "itinerary_2")
        save_plan.assert_not_called()

    def test_move_returns_both_affected_days_and_invalidates_routes(self):
        plan = make_plan()
        with patch.object(server, "get_plan", return_value=(plan, None)), patch.object(
            server, "save_plan"
        ):
            result = server.move_itinerary_item("trip-1", "itinerary_1", 2, "10:00")

        self.assertTrue(result["success"])
        self.assertEqual(result["affected_days"], [1, 2])
        self.assertEqual(plan["routes"], [])

    def test_reconcile_persists_adjacent_routes_and_audit(self):
        plan = make_plan()
        route_response = [{
            "distanceMeters": 21000,
            "duration": "1800s",
            "staticDuration": "1800s",
            "localizedValues": {"duration": {"text": "30分钟"}},
        }]
        with patch.object(server, "get_plan", return_value=(plan, None)), patch.object(
            server, "save_plan"
        ) as save_plan, patch.object(
            server, "compute_route", return_value=route_response
        ):
            result = server.reconcile_travel_plan(
                "trip-1", affected_days=[1]
            )

        self.assertTrue(result["success"])
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(plan["routes"][0]["distance_meters"], 21000)
        self.assertIn("audit", result)
        save_plan.assert_called_once()

    def test_reconcile_without_routes_reports_failure(self):
        plan = make_plan()
        with patch.object(server, "get_plan", return_value=(plan, None)), patch.object(
            server, "save_plan"
        ), patch.object(server, "compute_route", return_value=[]):
            result = server.reconcile_travel_plan("trip-1", affected_days=[1])

        self.assertFalse(result["success"])
        self.assertEqual(len(result["route_errors"]), 1)


if __name__ == "__main__":
    unittest.main()
