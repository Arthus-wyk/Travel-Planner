import unittest
from unittest.mock import patch

from service.travel.itinerary_audit import audit_plan
from service.mcp import server


def plan_with_separate_costs():
    return {
        "plan_id": "trip-1",
        "destination": "Beijing",
        "start_date": "2026-07-20",
        "end_date": "2026-07-20",
        "travelers": 1,
        "budget": 950,
        "currency": "CNY",
        "interests": [],
        "constraints": [],
        "itinerary": [{"item_id": "museum", "day": 1, "time_of_day": "09:00", "duration_minutes": 60, "category": "attraction", "estimated_cost": 100}],
        "recommendations": [],
        "budget_items": [{"category": "hotel", "amount": 900}],
    }


class BudgetReportingTests(unittest.TestCase):
    def test_audit_flags_possible_overrun_when_costs_may_be_separate(self):
        result = audit_plan(plan_with_separate_costs())

        self.assertIn("budget_risk", {item["type"] for item in result["issues"]})
        self.assertNotIn("budget_overrun", {item["type"] for item in result["issues"]})
        self.assertEqual(result["metrics"]["tracked_cost_range"], {"min": 900, "max": 1000})

    def test_summary_shows_remaining_budget_range(self):
        with patch.object(server, "get_plan", return_value=(plan_with_separate_costs(), None)):
            summary = server.summarize_travel_plan("trip-1")["summary"]

        self.assertEqual(summary["tracked_cost_range"], {"min": 900, "max": 1000})
        self.assertEqual(summary["remaining_budget_range"], {"min": -50, "max": 50})


if __name__ == "__main__":
    unittest.main()
