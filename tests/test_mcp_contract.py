"""与重写前快照比较，不访问数据库、模型或地图服务。"""

import json
from pathlib import Path
import unittest
from jsonschema import Draft202012Validator, ValidationError
from mcp.server.fastmcp import FastMCP
from tools.catalog import register_all, tool_metadata
from tooling.mcp import build_registry
from agent.tool_calls import model_tools
from service.validation import VALID_PROFILE_FIELDS
from service.mcp.server import update_travel_profile
from unittest.mock import patch


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
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
        mcp = FastMCP("profile-schema-test")
        register_all(mcp)
        tool = next(t for t in await mcp.list_tools() if t.name == "update_travel_profile")
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
        mcp = FastMCP("profile-guidance-test")
        register_all(mcp)
        registry = build_registry(None, await mcp.list_tools(), tool_metadata())
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
        mcp = FastMCP("contract-test")
        register_all(mcp)
        actual = {tool.name: tool.inputSchema for tool in await mcp.list_tools()}
        self.assertEqual(actual, expected)
