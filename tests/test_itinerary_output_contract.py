import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent.itinerary import generate_itinerary_proposal
from test_itinerary_agent import sample_plan


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.chat = AsyncMock()
        self.client = patch("service.utils.model_client.create_model_client", return_value=SimpleNamespace(chat=self.chat))
        self.model = patch("service.utils.model_client.get_model", return_value="test-model")
        self.client.start(); self.model.start()
        self.addCleanup(self.client.stop); self.addCleanup(self.model.stop)

    def propose(self, placements, plan=None):
        self.chat.return_value = SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[{"function": {
            "name": "submit_itinerary_schedule", "arguments": json.dumps({"placements": placements}, ensure_ascii=False)}}]))
        return generate_itinerary_proposal(plan or sample_plan())

    def test_server_binds_positions_and_duration_without_exposing_ids(self):
        result = self.propose([{"day": 2, "time_of_day": "09:00", "reason": "攻略第二天上午"},
                               {"day": None, "time_of_day": None, "reason": "午餐时间不足"}])
        self.assertEqual(result["stops"], [{"activity_id": "temple", "day": 2, "time_of_day": "09:00", "duration_minutes": 120, "reason": "攻略第二天上午"}])
        self.assertEqual(result["unscheduled"], [{"activity_id": "food", "reason": "午餐时间不足"}])
        request = self.chat.call_args.kwargs
        self.assertEqual(request["tools"][0]["function"]["name"], "submit_itinerary_schedule")
        self.assertNotIn("activity_id", json.dumps(request["tools"], ensure_ascii=False))
        self.assertNotIn("temple", request["messages"][1]["content"])
        self.assertIn("第二天先去灵隐寺", request["messages"][1]["content"])

    def test_rejects_missing_extra_and_injected_identity(self):
        valid = {"day": 1, "time_of_day": "10:00", "reason": "参观"}
        for placements in ([valid], [valid] * 3, [{**valid, "activity_id": "invented"}, valid],
                           [{"day": 1, "time_of_day": None, "reason": "冲突"}, valid]):
            with self.subTest(placements=placements), self.assertRaises(ValueError):
                self.propose(placements)

    def test_empty_places_uses_empty_vector(self):
        plan = sample_plan()
        plan["planning"]["activities"] = []
        self.assertEqual(self.propose([], plan)["stops"], [])


if __name__ == "__main__":
    unittest.main()
