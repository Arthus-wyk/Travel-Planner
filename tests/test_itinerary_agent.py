import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

try:
    from agent import itinerary
except ImportError:
    itinerary = None


def sample_plan():
    return {
        "destination": "杭州", "departure_city": "上海",
        "start_date": "2026-11-06", "end_date": "2026-11-08",
        "travelers": 2, "budget": 5000, "pace": "relaxed",
        "interests": ["文化"], "constraints": ["少走路"],
        "planning": {
            "guide_text": "第二天先去灵隐寺，再去西湖；午饭知味观。",
            "guide_constraints": [{"type": "day", "activity_ids": ["temple"], "value": 2}],
            "home_location": "上海的家",
            "selected_transport": {"outbound": "go", "return": "back"},
            "transport_candidates": [
                {"id": "go", "direction": "outbound", "kind": "train", "arrive_time": "10:30"},
                {"id": "back", "direction": "return", "kind": "flight", "depart_time": "19:30"},
                {"id": "not-selected", "direction": "outbound", "kind": "bus"},
            ],
            "selected_hotel": "hotel-a",
            "hotel_candidates": [{"id": "hotel-a", "name": "住处", "place": {"longitude": 120.1, "latitude": 30.2}}],
            "activities": [
                {"id": "temple", "name": "灵隐寺", "category": "attraction", "choice": "must",
                 "duration_minutes": 120, "note": "第二天上午", "visit_tips": "需预约",
                 "place": {"place_id": "manual-branch", "longitude": 120.1001, "latitude": 30.2001}},
                {"id": "food", "name": "知味观", "category": "food", "choice": "optional", "duration_minutes": 60},
                {"id": "excluded", "name": "不去", "choice": "exclude"},
                {"id": "hotel-a", "name": "住处", "category": "hotel", "choice": "must"},
            ],
        },
    }




class ItineraryAgentTests(unittest.TestCase):
    def setUp(self):
        self.chat = AsyncMock()
        client = patch("service.utils.model_client.create_model_client", return_value=SimpleNamespace(chat=self.chat))
        model = patch("service.utils.model_client.get_model", return_value="test-model")
        client.start(); model.start()
        self.addCleanup(client.stop); self.addCleanup(model.stop)

    def generate(self, placements, plan=None, feedback=None):
        self.chat.return_value = SimpleNamespace(message=SimpleNamespace(
            content=json.dumps({"placements": placements}, ensure_ascii=False), tool_calls=[]))
        return itinerary.generate_itinerary_proposal(plan or sample_plan(), feedback)

    def test_guide_and_feedback_reach_model_without_internal_ids(self):
        feedback = [{"type": "guide_constraint", "target": ["temple"], "message": "temple 应在第2天"}]
        self.generate([{"day": 2, "time_of_day": "09:00", "reason": "攻略指定上午"},
                       {"day": None, "time_of_day": None, "reason": "时间不够"}], feedback=feedback)
        request = self.chat.call_args.kwargs
        snapshot = json.loads(request["messages"][1]["content"])
        self.assertIn("第二天先去灵隐寺", snapshot["guide_text"])
        self.assertEqual(snapshot["ordered_places"][0]["name"], "灵隐寺")
        self.assertEqual(snapshot["ordered_places"][0]["coordinates"], {"longitude": 120.1001, "latitude": 30.2001})
        self.assertNotIn("temple", request["messages"][1]["content"])
        self.assertIn("输入第 1 个地点", request["messages"][1]["content"])
        self.assertEqual(request["format"]["properties"]["placements"]["maxItems"], 2)

    def test_rejects_tool_response_and_wrong_shape(self):
        for content, calls in [('{}', []), ('', [{"function": {"name": "submit_itinerary_schedule"}}])]:
            self.chat.return_value = SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))
            with self.assertRaises(ValueError):
                itinerary.generate_itinerary_proposal(sample_plan())

    def test_rejects_invalid_day_time_reason_and_extra_fields(self):
        good = {"day": 2, "time_of_day": "09:00", "reason": "攻略上午"}
        for entry in ({**good, "day": 4}, {**good, "time_of_day": "25:00"},
                      {**good, "reason": ""}, {**good, "place": "invented"},
                      {"day": None, "time_of_day": "09:00", "reason": "冲突"}):
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                self.generate([entry, {"day": None, "time_of_day": None, "reason": "未安排"}])

    def test_invalid_time_error_explains_value_and_allowed_range(self):
        with self.assertRaisesRegex(ValueError, "day=4.*time_of_day='9:00'.*1..3.*HH:MM"):
            self.generate([{"day": 4, "time_of_day": "9:00", "reason": "上午"},
                           {"day": None, "time_of_day": None, "reason": "未安排"}])

    def test_required_place_can_be_unscheduled_for_audit(self):
        result = self.generate([{"day": None, "time_of_day": None, "reason": "与返程冲突"},
                                {"day": None, "time_of_day": None, "reason": "午餐时间不够"}])
        self.assertEqual(result["unscheduled"][0]["activity_id"], "temple")
        self.assertEqual(result["unscheduled"][0]["reason"], "与返程冲突")

    def test_timeout_and_transport_error_become_value_error(self):
        for error in (TimeoutError("expired"), RuntimeError("connection failed")):
            with self.subTest(error=type(error).__name__):
                self.chat.reset_mock()
                self.chat.side_effect = error
                with self.assertRaises(ValueError):
                    itinerary.generate_itinerary_proposal(sample_plan())
                self.assertEqual(self.chat.await_count, 1)
        self.chat.side_effect = None

    def test_wait_for_cancels_a_hanging_call(self):
        async def pending(**kwargs):
            await asyncio.Event().wait()
        self.chat.side_effect = pending
        with patch.object(itinerary, "MODEL_TIMEOUT_SECONDS", 0.01), self.assertRaisesRegex(ValueError, "超时"):
            itinerary.generate_itinerary_proposal(sample_plan())

    def test_invalid_input_fails_before_model(self):
        plan = sample_plan()
        plan["start_date"] = "invalid"
        with self.assertRaises(ValueError):
            itinerary.generate_itinerary_proposal(plan)
        self.chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
