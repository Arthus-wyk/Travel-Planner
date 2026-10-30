import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from service.api import server


class DeletePlanApiTests(unittest.TestCase):
    def test_delete_plan_and_missing_plan(self):
        with patch.object(server, "delete_plan", side_effect=[(1, None), (0, None)]) as delete:
            with TestClient(server.create_app(Path("mcp_server.py"), "test")) as client:
                response = client.delete("/api/plans/trip-1")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), {"success": True})
                missing = client.delete("/api/plans/trip-1")
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(delete.call_count, 2)
