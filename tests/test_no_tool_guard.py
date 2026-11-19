import unittest
from agent.chat.prompts import is_empty_execution_promise
from service.chat import public_message_items
from agent.tools.registry import ToolRegistry
from agent.tools.schemas.inputs import ToolSpec


class InputAndGuardTests(unittest.TestCase):
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
