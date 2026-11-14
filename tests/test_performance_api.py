import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import server
from service.storage import planning_jobs
from service.storage.travel_db import compact_routes


class PerformanceApiTests(unittest.TestCase):
    def setUp(self):
        writer = patch("service.api.request_logging.write_event")
        writer.start()
        self.addCleanup(writer.stop)

    def test_compact_routes_preserves_map_geometry_and_mode_choice(self):
        legs = [{"steps": [{"polyline": "1,2;3,4", "instruction": "large"}]}]
        plan = [{"legs": legs, "route_options": {"WALK": {
            "legs": legs, "success": True, "duration": "60s", "raw_routes": ["large"]
        }}}]
        compact = compact_routes(plan)
        self.assertEqual(compact[0]["legs"], [{"polyline": "1,2;3,4"}])
        self.assertEqual(compact[0]["route_options"]["WALK"], {
            "legs": [{"polyline": "1,2;3,4"}], "success": True, "duration": "60s"
        })
        self.assertEqual(compact_routes(compact), compact)

    def test_detail_omits_route_payloads_and_review_accepts_version(self):
        plan = {"budget": 100, "routes": [{"legs": [{"polyline": "1,2;3,4"}],
                "route_options": {"WALK": {"duration": "60s", "raw_routes": ["large"], "legs": []}}}]}
        with patch.object(server, "get_plan", return_value=(plan, None)), \
             patch.object(server, "run_planning_action", return_value={"plan": plan, "trace": []}), \
             patch.object(planning_jobs, "save_review_changes") as save, \
             TestClient(server.create_app(Path("mcp_server.py"), "test"), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 50000)) as client:
            detail = client.get("/api/plans/p").json()
            self.assertNotIn("legs", detail["plan"]["routes"][0])
            self.assertEqual(detail["plan"]["routes"][0]["route_options"]["WALK"], {"duration": "60s"})
            self.assertEqual(detail["plan_version"], planning_jobs.fingerprint(plan))
            url = "/api/plans/p/planning/review_changes"
            self.assertEqual(client.post(url, json={"plan_version": "stale", "operations": []}).status_code, 409)
            self.assertEqual(client.post(url, json={"plan_version": detail["plan_version"], "operations": []}).status_code, 200)
            save.assert_called_once_with("p", plan, plan)

    def test_log_timeline_defers_payload_and_searches_full_event(self):
        run_id = "a" * 32
        event = {"type": "model_response", "timestamp": "2026-11-13T00:00:00Z",
                 "content": "needle"}
        with patch.object(server, "read_run", return_value={"run": {}, "events": [event], "truncated": False}), \
             TestClient(server.create_app(Path("mcp_server.py"), "test"), base_url="http://127.0.0.1",
                        client=("127.0.0.1", 50000)) as client:
            url = f"/api/logs/runs/{run_id}"
            timeline = client.get(url, params={"view": "timeline"}).json()["events"]
            self.assertNotIn("content", timeline[0])
            self.assertEqual(client.get(url, params={"view": "timeline", "search": "needle"}).json()["events"], timeline)
            self.assertEqual(client.get(f"{url}/events/{timeline[0]['eventId']}").json()["event"], event)
