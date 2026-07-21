import unittest

from agent.executor import ToolExecutor
from tooling.context import ToolContext
from tooling.errors import ToolPolicyError, ToolValidationError
from tooling.registry import ToolRegistry
from tooling.spec import ToolSpec
from tools.catalog import domain_tools, tool_domains


async def echo(arguments, context):
    return {"value": arguments["value"], "actor": context.actor_id}


class ToolingArchitectureTests(unittest.IsolatedAsyncioTestCase):
    async def test_executor_validates_and_invokes_registered_tool(self):
        registry = ToolRegistry([ToolSpec("echo", "Echo a value", {"type": "object", "required": ["value"]}, echo)])
        executor = ToolExecutor(registry)

        result = await executor.execute("echo", {"value": 7}, ToolContext(actor_id="user-1"))

        self.assertEqual(result, {"value": 7, "actor": "user-1"})

    async def test_executor_rejects_missing_required_argument(self):
        executor = ToolExecutor(ToolRegistry([ToolSpec("echo", "Echo", {"required": ["value"]}, echo)]))
        with self.assertRaises(ToolValidationError):
            await executor.execute("echo", {})

    async def test_policy_requires_confirmation_for_destructive_tool(self):
        executor = ToolExecutor(ToolRegistry([ToolSpec("erase", "Erase", {}, echo, risk="destructive")]))
        with self.assertRaises(ToolPolicyError):
            await executor.execute("erase", {})

    def test_catalog_has_project_business_domains(self):
        self.assertIn("create_travel_plan", tool_domains())
        self.assertIn("add_itinerary_item", domain_tools()["itinerary"])
        self.assertIn("read_travel_preference_profile", domain_tools()["memory"])
