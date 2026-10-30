import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from service.storage import memory_store


class MemoryStoreTests(unittest.TestCase):
    def test_mid_term_roundtrip_preserves_jsonl_format_and_plan(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                memory_store, "MID_TERM_PATH", Path(directory) / "memory.jsonl"
            ),
        ):
            count = memory_store.append_mid_term(
                [
                    {"content": "住市中心", "type": "constraint"},
                    {"content": "周日出发", "type": "fact"},
                ],
                plan_id="trip-1",
            )
            self.assertEqual(count, 2)
            self.assertEqual(
                [m["content"] for m in memory_store.read_mid_term()],
                ["住市中心", "周日出发"],
            )
            self.assertEqual(memory_store.read_mid_term()[0]["plan_id"], "trip-1")

    def test_existing_markdown_profile_is_preserved_on_append(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.md"
            memory_store.update_preference_profile({"hotel": ["安静"]}, path=path)
            memory_store.update_preference_profile({"pace": ["慢游"]}, path=path)
            profile = memory_store.read_preference_profile(path=path)["profile"]
            self.assertEqual(profile["hotel"], ["安静"])
            self.assertEqual(profile["pace"], ["慢游"])
