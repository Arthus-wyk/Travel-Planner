"""Checkpoint ownership survives cancellation without repeating a started save."""

import asyncio
import threading
import unittest
from unittest.mock import AsyncMock, patch

from agent.chat.review_execution import ReviewExecution
from service.travel import review_interaction as review


class ReviewCheckpointCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_before_save_releases_claim(self):
        status = {"value": "paused"}

        def claim(*_):
            status["value"] = "running"
            return "paused", {}

        def finish(*_):
            status["value"] = "paused"

        with patch.object(review.review_checkpoints, "claim", side_effect=claim), \
                patch.object(review.review_checkpoints, "finish", side_effect=finish), \
                patch.object(review, "_read_review_plan", return_value={}), \
                patch.object(review, "create_model_client"), \
                patch.object(review.ReviewInteractions, "_continue_checkpoint", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await review.ReviewInteractions().run("plan", "model", {
                    "conversation_id": "chat", "draft_revision": 0, "action_id": "a" * 32,
                })
        self.assertEqual(status["value"], "paused")

    async def test_cancellation_during_claim_waits_then_releases(self):
        started, release, claimed = threading.Event(), threading.Event(), threading.Event()
        status = {"value": "paused"}

        def claim(*_):
            started.set()
            release.wait(5)
            status["value"] = "running"
            claimed.set()
            return "paused", {}

        def finish(*_):
            status["value"] = "paused"

        with patch.object(review.review_checkpoints, "claim", side_effect=claim), \
                patch.object(review.review_checkpoints, "finish", side_effect=finish):
            task = asyncio.create_task(review.ReviewInteractions().run("plan", "model", {
                "conversation_id": "chat", "draft_revision": 0, "action_id": "a" * 32,
            }))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            await asyncio.sleep(0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(await asyncio.to_thread(claimed.wait, 5))
        self.assertEqual(status["value"], "paused")

    async def test_cancellation_after_save_starts_waits_for_atomic_completion(self):
        started, release = threading.Event(), threading.Event()
        status = {"value": "running"}

        def save(*_, **__):
            started.set()
            release.wait(5)
            status["value"] = "completed"
            return {"status": "saved"}

        with patch.object(review.planning_jobs, "save_review_changes", side_effect=save):
            task = asyncio.create_task(review._commit_checkpoint(
                {"plan_id": "plan"}, "a" * 32,
                {"plan": {}, "audit": {}, "maps": {}}, ReviewExecution(),
            ))
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            task.cancel()
            asyncio.get_running_loop().call_later(0.2, release.set)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(status["value"], "completed")
