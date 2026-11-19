"""与重写前快照比较，不访问数据库、模型或地图服务。"""

import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, ValidationError
from mcp.server.lowlevel import Server
from mcp.shared.memory import create_connected_server_and_client_session
from agent.tools.catalog import public_registry, register_all
from agent.tools.calls import model_tools
from service.validation import VALID_PROFILE_FIELDS
from service.mcp.server import update_travel_profile
from unittest.mock import patch


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_mcp_returns_structured_success_and_error(self):
        server = Server("structured-tools")
        register_all(server)
        with patch("service.mcp.server.geocode", return_value=[{"location": "120,30"}]) as geocode:
            async with create_connected_server_and_client_session(server) as session:
                schemas = {tool.name: tool.outputSchema for tool in (await session.list_tools()).tools}
                success = await session.call_tool("amap_geocode", {"address": "西湖"})
                invalid = await session.call_tool("amap_geocode", {"address": 7})
        self.assertFalse(success.isError)
        self.assertEqual(success.structuredContent["data"]["geocodes"], [{"location": "120,30"}])
        self.assertTrue(invalid.isError)
        self.assertEqual(invalid.structuredContent["error"]["code"], "INVALID_ARGUMENT")
        geocode.assert_called_once()
        for reply in (success, invalid):
            Draft202012Validator(schemas["amap_geocode"]).validate(reply.structuredContent)

    async def test_partial_route_result_remains_valid_structured_content(self):
        from agent.tools.implementations.itinerary import service

        route = {"origin_item_id": "a", "destination_item_id": "b", "travel_mode": "WALK", "duration": "600s"}
        missing = {"origin_item_id": "b", "destination_item_id": "c"}
        result = {"success": False, "plan_id": "p", "affected_days": [1],
                  "affected_item_ids": ["a", "b"], "routes": [route],
                  "route_errors": [{**missing, "message": "internal upstream detail"}],
                  "audit": {}, "suggestions": []}
        server = Server("partial-tools")
        malformed = {**result, "routes": [{}], "route_errors": [{}]}
        with patch.object(service, "reconcile_travel_plan", side_effect=[result, malformed]):
            register_all(server)
            async with create_connected_server_and_client_session(server) as session:
                await session.list_tools()
                reply = await session.call_tool("reconcile_travel_plan", {"plan_id": "p"})
                rejected = await session.call_tool("reconcile_travel_plan", {"plan_id": "p"})
        self.assertFalse(reply.isError)
        self.assertEqual(reply.structuredContent["status"], "partial")
        self.assertEqual(reply.structuredContent["data"]["routes"], [route])
        self.assertEqual(reply.structuredContent["error"]["details"]["missing"], [missing])
        self.assertNotIn("internal upstream detail", str(reply.structuredContent))
        self.assertTrue(rejected.isError)
        self.assertIsNone(rejected.structuredContent["data"])

    async def test_public_write_failure_cannot_advertise_safe_replay(self):
        from agent.tools.implementations.plan import service

        server = Server("non-replayable-write")
        with patch.object(service, "delete_travel_plan", side_effect=TimeoutError()) as delete:
            register_all(server)
            async with create_connected_server_and_client_session(server) as session:
                reply = await session.call_tool("delete_travel_plan", {"plan_id": "p"})
        self.assertTrue(reply.isError)
        error = reply.structuredContent["error"]
        self.assertEqual(error["code"], "UPSTREAM_TIMEOUT")
        self.assertFalse(error["retryable"])
        self.assertEqual(error["details"]["recovery"], "REPLAY_UNSAFE")
        delete.assert_called_once()

    async def test_profile_null_fields_preserve_existing_values(self):
        plan = {"plan_id": "p1", "destination": "杭州", "start_date": "2026-10-20",
                "end_date": "2026-10-22", "constraints": ["安静"]}
        updates = {field: None for field in VALID_PROFILE_FIELDS}
        updates["constraints"] = ["安静", "不要辣"]
        with patch("service.mcp.server.get_plan", return_value=(plan, None)), patch(
            "service.mcp.server.save_plan"
        ) as save:
            result = update_travel_profile("p1", updates)
        self.assertTrue(result["success"])
        self.assertEqual(result["plan"]["destination"], "杭州")
        self.assertEqual(result["plan"]["constraints"], ["安静", "不要辣"])
        save.assert_called_once()

    async def test_profile_updates_have_closed_typed_schema(self):
        server = Server("profile-schema-test")
        register_all(server)
        async with create_connected_server_and_client_session(server) as session:
            tool = next(t for t in (await session.list_tools()).tools if t.name == "update_travel_profile")
        validator = Draft202012Validator(tool.inputSchema)
        updates = {field: None for field in VALID_PROFILE_FIELDS}
        updates.update({"constraints": ["不要辣"], "pace": "relaxed"})
        validator.validate({"plan_id": "test", "updates": updates})
        self.assertEqual(
            set(tool.inputSchema["$defs"]["TravelProfileUpdates"]["required"]),
            VALID_PROFILE_FIELDS,
        )
        for field in VALID_PROFILE_FIELDS:
            field_schema = tool.inputSchema["$defs"]["TravelProfileUpdates"]["properties"][field]
            self.assertIn({"type": "null"}, field_schema["anyOf"])
        for updates in ({"dietary_preferences": "不要辣"}, {"constraints": "不要辣"},
                        {"travelers": 0}, {"travelers": True}, {"budget": -1},
                        {"start_date": "tomorrow"}, {}):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                validator.validate({"plan_id": "test", "updates": {**{field: None for field in VALID_PROFILE_FIELDS}, **updates} if updates else {}})

    async def test_profile_update_guidance_reaches_agent(self):
        registry = public_registry()
        tool = model_tools(registry, ["update_travel_profile"])[0]["function"]
        self.assertIn("_retry_of", tool["parameters"]["required"])
        self.assertEqual(tool["parameters"]["properties"]["_retry_of"]["type"], ["string", "null"])
        description = tool["description"]
        for field in VALID_PROFILE_FIELDS:
            self.assertIn(field, description)
        self.assertIn('"constraints": ["不要辣"]', description)
        self.assertIn('"pace": "relaxed"', description)
        self.assertIn("replace", description)
        self.assertIn("YYYY-MM-DD", description)

    async def test_names_and_input_schemas_remain_compatible(self):
        fixture = Path(__file__).parent / "fixtures/mcp_input_schemas.json"
        expected = json.loads(fixture.read_text(encoding="utf-8"))
        server = Server("contract-test")
        register_all(server)
        async with create_connected_server_and_client_session(server) as session:
            tools = (await session.list_tools()).tools
        self.assertEqual({tool.name for tool in tools}, set(expected))
        registry = public_registry()
        for tool in tools:
            with self.subTest(tool=tool.name):
                legacy_properties = set(expected[tool.name].get("properties", {}))
                if tool.name == "amap_compute_route":
                    # The old snapshot predates the existing optional refresh argument.
                    legacy_properties.add("refresh")
                self.assertEqual(set(tool.inputSchema.get("properties", {})),
                                 legacy_properties)
                self.assertEqual(tool.inputSchema, registry.get(tool.name).input_schema)
                self.assertIsNotNone(tool.outputSchema)
                Draft202012Validator.check_schema(tool.outputSchema)
                self.assertEqual("partial" in tool.outputSchema["properties"]["status"]["enum"],
                                 tool.name == "reconcile_travel_plan")
