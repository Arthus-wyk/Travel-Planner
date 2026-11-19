import asyncio
import unittest
from unittest.mock import AsyncMock

from agent.chat.review.parent import ReviewDelegations
from agent.chat.schemas.tasks import TaskContext, TaskResult


class NativeDelegationTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_search_with_a_known_gap_reports_partial(self):
        for remaining in ([], ["还需两个地点"]):
            with self.subTest(remaining=remaining):
                places = [{"place_id": "park"}]
                search = AsyncMock(return_value=TaskResult("completed", {"places": places, "remaining": remaining}))
                delegations = ReviewDelegations({"search_places": search}, TaskContext({}, None))
                result = await delegations.execute({
                    "task_id": "search", "role": "search_places", "task": "找三个公园",
                    "search_scope": {"category": "park", "day": None, "selection_count": 3},
                    "extra_data": {"facts": "", "artifact_refs": []},
                }, None)
                self.assertEqual(result.status, "partial")
                self.assertEqual(result.data["places"], places)
                self.assertEqual(result.error.details["missing"], remaining or ["还需补充 2 个候选。"])

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
        initial = await manager.execute(args("s", "search_places", "old"), None)
        inflight = asyncio.create_task(manager.execute(args("e", "build_edits", "edit", [initial.data["artifact_ref"]]), None))
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
        initial = await manager.execute(args("search", "search_places", "old"), None)
        await manager.execute(args("edit", "build_edits", "edit", [initial.data["artifact_ref"]]), None)
        revised = await manager.execute(args("search", "search_places", "new"), None)
        self.assertTrue(revised.success)
        self.assertEqual(revised.data["artifact_ref"], manager._reference("search"))
        self.assertEqual(manager.progress()["places"], [{"place_id": "new"}])
        self.assertNotIn("edit", manager.results)
        stale = await manager.execute(args("other", "build_edits", "edit", [initial.data["artifact_ref"]]), None)
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
        self.assertEqual(result.data["artifact_ref"], manager._reference("s"))

    async def test_cross_run_reference_cannot_resolve_to_same_named_task(self):
        handler = AsyncMock(return_value=TaskResult("completed", {"places": [{"place_id": "park"}]}))
        first = ReviewDelegations({"search_places": handler}, TaskContext({}, None))
        second = ReviewDelegations({"search_places": handler}, TaskContext({}, None))
        args = {"task_id": "park", "role": "search_places", "task": "park",
                "extra_data": {"facts": "", "artifact_refs": []}}
        old = await first.execute(args, None)
        current = await second.execute(args, None)
        self.assertNotEqual(old.data["artifact_ref"], current.data["artifact_ref"])
        dependent = {**args, "task_id": "dependent", "extra_data": {
            "facts": "", "artifact_refs": [old.data["artifact_ref"]]}}
        self.assertEqual((await second.execute(dependent, None)).error_type, "stale_artifact")
