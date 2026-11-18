import asyncio
import json
import unittest
from collections import Counter
from types import SimpleNamespace
from unittest.mock import patch

from agent.chat.review_tasks import TaskResult
from agent.chat.review_workflow import ClarificationReply, ReviewContext, ReviewRequest, run_review_turn
from tooling.context import ToolContext
from tooling.registry import ToolRegistry
from tooling.spec import ToolSpec
from service.travel.review import save_review_action
from service.storage.planning_jobs import PlanConflict
from service.travel.review_interaction import ReviewInteractions
from tools.review import _best_place_insertion


class ReviewWorkflowTest(unittest.IsolatedAsyncioTestCase):
    async def run_turn(self, decisions, *, handlers=None, save=False, allowed=False,
                       version="v1", timeout=5, request=None, preview_accepted=True):
        self.writes, self.visible, self.model_inputs = [], [], []
        responses = iter(decisions)
        snapshot = {"plan_version": "v1", "stage": "review"}

        async def fake_model(**kwargs):
            self.visible.extend(tool["function"]["name"] for tool in kwargs["tools"])
            self.model_inputs.append(json.loads(kwargs["messages"][1]["content"]))
            decision = next(responses)
            if decision is None:
                await asyncio.Event().wait()
            calls = decision.get("calls", [])
            content = {"kind": "answer", "message": "审核结果", "question": None,
                       "options": [], "candidate_groups": [], **decision}
            response = SimpleNamespace(message=SimpleNamespace(
                content="" if calls else json.dumps(content), tool_calls=calls))
            return response, kwargs["messages"], None

        async def read(args, context):
            return snapshot

        async def write(args, context):
            self.writes.append(args)
            return {**snapshot, "plan_version": "v2"}

        async def preview(args, context):
            return {**snapshot, "accepted": preview_accepted}

        def registry(plan_id, *, writable=False):
            specs = [ToolSpec("get_review_context", "", {"type": "object"}, read),
                     ToolSpec("preview_review_changes", "", {"type": "object"}, preview)]
            if writable:
                specs.append(ToolSpec("apply_review_changes", "", {
                    "properties": {"operations": {"type": "array"}}}, write,
                    required_permission="review:write"))
            return ToolRegistry(specs)

        async def rewrite(**kwargs):
            return {"original_text": kwargs["text"], "rewritten_text": kwargs["text"],
                    "references": [], "unresolved_references": []}

        context = ReviewContext("plan", version, None, "mock", save_requested=save,
            tool_context=ToolContext(permissions=frozenset({"review:write"} if allowed else ())),
            handlers=handlers or {}, timeout_seconds=timeout)
        with patch("agent.chat.review_workflow.review_registry", registry), patch(
            "agent.chat.nodes.llm.request_model_with_retry", fake_model
        ), patch("agent.chat.review_workflow.rewrite_request", rewrite):
            return await run_review_turn(request or ReviewRequest("审核当前路线"), context)

    @staticmethod
    def delegate(key="edit", role="build_edits", task="调整行程", refs=(), retry=None):
        args = {"task_id": key, "role": role, "task": task,
                "extra_data": {"facts": "", "artifact_refs": list(refs)}}
        if retry:
            args["_retry_of"] = retry
        return {"calls": [{"function": {"name": "delegate_task", "arguments": args}}]}

    async def test_clarification_pauses_and_resumes_with_selected_or_custom_answer(self):
        message, question = "把博物馆延后", "博物馆要延到什么时候？"
        result = await self.run_turn([{"kind": "clarify", "question": question,
                                      "options": ["延后一小时", "移到当天最后"]}], request=ReviewRequest(message))
        self.assertEqual(result.status, "clarify")
        self.assertTrue(result.clarification.prompt.allow_custom)
        self.assertFalse(self.writes)
        for reply, answer in ((ClarificationReply(option_index=0), "延后一小时"),
                              (ClarificationReply(custom_text="当天 16:00"), "当天 16:00")):
            resumed = await self.run_turn([{}], request=ReviewRequest(message, result.clarification, reply))
            self.assertEqual(resumed.status, "answer")
            evidence = self.model_inputs[0]["extra_data"]["request_context"]
            self.assertEqual(evidence["rewrite"]["original_text"], message)
            self.assertEqual(evidence["clarification_answers"], [{"question": question, "answer": answer}])
            self.assertNotIn("semantic", evidence)

    async def test_answer_proposal_and_single_authorized_commit(self):
        operations = [{"action": "remove_activity", "payload": {"item_id": "A"}}]
        async def handler(*args):
            return TaskResult("completed", {"kind": "changes", "operations": operations})
        for save, allowed, expected in ((False, True, "proposal"), (True, False, "failed"), (True, True, "saved")):
            result = await self.run_turn([self.delegate(), {"kind": "changes"}],
                handlers={"build_edits": handler}, save=save, allowed=allowed)
            self.assertEqual(result.status, expected)
            self.assertEqual(len(self.writes), int(expected == "saved"))
            self.assertNotIn("apply_review_changes", self.visible)
        result = await self.run_turn([{}])
        self.assertEqual(result.status, "answer")
        self.assertFalse(self.writes)

    async def test_unverified_parent_operations_never_save(self):
        result = await self.run_turn([{"kind": "changes", "operations": []}], save=True, allowed=True)
        self.assertEqual(result.status, "failed")
        self.assertFalse(self.writes)

    async def test_old_version_and_model_timeout(self):
        result = await self.run_turn([], version="stale")
        self.assertEqual((result.status, result.plan_version), ("conflict", "v1"))
        result = await self.run_turn([None], timeout=0.01)
        self.assertEqual(result.status, "failed")
        self.assertFalse(self.writes)

    async def test_trial_rejection_preserves_verified_evidence(self):
        operations = [{"action": "remove_activity", "payload": {"item_id": "A"}}]
        place = {"place_id": "park", "name": "公园"}
        async def handler(*args):
            return TaskResult("completed", {"kind": "changes", "operations": operations,
                                           "places": [place], "routes": [{"duration": "600s"}]})
        result = await self.run_turn([self.delegate(), {"kind": "changes"}],
            handlers={"build_edits": handler}, preview_accepted=False)
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.progress["routes"], [{"duration": "600s"}])
        self.assertEqual(result.places, [place])
        self.assertTrue(result.progress["remaining"])

    async def test_invalid_native_delegation_runs_no_handlers(self):
        async def handler(*args):
            self.fail("invalid delegation reached handler")
        invalid = self.delegate(role="unknown")
        result = await self.run_turn([invalid, {}], handlers={"audit_route": handler})
        self.assertEqual(result.status, "failed")
        self.assertFalse(self.writes)

    async def test_replan_reuses_independent_work_and_saves_current_proposal(self):
        counts = Counter()
        async def handler(task, dependencies, context):
            counts[task.id] += 1
            if task.id == "museum":
                return TaskResult("completed", {"poi_id": task.input["goal"]})
            if task.id == "station":
                return TaskResult("completed")
            self.assertEqual(set(dependencies), {"museum", "station"})
            return TaskResult("completed", {"kind": "changes", "operations": [
                {"action": "review_place", "payload": {"poi_id": dependencies["museum"].data["poi_id"]}}]})
        first = self.delegate("museum", "search_places", "closed")
        station = self.delegate("station", "audit_route", "核对交通")
        first["calls"].extend(station["calls"])
        result = await self.run_turn([first, self.delegate("museum", "search_places", "alternative"),
            station, self.delegate(refs=("museum:v2", "station:v1")), {"kind": "changes"}],
            handlers={role: handler for role in ("search_places", "audit_route", "build_edits")},
            save=True, allowed=True)
        self.assertEqual(result.status, "saved")
        self.assertEqual(counts, {"museum": 2, "station": 1, "edit": 1})
        self.assertEqual(self.writes[0]["operations"][0]["payload"]["poi_id"], "alternative")


class ReviewAcceptanceTest(unittest.TestCase):
    def test_new_high_issue_prevents_persistence(self):
        original = {"plan_id": "plan"}
        issue = {"type": "time_conflict", "severity": "high", "target": 1}
        outcome = {"plan": original, "audit": {"issues": [issue]}}
        with patch("service.travel.review.preview_review_action", return_value=outcome), patch(
            "service.travel.review.audit_plan", return_value={"issues": []}
        ), patch("service.travel.review.planning_jobs.save_review_changes") as save:
            result = save_review_action(original, "review_changes", {}, require_acceptance=True)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["blocking_issues"], [issue])
        save.assert_not_called()


class ReviewInteractionTest(unittest.IsolatedAsyncioTestCase):
    """验证候选点击会恢复到同一份草稿并只消费一次。"""

    async def test_place_card_resumes_original_request_on_draft(self):
        """用模拟 Agent 验证动作绑定、续跑和草稿增量。"""
        interactions = ReviewInteractions()
        draft = {"planning": {"stage": "review"}, "itinerary": [{"item_id": "pending-edit"}]}
        payload = {"message": "添加博物馆", "plan_version": "v1", "draft_revision": 3,
                   "operations": [{"action": "update_budget", "payload": {"budget": 500}}]}
        operations = [{"action": "review_place", "payload": {"poi_id": "poi"}}]
        search = SimpleNamespace(status="answer", message="请选择", clarification=None,
                                 places=[{"place_id": "poi", "name": "博物馆"}], snapshot=None, progress={}, candidate_groups=[{
                                     "id": "museum", "label": "博物馆", "place_ids": ["poi"], "selection_count": 1}])
        proposal = SimpleNamespace(status="proposal", message="可加入", clarification=None, places=[],
                                   operations=operations, snapshot={"draft_plan": {"itinerary": []}, "audit": {}}, progress={}, candidate_groups=[])
        with patch("service.travel.review_interaction._load_draft", return_value=draft), patch(
            "service.travel.review_interaction.create_model_client"
        ), patch("service.travel.review_interaction.run_review_turn", side_effect=[search, proposal]) as run:
            first = await interactions.run("plan", "mock", payload)
            action = first["blocks"][0]["action_id"]
            with self.assertRaises(PlanConflict):
                await interactions.run("plan", "mock", {**payload, "action_id": action, "draft_revision": 4})
            updated = await interactions.run("plan", "mock", {**payload, "action_id": action, "option_indexes": [0]})
            request, context = run.call_args.args
            self.assertEqual(request.message, "添加博物馆")
            self.assertEqual(request.selection["places"][0]["place_id"], "poi")
            self.assertEqual(context.draft["plan"], draft)
            self.assertFalse(context.save_requested)
            self.assertEqual((updated["status"], updated["operations"]), ("draft_updated", operations))
            with self.assertRaises(PlanConflict):
                await interactions.run("plan", "mock", {**payload, "action_id": action})

    def test_insertion_chooses_shortest_accepted_candidate(self):
        """验证试算会排除未通过审核的锚点。"""
        plan = {"itinerary": [{"item_id": key, "day": 1} for key in ("A", "B", "C")]}
        payload = {"operations": [{"action": "review_place", "payload": {"item_id": "A", "poi_id": "poi"}}]}

        def trial(original, action, args):
            """按锚点构造可比较的试算结果。"""
            key = args["operations"][0]["payload"]["item_id"]
            minutes = {"A": 20, "B": 10, "C": 2}[key]
            return {"plan": {"routes": [{"duration_minutes": minutes}]}, "accepted": key != "C"}

        with patch("tools.review.preview_review_action", side_effect=trial), patch(
            "tools.review.review_acceptance", side_effect=lambda original, result: result
        ):
            _, selected = _best_place_insertion(plan, payload, "poi")
        self.assertEqual(selected["operations"][0]["payload"]["item_id"], "B")
