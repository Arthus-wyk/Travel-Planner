"""Review continuation and semantic correction must retain real execution state."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests import test_review_agent_workflow as workflow_fixture
from agent.chat.review.parent import Clarification, ParentDecision
from agent.chat.review.workflow import ReviewRequest, ClarificationReply
from agent.chat.schemas.tasks import TaskResult
from service.travel.review_public_reply import format_review_reply


class ReviewExecutionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_answer_and_repeat_clarify_correct_with_live_mcp(self):
        for first, resume in (({}, None), ({"kind": "clarify", "question": "Again?", "options": ["A", "B"]},
            {"original_text": "Search", "clarification_answers": [{"question": "Where?", "answer": "Near park"}]})):
            fixture = workflow_fixture.ReviewWorkflowTest()
            handler = AsyncMock(return_value=TaskResult("completed", {"context": {"checked": True}}))
            result = await fixture.run_turn([first, fixture.delegate("audit", "audit_route"), {}],
                handlers={"audit_route": handler}, request=ReviewRequest("Search", resume_context=resume))
            self.assertEqual(result.status, "answer")
            handler.assert_awaited_once()
            self.assertFalse(fixture.writes)
            first_context = fixture.model_inputs[0]["extra_data"]["request_context"]
            self.assertEqual(first_context["execution_state"]["delegated_tasks_count"], 0)

    async def test_second_empty_answer_fails_without_unbounded_retry(self):
        fixture = workflow_fixture.ReviewWorkflowTest()
        result = await fixture.run_turn([{}, {}])
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(fixture.model_inputs), 2)

    async def test_pending_and_resume_merge_history_once_and_refresh_history(self):
        fixture = workflow_fixture.ReviewWorkflowTest()
        question = Clarification("When?", ("Morning", "Evening"))
        with patch("agent.chat.review.workflow._plan", new=AsyncMock(return_value=ParentDecision(
            "clarify", "Different message", clarification=question))) as plan:
            initial = await fixture.run_turn([], request=ReviewRequest("Search"))
            history = ({"role": "user", "content": "latest"},)
            resumed = await fixture.run_turn([], request=ReviewRequest("Search", initial.clarification,
                ClarificationReply(option_index=0), history=history, resume_context={
                    "original_text": "Search", "clarification_answers": [{"question": "When?", "answer": "Morning"}]}))
        self.assertEqual(resumed.message, question.question)
        self.assertEqual(resumed.clarification.answers, (("When?", "Morning"),))
        context = plan.call_args.args[0]
        self.assertEqual(context["conversation_history"], list(history))
        self.assertEqual(len(context["clarification_answers"]), 1)

    async def test_clarification_text_never_goes_to_public_model(self):
        with patch("service.travel.review_public_reply.request_model_with_retry", new=AsyncMock()) as model:
            text = await format_review_reply(None, "offline", None, {"状态": "clarify"}, "When?", set())
        self.assertEqual(text, "When?")
        model.assert_not_awaited()
