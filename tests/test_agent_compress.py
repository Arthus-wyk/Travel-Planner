import unittest

from agent.chat.runtime.compress import compress_messages


class CompressionTests(unittest.TestCase):
    def test_preserves_role_task_updates_and_complete_native_tool_groups(self):
        messages = [{"role": "system", "content": "role"},
                    {"role": "user", "content": '{"task":"initial"}'},
                    {"role": "assistant", "content": "old answer"},
                    {"role": "user", "content": '{"task":"updated"}'},
                    {"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "b"}]},
                    {"role": "tool", "tool_call_id": "a", "content": "first"},
                    {"role": "tool", "tool_call_id": "b", "content": "second"}]
        result = compress_messages(messages, keep_recent=1)
        self.assertIn(messages[0], result)
        self.assertIn(messages[1], result)
        self.assertIn(messages[3], result)
        self.assertEqual(result[-3:], messages[-3:])
        self.assertNotIn(messages[2], result)

    def test_truncates_tool_content_without_mutating_original(self):
        messages = [{"role": "assistant", "tool_calls": [{"id": "a"}]},
                    {"role": "tool", "tool_call_id": "a", "content": "x" * 4000}]
        result = compress_messages(messages, max_tool_result_chars=100)
        self.assertLess(len(result[-1]["content"]), 200)
        self.assertEqual(len(messages[-1]["content"]), 4000)
