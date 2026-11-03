import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent import itinerary


def sample_plan():
    return {
        "destination": "杭州", "departure_city": "上海", "start_date": "2026-11-06", "end_date": "2026-11-08",
        "travelers": 2, "budget": 5000, "pace": "relaxed",
        "planning": {"guide_text": "第二天先去灵隐寺，再去西湖；午饭知味观。",
                     "guide_constraints": [{"type": "day", "activity_ids": ["temple"], "value": 2}],
                     "activities": [{"id": "temple", "name": "灵隐寺", "category": "attraction", "choice": "must",
                                     "place": {"longitude": 120.1, "latitude": 30.2}},
                                    {"id": "food", "name": "知味观", "category": "food", "choice": "optional"},
                                    {"id": "excluded", "name": "不去", "choice": "exclude"}]}}


class ItineraryAgentTests(unittest.TestCase):
    def setUp(self):
        self.chat = AsyncMock()
        client = patch("service.utils.model_client.create_model_client", return_value=SimpleNamespace(chat=self.chat))
        model = patch("service.utils.model_client.get_model", return_value="test-model")
        client.start(); model.start()
        self.addCleanup(client.stop); self.addCleanup(model.stop)

    def propose(self, days, feedback=None):
        self.chat.return_value = SimpleNamespace(message=SimpleNamespace(content=json.dumps({"days": days}), tool_calls=[]))
        return itinerary.generate_itinerary_proposal(sample_plan(), feedback)

    def test_model_gets_guide_and_feedback_but_only_returns_order(self):
        result = self.propose([{"day": 1, "visits": []}, {"day": 2, "visits": [{"position": 1}, {"position": 2}]},
                               {"day": 3, "visits": []}], [{"message": "temple 应在第2天"}])
        request = self.chat.call_args.kwargs
        snapshot = json.loads(request["messages"][1]["content"])
        self.assertIn("第二天先去灵隐寺", snapshot["guide_text"])
        self.assertEqual(snapshot["ordered_places"][0]["coordinates"], {"longitude": 120.1, "latitude": 30.2})
        self.assertIn("输入第 1 个地点", request["messages"][1]["content"])
        self.assertNotIn("temple", request["messages"][1]["content"])
        self.assertEqual(set(request["format"]["properties"]), {"days"})
        self.assertIn(json.dumps(request["format"], ensure_ascii=False), request["messages"][0]["content"])
        self.assertNotIn("unscheduled", result)

    def test_rejects_duplicate_and_invalid_position(self):
        for visits in ([{"position": 1}, {"position": 1}], [{"position": 99}], [{"position": 1, "travel_mode": "WALK"}]):
            with self.subTest(visits=visits), self.assertRaises(ValueError):
                self.propose([{"day": 1, "visits": visits}, {"day": 2, "visits": []}, {"day": 3, "visits": []}])

    def test_rejects_tool_response_and_wrong_shape(self):
        for content, calls in [('{}', []), ('', [{"function": {"name": "submit"}}])]:
            self.chat.return_value = SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls))
            with self.assertRaises(ValueError):
                itinerary.generate_itinerary_proposal(sample_plan())

    def test_timeout_and_transport_error_become_value_error(self):
        for error in (TimeoutError("expired"), RuntimeError("connection failed")):
            self.chat.side_effect = error
            with self.assertRaises(ValueError):
                itinerary.generate_itinerary_proposal(sample_plan())
        self.chat.side_effect = None

    def test_wait_for_cancels_a_hanging_call(self):
        async def pending(**kwargs):
            await asyncio.Event().wait()
        self.chat.side_effect = pending
        with patch.object(itinerary, "MODEL_TIMEOUT_SECONDS", 0.01), self.assertRaisesRegex(ValueError, "超时"):
            itinerary.generate_itinerary_proposal(sample_plan())

    def test_invalid_input_fails_before_model(self):
        plan = sample_plan(); plan["start_date"] = "invalid"
        with self.assertRaises(ValueError):
            itinerary.generate_itinerary_proposal(plan)
        self.chat.assert_not_called()
