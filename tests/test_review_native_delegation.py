import asyncio
import unittest
from unittest.mock import AsyncMock

from agent.chat.review_parent import ReviewDelegations
from agent.chat.review_tasks import TaskContext, TaskResult


class NativeDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_inflight_proposal_cannot_restore_invalidated_dependency(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(task, dependencies, context):
            if task.kind == "build_edits":
                started.set()
                await release.wait()
            return TaskResult("completed", {"places": [{"place_id": task.input["goal"]}]})
        manager = ReviewDelegations({"search_places": handler, "build_edits": handler}, TaskContext({}, AsyncMock()))
        def args(key, role, task, refs=()):
            return {"task_id": key, "role": role, "task": task,
                    "extra_data": {"facts": "", "artifact_refs": list(refs)}}
        await manager.execute(args("s", "search_places", "old"), None)
        inflight = asyncio.create_task(manager.execute(args("e", "build_edits", "edit", ["s:v1"]), None))
        await started.wait()
        await manager.execute(args("s", "search_places", "new"), None)
        release.set()
        result = await inflight
        self.assertFalse(result.success)
        self.assertEqual(result.error_type, "stale_artifact")
        self.assertNotIn("e", manager.results)

    async def test_revision_invalidates_previous_places_and_dependent_proposal(self):
        async def handler(task, dependencies, context):
            if task.kind == "search_places":
                return TaskResult("completed", {"places": [{"place_id": task.input["goal"]}]})
            return TaskResult("completed", {"kind": "changes", "operations": [{"old": True}]})

        manager = ReviewDelegations({"search_places": handler, "build_edits": handler}, TaskContext({}, AsyncMock()))
        def args(key, role, goal, refs=()):
            return {"task_id": key, "role": role, "task": goal,
                    "extra_data": {"facts": "", "artifact_refs": list(refs)}}
        await manager.execute(args("search", "search_places", "old"), None)
        await manager.execute(args("edit", "build_edits", "edit", ["search:v1"]), None)
        await manager.execute(args("search", "search_places", "new"), None)
        self.assertEqual(manager.progress()["places"], [{"place_id": "new"}])
        self.assertNotIn("edit", manager.results)
        stale = await manager.execute(args("other", "build_edits", "edit", ["search:v1"]), None)
        self.assertFalse(stale.success)
        self.assertEqual(stale.error_type, "stale_artifact")
        self.assertEqual(len(manager.history), 3)

    async def test_parent_feedback_is_compact(self):
        async def handler(*args):
            return TaskResult("completed", {"places": [{"place_id": "p", "name": "公园", "raw": "x" * 1000}],
                                            "context": {"large": "x" * 1000}, "routes": [{"duration": 600}]})
        manager = ReviewDelegations({"search_places": handler}, TaskContext({}, AsyncMock()))
        result = await manager.execute({"task_id": "s", "role": "search_places", "task": "公园",
                                        "extra_data": {"facts": "", "artifact_refs": []}}, None)
        self.assertNotIn("context", result.data)
        self.assertNotIn("raw", result.data["places"][0])
        self.assertEqual(result.data["artifact_ref"], "s:v1")
