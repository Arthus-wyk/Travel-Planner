import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from service.agent.client import (
    answer_user,
    extract_embedded_tool_calls,
    is_empty_execution_promise,
)
from service.agent.intent_understanding import build_entity_memory, parse_semantics, build_task_plan
from service.api.server import build_system_prompt, public_message_items


class EmbeddedToolCallTests(unittest.TestCase):
    def test_extracts_fenced_tool_json(self) -> None:
        content = """I will add it now.
```json
{"name":"add_itinerary_item","arguments":{"plan_id":"trip-1","day":1}}
```
"""

        calls = extract_embedded_tool_calls(
            content,
            {"add_itinerary_item"},
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].name, "add_itinerary_item")
        self.assertEqual(calls[0].arguments["plan_id"], "trip-1")

    def test_deduplicates_repeated_json_blocks(self) -> None:
        block = (
            '```json\n{"name":"add_itinerary_item",'
            '"arguments":{"plan_id":"trip-1"}}\n```'
        )

        calls = extract_embedded_tool_calls(
            f"{block}\n{block}",
            {"add_itinerary_item"},
        )

        self.assertEqual(len(calls), 1)

    def test_rejects_unknown_tool(self) -> None:
        content = '```json\n{"name":"delete_everything","arguments":{}}\n```'

        calls = extract_embedded_tool_calls(
            content,
            {"add_itinerary_item"},
        )

        self.assertEqual(calls, [])

    def test_completed_answer_is_not_a_promise(self) -> None:
        self.assertFalse(
            is_empty_execution_promise("Done. The activity was added.")
        )

    def test_public_history_hides_internal_and_promise_messages(self) -> None:
        messages = [
            {"message_id": "1", "role": "system", "content": "guard"},
            {
                "message_id": "2",
                "role": "assistant",
                "content": "I will add it now.",
                "metadata": {},
            },
            {
                "message_id": "3",
                "role": "assistant",
                "content": "The activity was added.",
                "metadata": {},
            },
            {
                "message_id": "4",
                "role": "assistant",
                "content": "The previous assistant message described future tool work but did not call any tool.",
                "metadata": {},
            },
        ]

        visible = public_message_items(messages)

        self.assertEqual(
            [message["content"] for message in visible],
            ["The activity was added."],
        )


class CurrentPlanContextTests(unittest.TestCase):
    def test_current_plan_context_drives_itinerary_task_plan(self) -> None:
        prompt = build_system_prompt(
            {
                "plan_id": "aus-couple",
                "title": "Australia couple trip",
                "destination": "Sydney, Great Barrier Reef, Melbourne, Gold Coast",
                "start_date": None,
                "end_date": None,
                "travelers": 2,
                "budget": 6000,
                "currency": "CNY",
                "interests": [],
                "constraints": [],
                "itinerary": [],
                "recommendations": [],
                "budget_items": [],
            }
        )
        memory = build_entity_memory([{"role": "system", "content": prompt}])

        semantic = parse_semantics("帮我规划一个3天2晚的行程出来", memory)
        task_plan = build_task_plan("帮我规划一个3天2晚的行程出来", semantic, memory)

        self.assertEqual(memory.current_plan_id, "aus-couple")
        self.assertEqual(semantic.slots["plan_id"], "aus-couple")
        self.assertEqual(semantic.slots["trip_days"], 3)
        self.assertIn("get_travel_plan", task_plan.tool_sequence)
        self.assertIn("add_itinerary_item", task_plan.tool_sequence)
        self.assertNotIn("create_travel_plan", task_plan.tool_sequence)


class ToolLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_embedded_json_executes_once_and_only_emits_final_text(
        self,
    ) -> None:
        pseudo_call = """I will add it now. Please wait.
```json
{"name":"add_itinerary_item","arguments":{"plan_id":"trip-1","day":1}}
```
"""
        final_text = "The activity was added to day 1."
        responses = iter(
            [
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=pseudo_call,
                        tool_calls=[],
                    ),
                    usage={},
                ),
                SimpleNamespace(
                    message=SimpleNamespace(
                        role="assistant",
                        content=final_text,
                        tool_calls=[],
                    ),
                    usage={},
                ),
            ]
        )

        async def fake_model_request(**kwargs):
            return next(responses), list(kwargs["messages"]), None

        mcp = SimpleNamespace(
            call_tool=AsyncMock(
                return_value=SimpleNamespace(
                    structuredContent={"status": "success", "item_id": "item-1"},
                    content=[],
                )
            )
        )
        emitted = []

        async def on_text(text: str) -> None:
            emitted.append(text)

        messages = [{"role": "user", "content": "Add this activity."}]
        with patch(
            "service.agent.client.request_model_with_retry",
            side_effect=fake_model_request,
        ):
            answer = await answer_user(
                SimpleNamespace(),
                mcp,
                "test-model",
                [],
                {"add_itinerary_item"},
                messages,
                use_streaming=True,
                on_text=on_text,
            )

        self.assertEqual(answer, final_text)
        self.assertEqual(emitted, [final_text])
        mcp.call_tool.assert_awaited_once_with(
            "add_itinerary_item",
            {"plan_id": "trip-1", "day": 1},
        )
        assistant_messages = [
            message for message in messages if message.get("role") == "assistant"
        ]
        self.assertTrue(assistant_messages[0]["_internal"])
        self.assertFalse(assistant_messages[-1]["_internal"])


if __name__ == "__main__":
    unittest.main()
