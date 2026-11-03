import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from agent.itinerary import generate_itinerary_proposal
from test_itinerary_agent import sample_plan


class OutputContractTests(unittest.TestCase):
    def setUp(self):
        self.chat = AsyncMock()
        client = patch("service.utils.model_client.create_model_client", return_value=SimpleNamespace(chat=self.chat))
        model = patch("service.utils.model_client.get_model", return_value="test-model")
        client.start(); model.start()
        self.addCleanup(client.stop); self.addCleanup(model.stop)

    def propose(self, days, plan=None):
        self.chat.return_value = SimpleNamespace(message=SimpleNamespace(content=json.dumps({"days": days}), tool_calls=[]))
        return generate_itinerary_proposal(plan or sample_plan())

    def test_server_binds_positions_without_exposing_ids(self):
        result = self.propose([{"day": 1, "visits": []}, {"day": 2, "visits": [{"position": 1}, {"position": 2}]},
                               {"day": 3, "visits": []}])
        self.assertEqual(result["days"][1]["visits"], [{"activity_id": "temple"}, {"activity_id": "food"}])
        self.assertNotIn("unscheduled", result)
        request = self.chat.call_args.kwargs
        self.assertEqual(request["tools"], [])
        self.assertFalse(request["format"]["additionalProperties"])
        self.assertEqual(set(request["format"]["properties"]), {"days"})
        self.assertNotIn("temple", request["messages"][1]["content"])

    def test_empty_places_use_empty_days(self):
        plan = sample_plan(); plan["planning"]["activities"] = []
        result = self.propose([{"day": day, "visits": []} for day in range(1, 4)], plan)
        self.assertNotIn("unscheduled", result)

    def test_rejects_extra_fields(self):
        self.chat.return_value = SimpleNamespace(message=SimpleNamespace(content=json.dumps({"days": [
            {"day": 1, "visits": []}, {"day": 2, "visits": []}, {"day": 3, "visits": []}],
            "unscheduled": []}), tool_calls=[]))
        with self.assertRaisesRegex(ValueError, "只能提交 days"):
            generate_itinerary_proposal(sample_plan())
