import unittest
from agent.intent import understand
from agent.prompts import is_empty_execution_promise
from agent.tool_calls import embedded_calls
from service.chat import public_message_items
from agent.router import select_tools
from tooling.registry import ToolRegistry
from tooling.spec import ToolSpec


class InputAndGuardTests(unittest.TestCase):
    def test_plan_creation_and_referential_schedule_edit_keep_needed_domains(self):
        created = understand("帮我规划东京三天游", None, [])
        self.assertIn("plan", created["semantic"]["domains"])
        edited = understand("把它挪后半小时", {"plan_id": "trip-1"}, [])
        self.assertIn("itinerary", edited["semantic"]["domains"])

    def test_destination_and_trip_days_allow_creating_the_trip(self):
        parsed = understand("去北京玩3天", None, [])
        registry = ToolRegistry(
            [
                ToolSpec(
                    "create_travel_plan",
                    "Create",
                    {},
                    None,
                    domains=frozenset({"plan"}),
                ),
                ToolSpec(
                    "add_itinerary_item",
                    "Add",
                    {},
                    None,
                    domains=frozenset({"itinerary"}),
                ),
            ]
        )
        available = select_tools(registry, parsed["semantic"]["domains"], None)
        self.assertIn("create_travel_plan", available)
        self.assertIn("add_itinerary_item", available)

    def test_existing_plan_is_explicit_context_and_relative_days_do_not_need_dates(
        self,
    ):
        result = understand(
            "帮我规划一个3天2晚的行程出来",
            {"plan_id": "trip-1", "destination": "悉尼"},
            [],
        )
        self.assertEqual(result["semantic"]["slots"]["plan_id"], "trip-1")
        self.assertEqual(result["semantic"]["slots"]["trip_days"], 3)
        self.assertEqual(result["semantic"]["primary_intent"], "update")
        self.assertEqual(result["task_plan"]["tool_sequence"], [])

    def test_explicit_new_trip_is_not_mistaken_for_current_plan_update(self):
        result = understand("新建一个旅行计划", {"plan_id": "trip-1"}, [])
        self.assertEqual(result["semantic"]["primary_intent"], "create")

    def test_embedded_calls_are_deduplicated_and_unknown_tools_ignored(self):
        block = '```json\n{"name":"add_itinerary_item","arguments":{"day":1}}\n```'
        calls = embedded_calls(block + block, {"add_itinerary_item"})
        self.assertEqual(
            calls, [{"name": "add_itinerary_item", "arguments": {"day": 1}}]
        )
        self.assertEqual(embedded_calls(block, {"other"}), [])

    def test_completed_answer_is_not_a_promise(self):
        self.assertFalse(is_empty_execution_promise("Done. The activity was added."))

    def test_public_history_hides_old_internal_and_promise_messages(self):
        visible = public_message_items(
            [
                {"role": "system", "content": "guard"},
                {"role": "assistant", "content": "I will add it now."},
                {"role": "assistant", "content": "完成"},
                {
                    "role": "assistant",
                    "content": "The previous assistant message described future tool work",
                },
            ]
        )
        self.assertEqual([m["content"] for m in visible], ["完成"])
