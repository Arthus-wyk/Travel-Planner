import unittest

from service.travel.itinerary_audit import audit_plan
from service.validation import time_minutes, validate_time_of_day


class ItineraryScheduleTests(unittest.TestCase):
    def test_time_is_concrete_hhmm(self):
        self.assertEqual(validate_time_of_day("09:30"), (True, None))
        self.assertFalse(validate_time_of_day("morning")[0])
        self.assertEqual(time_minutes("09:30"), 570)

    def test_audit_reports_missing_time_and_overlap(self):
        result = audit_plan(
            {
                "plan_id": "test",
                "start_date": "2026-07-20",
                "end_date": "2026-07-20",
                "itinerary": [
                    {"item_id": "a", "day": 1, "time_of_day": "09:00", "duration_minutes": 90},
                    {"item_id": "b", "day": 1, "time_of_day": "10:00", "duration_minutes": 30},
                    {"item_id": "c", "day": 1, "time_of_day": "morning", "duration_minutes": 30},
                ],
                "recommendations": [],
                "budget_items": [],
                "budget": 0,
            }
        )
        issue_types = {item["type"] for item in result["issues"]}
        self.assertIn("time_conflict", issue_types)
        self.assertIn("missing_schedule_time", issue_types)


if __name__ == "__main__":
    unittest.main()
