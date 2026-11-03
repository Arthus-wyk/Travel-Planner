import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from planning_fixtures import located_plan, mock_itinerary_proposal

from service.api import server


FORM = {"destination": "杭州", "departure_city": "上海", "start_date": "2026-11-06",
        "end_date": "2026-11-08", "travelers": 2, "budget": 5000}


class GuidedApiTests(unittest.TestCase):
    def setUp(self):
        self.plans = {}
        self.patches = [
            patch.object(server, "save_plan", side_effect=lambda plan: self.plans.update({plan["plan_id"]: plan})),
            patch.object(server, "get_plan", side_effect=lambda plan_id: (self.plans.get(plan_id), None if plan_id in self.plans else {"message": "Travel plan not found."})),
            patch("agent.planning_graph._hotel_places", return_value=[]),
            patch("agent.planning_graph.AMAP_API_KEY", ""),
            patch("agent.planning_graph.generate_itinerary_proposal", side_effect=mock_itinerary_proposal),
        ]
        for item in self.patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
        self.client = TestClient(server.create_app(Path("mcp_server.py"), "test"))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def request(self, path, payload):
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def get(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_form_and_candidate_actions_persist_plan(self):
        created = self.request("/api/plans/drafts", FORM)
        self.assertTrue(created["success"])
        plan_id = created["plan"]["plan_id"]
        candidates = self.request(f"/api/plans/{plan_id}/planning/candidates", {})
        self.assertEqual(candidates["trace"], ["coordinator", "transport_agent"])
        self.assertEqual(self.plans[plan_id]["planning"]["stage"], "candidates")

    def test_draft_form_accepts_trip_days(self):
        form = {key: value for key, value in FORM.items() if key != "end_date"}
        created = self.request("/api/plans/drafts", {**form, "trip_days": 3})
        self.assertEqual(created["plan"]["end_date"], "2026-11-08")

    def test_audit_is_readable_after_reload_and_sheet_requires_confirmation(self):
        created = self.request("/api/plans/drafts", FORM)
        plan_id = created["plan"]["plan_id"]
        self.request(f"/api/plans/{plan_id}/planning/candidates", {})
        candidates = self.plans[plan_id]["planning"]["transport_candidates"]
        hotel = self.plans[plan_id]["planning"]["hotel_candidates"][0]
        self.request(f"/api/plans/{plan_id}/planning/choose_candidates", {
            "outbound": next(x["id"] for x in candidates if x["direction"] == "outbound"),
            "return": next(x["id"] for x in candidates if x["direction"] == "return"),
            "hotel": hotel["id"],
        })
        self.plans[plan_id] = located_plan(self.plans[plan_id])
        self.request(f"/api/plans/{plan_id}/planning/draft", {})
        self.assertTrue(self.get(f"/api/plans/{plan_id}/planning/audit")["audit"]["success"])
        self.assertEqual(self.client.get(f"/api/plans/{plan_id}/sheet").status_code, 400)

    def test_confirmed_sheet_records_actual_purchase_price(self):
        created = self.request("/api/plans/drafts", FORM)
        plan_id = created["plan"]["plan_id"]
        self.request(f"/api/plans/{plan_id}/planning/candidates", {})
        transport = self.plans[plan_id]["planning"]["transport_candidates"]
        outbound = next(x for x in transport if x["direction"] == "outbound")
        self.request(f"/api/plans/{plan_id}/planning/choose_candidates", {
            "outbound": outbound["id"],
            "return": next(x["id"] for x in transport if x["direction"] == "return"),
            "hotel": self.plans[plan_id]["planning"]["hotel_candidates"][0]["id"],
        })
        self.request(f"/api/plans/{plan_id}/planning/add_activity", {"name": "西湖"})
        self.plans[plan_id] = located_plan(self.plans[plan_id])
        self.request(f"/api/plans/{plan_id}/planning/draft", {})
        self.request(f"/api/plans/{plan_id}/planning/confirm", {})
        before = self.get(f"/api/plans/{plan_id}/sheet")["sheet"]["budget"]["estimated_total"]
        self.request(f"/api/plans/{plan_id}/planning/purchase", {
            "reference_id": outbound["id"], "actual_total": 80,
        })
        sheet = self.get(f"/api/plans/{plan_id}/sheet")["sheet"]
        self.assertEqual(sheet["budget"]["estimated_total"], before + 80)
        self.assertEqual(sheet["purchases"][0]["actual_total"], 80)

    def test_budget_repair_persists_and_returns_new_audit(self):
        plan_id = self.request("/api/plans/drafts", FORM)["plan"]["plan_id"]
        updated = self.request(f"/api/plans/{plan_id}/planning/update_budget", {"budget": 6000})
        self.assertEqual(updated["plan"]["budget"], 6000)
        self.assertIn("audit", updated)
        self.assertEqual(self.get(f"/api/plans/{plan_id}")["plan"]["budget"], 6000)
        invalid = self.client.post(f"/api/plans/{plan_id}/planning/update_budget", json={"budget": -1})
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.plans[plan_id]["budget"], 6000)


if __name__ == "__main__":
    unittest.main()
