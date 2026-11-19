"""从 HTTP 模拟审核页交互，使用离线模型、高德和存储替身。"""

import asyncio
import json
import unittest
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from infrastructure.llm import request_model_with_retry as real_model_request

from service.api.server import create_app
from service.storage.planning_jobs import fingerprint
from service.storage.planning_jobs import PlanConflict
from service.travel.planning import create_draft


PLACE = {"place_id": "museum", "name": "测试博物馆", "address": "杭州测试路",
         "longitude": 120.2, "latitude": 30.2}
SECOND_PLACE = {**PLACE, "place_id": "gallery", "name": "测试美术馆", "longitude": 120.25}
ANCHOR = {"item_id": "guided-1-park", "reference_id": "park", "day": 1,
          "category": "attraction", "title": "测试公园", "location": "杭州公园路",
          "time_of_day": "09:00", "duration_minutes": 60, "estimated_cost": 0, "day_offset": 0,
          "place": {**PLACE, "place_id": "park", "name": "测试公园", "longitude": 120.1}}


class ReviewAssistantHttpTest(unittest.TestCase):
    """使用与前端相同的请求体访问真实审核助手和保存路由。"""

    def setUp(self):
        """加载单日行程，只替换模型、持久化和地图外部请求。"""
        self.plan = create_draft({
            "destination": "杭州", "departure_city": "杭州", "start_date": "2026-11-17",
            "trip_days": 1, "travelers": 1, "budget": 1000,
        }, "http-review")
        self.plan["planning"].update(
            stage="review", day_departures={"1": "09:00"},
            selected_transport={"outbound": "out", "return": "back"},
            transport_candidates=[{"id": "out", "arrive_time": "07:00", "direction": "outbound"},
                                  {"id": "back", "arrive_time": "20:00", "direction": "return"}],
        )
        self.plan["itinerary"] = [deepcopy(ANCHOR)]
        self.responses = deque()
        self.inputs = []
        self.tool_sets = []
        self.tool_results = {}
        self.checkpoints = {}
        self.plan_reads = 0
        self.path = "/api/plans/http-review/planning"
        self.payload = {"plan_version": fingerprint(self.plan), "draft_revision": 1,
                        "operations": [{"action": "update_budget", "payload": {"budget": 900}}]}
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.log_root = Path(stack.enter_context(TemporaryDirectory()))
        stack.enter_context(patch("service.utils.log_paths.PROJECTS_DIR", self.log_root))
        for target in ("service.api.server.get_plan", "service.travel.review_interaction.get_plan",
                       "service.travel.review.get_plan"):
            stack.enter_context(patch(target, side_effect=self._load_plan))
        self.save = stack.enter_context(patch("service.storage.planning_jobs.save_review_changes",
                                             side_effect=self._save_plan))
        stack.enter_context(patch("service.travel.review_interaction.create_model_client"))
        stack.enter_context(patch("infrastructure.llm.request_model_with_retry", self._model))
        stack.enter_context(patch("agent.chat.review.workflow.rewrite_request", self._rewrite))
        stack.enter_context(patch("agent.tools.implementations.review.amap_search_places", return_value={
            "success": True, "places": [PLACE, SECOND_PLACE],
        }))
        stack.enter_context(patch("service.travel.map_planning.request_amap", side_effect=AssertionError("意外地图网络调用")))
        stack.enter_context(patch("agent.planning.implementations.routes.amap_compute_route", side_effect=self._route))
        stack.enter_context(patch("service.travel.map_planning.lookup_review_place",
                                  side_effect=lambda poi: {"museum": PLACE, "gallery": SECOND_PLACE}[poi]))
        stack.enter_context(patch("service.storage.review_checkpoints.offer", side_effect=self._offer))
        stack.enter_context(patch("service.storage.review_checkpoints.claim", side_effect=self._claim))
        stack.enter_context(patch("service.storage.review_checkpoints.finish", side_effect=self._finish))
        stack.enter_context(patch("service.travel.review_public_reply.request_model_with_retry", self._public_model))
        self.client = stack.enter_context(TestClient(create_app(Path("mcp_server.py"), "offline")))

    def _load_plan(self, plan_id):
        """读取独立副本，防止试算通过对象引用修改库存。"""
        self.assertEqual(plan_id, self.plan["plan_id"])
        self.plan_reads += 1
        return deepcopy(self.plan), None

    def _save_plan(self, plan_id, original, candidate, *, checkpoint_id=None, response=None):
        """模拟按版本保存并保留最终结果。"""
        self.assertEqual(original, self.plan)
        self.plan = deepcopy(candidate)
        if checkpoint_id:
            result = {**response, "plan": self.plan, "plan_version": fingerprint(self.plan)}
            self._finish(checkpoint_id, result)
            return result

    def _offer(self, key, plan_id, conversation, state):
        """模拟服务端持久化一条暂停记录。"""
        self.checkpoints[key] = ((plan_id, conversation), "paused", deepcopy(state))

    def _claim(self, key, plan_id, conversation):
        """模拟仅对应会话可认领，已经完成的响应可以重放。"""
        scope, status, state = self.checkpoints[key]
        if scope != (plan_id, conversation) or status == "running":
            raise PlanConflict("选择已失效")
        if status == "paused":
            self.checkpoints[key] = (scope, "running", state)
        return status, deepcopy(state)

    def _finish(self, key, result=None):
        """写入完成记录，或保留原暂停内容供更正。"""
        scope, _, state = self.checkpoints[key]
        self.checkpoints[key] = (scope, "completed" if result is not None else "paused",
                                 deepcopy(result) if result is not None else state)

    async def _public_model(self, **kwargs):
        """模拟回复模型复述内部字段，验证出站兜底。"""
        return SimpleNamespace(message=SimpleNamespace(
            content="子Agent 已处理 item_id guided-1-park", tool_calls=[]
        )), kwargs["messages"], None

    @staticmethod
    def _route(*args, **kwargs):
        """给真实路线和排程流程提供固定耗时与折线。"""
        return {"success": True, "duration": "600s", "fare": 0,
                "legs": [{"steps": [{"polyline": "120.1,30.2;120.2,30.2"}]}]}

    async def _model(self, **kwargs):
        """旧业务场景数据在测试边界转换为共用循环的原生调用和严格结果。"""
        self.inputs.append(json.loads(kwargs["messages"][1]["content"])["extra_data"])
        self.tool_sets.append({tool["function"]["name"] for tool in kwargs["tools"]})
        for entry in kwargs["messages"]:
            if entry["role"] == "tool":
                self.tool_results[entry["tool_name"]] = json.loads(entry["content"])
        reply = self.responses.popleft()
        while reply.get("kind") == "ready":
            reply = self.responses.popleft()
        if (kwargs.get("stage") == "review_parent" and reply.get("kind") == "answer"
                and not any(entry.get("tool_name") == "delegate_task" for entry in kwargs["messages"])):
            self.responses.appendleft(reply)
            self.responses.appendleft({"status": "completed", "data": {"message": "已核对当前行程", "remaining": []}})
            reply = {"kind": "tasks", "tasks": [{"id": "audit", "kind": "audit_route", "input": {}}]}
        if reply.get("kind") == "tasks":
            reply = {"calls": [{"function": {"name": "delegate_task", "arguments": {
                "task_id": task["id"], "role": task["kind"],
                "task": task["input"].get("question", "处理当前审核任务"),
                **({"search_scope": {"category": "attraction", "day": None, "selection_count": task["input"].get("selection_count", 1)}}
                   if task["kind"] == "search_places" else {}),
                "extra_data": {"facts": json.dumps(task["input"], ensure_ascii=False), "artifact_refs": []},
            }}} for task in reply["tasks"]]}
        if (kwargs.get("stage") == "build_edits" and reply.get("status") == "completed"
                and not any(item.get("tool_name") == "preview_review_changes" for item in kwargs["messages"])):
            self.responses.appendleft(reply)
            reply = {"calls": [{"function": {"name": "preview_review_changes", "arguments": {
                "plan_version": self.inputs[-1]["snapshot"]["plan_version"],
                "operations": reply["data"]["operations"],
            }}}]}
        if reply.get("kind") == "answer" and "candidate_groups" not in reply:
            places = self.tool_results.get("delegate_task", {}).get("data", {}).get("places", [])
            if places:
                reply = {**reply, "candidate_groups": [{
                    "id": "places", "label": "地点", "place_ids": [place["place_id"] for place in places],
                    "selection_count": 1,
                }]}
        if not reply.get("calls"):
            reply = self._strict_result(reply, kwargs["response_format"])
        content = json.dumps(reply.get("content", reply), ensure_ascii=False)
        return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=reply.get("calls", []))), kwargs["messages"], None

    @classmethod
    def _strict_result(cls, value, schema):
        """补齐测试数据的严格输出字段，不替生产解析器修正结果。"""
        if value is None:
            if "null" in schema.get("type", []) or any(branch.get("type") == "null" for branch in schema.get("anyOf", [])):
                return None
            value = {"object": {}, "array": [], "string": ""}.get(schema.get("type"))
        if schema.get("type") == "object":
            return {key: cls._strict_result(value.get(key), child) for key, child in schema["properties"].items()}
        if schema.get("type") == "array":
            return [cls._strict_result(item, schema["items"]) for item in value]
        for branch in schema.get("anyOf", []):
            action = value.get("action") if isinstance(value, dict) else None
            if action and action not in branch.get("properties", {}).get("action", {}).get("enum", []):
                continue
            if branch.get("type") != "null":
                return cls._strict_result(value, branch)
        return value

    async def _rewrite(self, **kwargs):
        from service.utils.runtime_log import record_event

        result = {"original_text": kwargs["text"], "rewritten_text": kwargs["text"],
                  "references": [], "unresolved_references": []}
        record_event("request_rewrite", result)
        return result

    def _turn(self, *, status=200, **fields):
        """发送前端助手 POST 并检查状态码。"""
        response = self.client.post(f"{self.path}/assistant", json={**self.payload, **fields})
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def _choose_first(self, reply):
        """模拟前端点按首个可操作内容块及其第一个选项。"""
        for block in reply["blocks"]:
            if block["type"] == "choice":
                return self._turn(action_id=block["action_id"], option_index=0)
            if block["type"] == "place_candidates" and block["places"]:
                indexes = [index for index, place in enumerate(block["places"]) if place["place_id"] in {
                    key for group in block["groups"] for key in group["place_ids"][:group["selection_count"] or 1]}]
                return self._turn(action_id=block["action_id"], option_indexes=indexes)
            if block["type"] == "change_preview":
                return self._turn(action_id=block["action_id"])
        self.fail(f"没有可点击的交互内容：{reply}")

    def test_partial_candidates_and_routes_survive_both_http_entry_modes(self):
        from agent.chat.review.workflow import ReviewTurnResult
        from service.runtime_logs import read_run

        for extra in ({}, {"conversation_id": "partial-chat"}):
            result = ReviewTurnResult("partial", "审核尚未完成", self.payload["plan_version"], places=[PLACE],
                progress={"places": [PLACE], "routes": [{"duration": "600s"}], "remaining": ["补齐餐厅"], "failures": []})
            with self.subTest(extra=extra), patch("service.travel.review_interaction.run_review_turn", return_value=result):
                reply = self._turn(message="请找两个景点和餐厅", **extra)
            self.assertEqual(reply["status"], "partial")
            self.assertEqual(reply["progress"]["routes"], [{"duration": "600s"}])
            self.assertEqual(reply["progress"]["remaining"], ["补齐餐厅"])
            self.assertEqual(reply["blocks"][0]["places"][0]["place_id"], "museum")
            self.assertNotIn("action_id", reply["blocks"][0])
            self.assertNotIn("action_id", reply["blocks"][0]["places"][0])
            log = read_run(Path.cwd(), reply["run_id"])
            self.assertEqual(log["run"]["status"], "failed")

    def test_search_click_preview_refresh_and_save(self):
        """地点卡片续跑、草稿地图刷新、版本冲突与最终保存。"""
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "search", "kind": "search_places", "input": {}, "depends_on": []}]},
            {"calls": [{"function": {"name": "search_review_places", "arguments": {"query": "博物馆"}}}]},
            {"status": "completed", "data": {"place_ids": ["museum", "gallery"]}},
            {"kind": "answer", "message": "请选择一个地点"},
        ])
        reply = self._turn(message="帮我添加一家博物馆")
        block = reply["blocks"][0]
        self.assertEqual(block["type"], "place_candidates")
        card = block["places"][0]
        self.assertEqual((card["name"], card["place_id"]), ("测试博物馆", "museum"))
        action = reply["blocks"][0]["action_id"]
        self._turn(status=409, action_id=action, draft_revision=2)
        self._turn(status=409, action_id=action, operations=[])
        operations = [{"action": "review_place", "payload": {
            "item_id": ANCHOR["item_id"], "poi_id": "museum", "category": "attraction", "duration_minutes": 60,
        }}]
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "edit", "kind": "build_edits", "input": {}, "depends_on": []}]},
            {"status": "completed", "data": {"kind": "changes", "operations": operations}},
            {"kind": "changes"},
        ])
        updated = self._choose_first(reply)
        self._turn(status=409, action_id=action)
        self.assertIn("museum", {entry["request_context"]["selection"]["places"][0]["place_id"]
                                 for entry in self.inputs if entry.get("request_context", {}).get("selection")})
        self.assertEqual(updated["status"], "draft_updated", updated)
        self.assertEqual(updated["plan"]["budget"], 900)
        self.assertIn("测试博物馆", {point["title"] for point in updated["maps"]["1"]["points"]})
        titles = {item["title"] for item in updated["plan"]["itinerary"]}
        self.assertIn("测试博物馆", titles)
        self.assertNotIn("测试美术馆", titles)
        self.save.assert_not_called()
        saved = self.client.post(f"{self.path}/review_changes", json={
            "plan_version": self.payload["plan_version"],
            "operations": [*self.payload["operations"], *updated["operations"]],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotEqual(saved.json()["plan_version"], self.payload["plan_version"])
        self.save.assert_called_once()
        self.assertFalse(self.responses)

    def test_clarification_custom_reply_resumes_original_request(self):
        """自定义回复合入意图上下文，并通过同一 HTTP 入口继续。"""
        self.responses.append({"kind": "clarify", "question": "延后到几点？", "options": ["10:00", "11:00"]})
        reply = self._turn(message="把公园延后")
        block = reply["blocks"][0]
        self.assertEqual((block["type"], block["options"], block["allow_custom"]), ("choice", ["10:00", "11:00"], True))
        self.responses.extend([{"kind": "ready"}, {"kind": "answer", "message": "收到，延后到 12:00。"}])
        resumed = self._turn(action_id=block["action_id"], custom_text="12:00")
        self.assertEqual(resumed["message"], "收到，延后到 12:00。")
        self.assertEqual(self.inputs[-1]["request_context"]["clarification_answers"],
                         [{"question": "延后到几点？", "answer": "12:00"}])
        self.save.assert_not_called()

    def test_invalid_and_stale_requests_do_not_call_model(self):
        """无效草稿和旧数据库版本在请求模型前被拒绝。"""
        self._turn(status=400, message="检查路线", draft_revision=-1)
        self._turn(status=409, message="检查路线", plan_version="stale")
        self.assertFalse(self.inputs)
        self.save.assert_not_called()

    def test_run_id_links_to_model_transcript(self):
        """审核回复和模型输出归到同一运行记录。"""
        from service.runtime_logs import read_run
        from agent.chat.review.workflow import ReviewContext
        from agent.chat.runtime.rewrite import rewrite_request

        class Model:
            """模拟传输对象，保留真实模型日志记录逻辑。"""

            def __init__(self, slow=False):
                """配置是否模拟慢速父智能体。"""
                self.slow = slow

            async def chat(self, **kwargs):
                """根据阶段返回公开 JSON 输出。"""
                is_rewrite = "original_text" in kwargs.get("format", {}).get("properties", {})
                if self.slow and not is_rewrite:
                    await asyncio.Event().wait()
                text = json.loads(kwargs["messages"][-1]["content"])["original_text"] if is_rewrite else ""
                has_tools = any(message["role"] == "tool" for message in kwargs["messages"])
                is_child = "status" in kwargs.get("format", {}).get("properties", {})
                if not is_rewrite and not has_tools:
                    name = "get_review_context" if is_child else "delegate_task"
                    arguments = {} if is_child else {"task_id": "audit", "role": "audit_route",
                        "task": "核对当前路线", "extra_data": {"facts": "", "artifact_refs": []}}
                    return SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[{
                        "function": {"name": name, "arguments": arguments}}]))
                value = ({"original_text": text, "rewritten_text": text, "references": [], "unresolved_references": []}
                         if is_rewrite else {"status": "completed", "reason": "", "data": {
                             "message": "已核对当前路线", "remaining": []}} if is_child else
                         {"kind": "answer", "message": "路线已审核", "question": None,
                                             "options": [], "candidate_groups": []})
                content = json.dumps(value, ensure_ascii=False)
                return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))

        with patch("service.travel.review_interaction.create_model_client", return_value=Model()), patch(
            "infrastructure.llm.request_model_with_retry", real_model_request
        ), patch("agent.chat.review.workflow.rewrite_request", rewrite_request):
            reply = self._turn(message="第一天想看看路线")
        log = read_run(Path.cwd(), reply["run_id"])
        self.assertEqual(log["run"]["status"], "completed")
        responses = [event for event in log["events"] if event["type"] == "model_response"]
        self.assertEqual([event["stage"] for event in responses],
                         ["request_rewrite", "review_parent", "audit_route", "audit_route", "review_parent"])
        self.assertEqual(json.loads(responses[-1]["content"])["message"], "路线已审核")
        rewrites = [event for event in log["events"] if event["type"] == "request_rewrite"]
        self.assertEqual(rewrites[0]["original_text"], "第一天想看看路线")
        self.assertFalse(any(event["type"] == "intent_understanding" for event in log["events"]))
        planning = next(event for event in log["events"] if event["type"] == "model_request"
                        and event["stage"] == "review_parent")
        payload = json.loads(planning["messages"][1]["content"])["extra_data"]["request_context"]
        self.assertEqual(payload["original_text"], "第一天想看看路线")
        self.assertNotIn("rewritten_text", payload["rewrite"])

        with patch("service.travel.review_interaction.create_model_client", return_value=Model(slow=True)), patch(
            "infrastructure.llm.request_model_with_retry", real_model_request
        ), patch("agent.chat.review.workflow.rewrite_request", rewrite_request), patch("service.travel.review_interaction.ReviewContext",
                 side_effect=lambda *args, **kwargs: ReviewContext(*args, timeout_seconds=0.2, **kwargs)):
            timed_out = self._turn(message="再检查一次路线")
        log = read_run(Path.cwd(), timed_out["run_id"])
        self.assertEqual(log["run"]["status"], "failed")
        self.assertIn(("model_response", "request_rewrite"), [(e["type"], e.get("stage")) for e in log["events"]])
        self.assertIn(("model_cancelled", "review_parent"), [(e["type"], e.get("stage")) for e in log["events"]])

    def test_stream_reports_steps_without_combining_model_deadlines(self):
        """两个模型调用各自未超时，即使累计超过单次上限也能返回。"""
        from agent.chat.review.workflow import ReviewContext

        async def delayed_model(**kwargs):
            """让两次模型调用累计超过任一次的时限。"""
            await asyncio.sleep(0.15)
            return await self._model(**kwargs)

        async def delayed_rewrite(**kwargs):
            async def resolve():
                await asyncio.sleep(0.15)
                return await self._rewrite(**kwargs)
            return await kwargs["execution"].run(resolve(), "解析指代")

        self.responses.extend([{"kind": "ready"}, {"kind": "answer", "message": "审核完成"}])
        with patch("infrastructure.llm.request_model_with_retry", delayed_model), patch(
            "agent.chat.review.workflow.rewrite_request", delayed_rewrite), patch(
            "service.travel.review_interaction.ReviewContext",
            side_effect=lambda *args, **kwargs: ReviewContext(*args, timeout_seconds=0.25, **kwargs),
        ):
            response = self.client.post(f"{self.path}/assistant?stream=true", json={
                **self.payload, "message": "检查路线",
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("application/x-ndjson", response.headers["content-type"])
        events = [json.loads(line) for line in response.text.splitlines()]
        progress = [event["message"] for event in events if event["type"] == "progress"]
        self.assertIn("正在解析指代…", progress)
        self.assertIn("正在调用模型…", progress)
        self.assertEqual(events[-1]["data"]["message"], "审核完成")

    def test_confirmed_plan_can_query_but_cannot_edit(self):
        """已确认行程允许通过助手查询，修改提案不会试算或写库。"""
        self.plan["planning"]["stage"] = "confirmed"
        self.payload.update(plan_version=fingerprint(self.plan), operations=[])
        self.responses.extend([{"kind": "ready"}, {"kind": "answer", "message": "行程已确认。"}])
        self.assertEqual(self._turn(message="检查已确认的路线")["status"], "answer")
        self._turn(status=400, message="修改预算", operations=[
            {"action": "update_budget", "payload": {"budget": 100}},
        ])
        self.save.assert_not_called()

    def test_generic_change_preview_requires_apply_action(self):
        """通用预览须经点击才返回草稿增量，而且点击仍不写库。"""
        operations = [{"action": "update_budget", "payload": {"budget": 800}}]
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "edit", "kind": "build_edits", "input": {}, "depends_on": []}]},
            {"status": "completed", "data": {"kind": "changes", "operations": operations}},
            {"kind": "changes", "message": "试算后总预算为 800 元。"},
        ])
        reply = self._turn(message="把总预算改到 800 元")
        self.assertEqual(reply["status"], "proposal")
        self.assertNotIn("plan", reply)
        block = reply["blocks"][0]
        self.assertEqual(block["type"], "change_preview")
        self.assertEqual(block["operations"], operations)
        updated = self._choose_first(reply)
        self.assertEqual(updated["status"], "draft_updated")
        self.assertEqual(updated["plan"]["budget"], 800)
        self.save.assert_not_called()
        self.assertFalse(self.responses)

    def test_delay_first_choice_then_apply_first_preview(self):
        """用户只说延后时，选择第一个时间并点击应用预览。"""
        self.responses.append({"kind": "clarify", "question": "测试公园延后到几点？",
                               "options": ["10:00", "11:30", "13:00"]})
        question = self._turn(message="把测试公园延后")
        operations = [{"action": "day_departure", "payload": {"day": 1, "time": "10:00"}}]
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "edit", "kind": "build_edits", "input": {}, "depends_on": []}]},
            {"status": "completed", "data": {"kind": "changes", "operations": operations}},
            {"kind": "changes", "message": "第一天将改为 10:00 出发。"},
        ])
        preview = self._choose_first(question)
        self.assertEqual(preview["status"], "proposal")
        resumed_intent = next(entry["request_context"] for entry in self.inputs
                              if entry.get("request_context", {}).get("clarification_answers"))
        self.assertEqual(resumed_intent["clarification_answers"][0]["answer"], "10:00")
        updated = self._choose_first(preview)
        self.assertEqual(updated["plan"]["planning"]["day_departures"]["1"], "10:00")
        self.assertEqual(updated["plan"]["itinerary"][0]["time_of_day"], "10:00")
        self.save.assert_not_called()
        self.assertFalse(self.responses)

    def test_audit_budget_read_only_reply(self):
        """模拟用户核对路线节奏和待保存预算，确认审核角色看到草稿。"""
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "audit", "kind": "audit_route",
                                       "input": {"question": "第一天会不会太赶，预算多少？"}, "depends_on": []}]},
            {"calls": [{"function": {"name": "get_review_context", "arguments": {"day": 1}}}]},
            {"status": "completed", "data": {"message": "第一天安排一处游玩地点，草稿总预算 900 元。"}},
            {"kind": "answer", "message": "第一天安排一处游玩地点，草稿总预算 900 元。"},
        ])
        history = [{"role": "user", "content": "先看第一天的路线。"},
                   {"role": "assistant", "content": "第一天有一处游玩地点。"}]
        result = self._turn(message="第一天会不会太赶？草稿总预算是多少？", history=history)
        self.assertEqual(result["status"], "answer")
        self.assertIn("900 元", result["message"])
        self.assertEqual(self.tool_results["get_review_context"]["status"], "success")
        scoped = self.tool_results["get_review_context"]["data"]
        self.assertEqual(scoped["day"], 1)
        self.assertEqual([item["item_id"] for item in scoped["itinerary"]], [ANCHOR["item_id"]])
        self.assertNotIn("place", scoped["itinerary"][0])
        self.assertEqual(scoped["itinerary"][0]["reference_id"], "park")
        self.assertNotIn("metrics", scoped["audit"])
        self.assertEqual(self.plan_reads, 1)
        for entry in self.inputs:
            self.assertFalse({"itinerary", "routes", "audit"} & entry["snapshot"].keys())
        budget = self.tool_results["get_review_context"]["data"]["audit"]["budget"]
        self.assertEqual(budget["limit"], 900)
        self.assertNotIn("items", budget)
        self.assertAlmostEqual(budget["remaining"] + budget["estimated_total"], 900)
        self.assertEqual(result["blocks"], [])
        self.assertEqual(self.inputs[0]["request_context"]["conversation_history"], history)
        self.assertEqual(self.inputs[-1]["request_context"]["conversation_history"], history)
        self.assertEqual(self.tool_sets, [
            {"delegate_task"}, {"get_review_context", "compare_review_route"},
            {"get_review_context", "compare_review_route"}, {"delegate_task"},
        ])
        self.save.assert_not_called()
        self.assertFalse(self.responses)

    def test_checkpoint_resume_uses_latest_plan_and_commits_once(self):
        """澄清从意图继续，预览确认只提交一次，不重复模型调用。"""
        self.responses.append({"kind": "clarify", "goal": "调整预算", "question": "调整到多少？",
                               "options": ["800 元", "900 元"]})
        paused = self._turn(conversation_id="chat", message="调整预算")["blocks"][0]
        self.plan["budget"] = 950  # 手动编辑在断点等待期间已经保存。
        operations = [{"action": "update_budget", "payload": {"budget": 800}}]
        self.responses.extend([
            {"kind": "tasks", "tasks": [{"id": "edit", "kind": "build_edits", "input": {}, "depends_on": []}]},
            {"status": "completed", "data": {"kind": "changes", "operations": operations}},
            {"kind": "changes", "message": "应用 item_id guided-1-park"},
        ])
        self._turn(status=400, conversation_id="chat", action_id=paused["action_id"], option_index=9)
        reads = self.plan_reads
        preview = self._turn(conversation_id="chat", action_id=paused["action_id"], option_index=0)
        self.assertEqual(self.plan_reads, reads + 1)
        self.assertEqual(preview["status"], "proposal")
        self.assertEqual(self.inputs[1]["request_context"]["clarification"],
                         {"question": "调整到多少？", "answer": "800 元"})
        self.assertNotIn("item_id", preview["message"])
        token = preview["blocks"][0]["action_id"]
        self._turn(status=409, conversation_id="other-chat", action_id=token)
        result = self._turn(conversation_id="chat", action_id=token)
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["plan"]["budget"], 800)
        self.assertNotIn("子Agent", result["message"])
        repeated = self._turn(conversation_id="chat", action_id=token)
        self.assertEqual(repeated["plan_version"], result["plan_version"])
        self.save.assert_called_once()
        self.assertFalse(self.responses)

    def test_checkpoint_multiple_places_share_one_save(self):
        """选择两个地点后无需再搜索，两项经联合试算一次写入。"""
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "search", "kind": "search_places", "input": {"selection_count": 2}, "depends_on": []}]},
            {"calls": [{"function": {"name": "search_review_places", "arguments": {"query": "景点"}}}]},
            {"status": "completed", "data": {"place_ids": ["museum", "gallery"]}},
            {"kind": "answer", "message": "请选择地点", "candidate_groups": [{
                "id": "attractions", "label": "景点", "place_ids": ["museum", "gallery"], "selection_count": 2,
            }]},
        ])
        block = self._turn(conversation_id="chat", message="添加两个地点")["blocks"][0]
        self.assertEqual((block["selection_mode"], block["groups"][0]["selection_count"]), ("multiple", 2))
        self._turn(status=400, conversation_id="chat", action_id=block["action_id"], option_indexes=[0])
        operations = [
            {"action": "review_place", "payload": {"item_id": ANCHOR["item_id"], "poi_id": poi,
                                                    "category": "attraction", "duration_minutes": 30}}
            for poi in ("museum", "gallery")
        ]
        self.responses.extend([
            {"kind": "tasks", "tasks": [{"id": "edit", "kind": "build_edits", "input": {}, "depends_on": []}]},
            {"status": "completed", "data": {"kind": "changes", "operations": operations}},
            {"kind": "changes"},
        ])
        result = self._turn(conversation_id="chat", action_id=block["action_id"], option_indexes=[0, 1])
        self.assertEqual(result["status"], "saved", result)
        self.assertEqual({x["title"] for x in result["plan"]["itinerary"]},
                         {"测试公园", "测试博物馆", "测试美术馆"})
        self.save.assert_called_once()
        self.assertFalse(self.responses)

    def test_checkpoint_preview_rechecks_version_before_saving(self):
        """确认时重新读取新基线；保留已验证的编辑并要求确认更新后的预览。"""
        self.responses.extend([
            {"kind": "ready"},
            {"kind": "tasks", "tasks": [{"id": "edit", "kind": "build_edits", "input": {}, "depends_on": []}]},
            {"status": "completed", "data": {"kind": "changes", "operations": [
                {"action": "update_budget", "payload": {"budget": 800}}
            ]}},
            {"kind": "changes"},
        ])
        preview = self._turn(conversation_id="chat", message="预算调整到800元")["blocks"][0]
        self.plan["notes"] = "另一个请求已修改备注"
        with TestClient(create_app(Path("mcp_server.py"), "offline")) as restarted:
            self.client = restarted
            refreshed = self._turn(conversation_id="chat", action_id=preview["action_id"])
            self.assertEqual(refreshed["status"], "proposal")
            self.save.assert_not_called()
            self.plan["budget"] = 750
            self._turn(status=409, conversation_id="chat", action_id=refreshed["blocks"][0]["action_id"])
        self.save.assert_not_called()
        self.assertFalse(self.responses)

    def test_checkpoint_mixed_groups_validate_counts_before_one_atomic_save(self):
        """公园一处、景点两处、餐饮一处独立验数，合并试算后只保存一次。"""
        park = {**PLACE, "place_id": "lake-park", "name": "测试湖滨公园", "longitude": 120.12}
        other_park = {**park, "place_id": "garden", "name": "测试花园", "longitude": 120.13}
        restaurant = {**PLACE, "place_id": "restaurant", "name": "测试餐厅", "longitude": 120.27}
        other_restaurant = {**restaurant, "place_id": "noodles", "name": "测试面馆", "longitude": 120.28}
        places = [park, other_park, PLACE, SECOND_PLACE, restaurant, other_restaurant]
        groups = [
            {"id": "parks", "label": "公园", "place_ids": ["lake-park", "garden"], "selection_count": 1},
            {"id": "attractions", "label": "景点", "place_ids": ["museum", "gallery"], "selection_count": 2},
            {"id": "dining", "label": "餐饮", "place_ids": ["restaurant", "noodles"], "selection_count": 1},
        ]
        candidates = {"park": [park, other_park], "attraction": [PLACE, SECOND_PLACE],
                      "food": [restaurant, other_restaurant]}

        async def model(**kwargs):
            if kwargs["stage"] != "search_places":
                return await self._model(**kwargs)
            messages = kwargs["messages"]
            scope = json.loads(messages[1]["content"])["extra_data"]["task_input"]["search_scope"]
            category = scope["category"]
            if not any(message["role"] == "tool" for message in messages):
                calls = [{"function": {"name": "search_review_places", "arguments": {"query": category}}}]
                content = ""
            else:
                calls = []
                content = json.dumps({"status": "completed", "reason": "", "data": {
                    "message": "已查到候选", "remaining": [],
                    "place_ids": [place["place_id"] for place in candidates[category]],
                }})
            return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls)), messages, None

        self.responses.extend([
            {"calls": [{"function": {"name": "delegate_task", "arguments": {
                "task_id": f"search-{category}", "role": "search_places", "task": f"寻找{group['label']}",
                "search_scope": {"category": category, "day": 1, "selection_count": group["selection_count"]},
                "extra_data": {"facts": "", "artifact_refs": []},
            }}} for category, group in zip(candidates, groups)]},
            {"kind": "answer", "message": "请按分组选择地点。", "candidate_groups": groups},
        ])
        with patch("infrastructure.llm.request_model_with_retry", model), patch(
            "agent.tools.implementations.review.amap_search_places",
            side_effect=lambda query, **_: {"success": True, "places": candidates[query]},
        ) as search, patch(
            "service.travel.map_planning.lookup_review_place", side_effect={place["place_id"]: place for place in places}.get
        ), patch("httpx.HTTPTransport.handle_request", side_effect=AssertionError("禁止真实网络")), patch(
            "httpx.AsyncHTTPTransport.handle_async_request", side_effect=AssertionError("禁止真实网络")
        ), patch("agent.chat.review.workflow.rewrite_request", side_effect=self._rewrite) as rewrite, patch(
            "service.travel.review_public_reply.request_model_with_retry", side_effect=self._public_model
        ) as public_reply:
            reply = self._turn(conversation_id="mixed-chat", message="添加1个公园、2个景点和1处餐饮")
            block = reply["blocks"][0]
            self.assertEqual(block["groups"], groups)
            self.assertCountEqual([call.args[0] for call in search.call_args_list], candidates)
            calls_before = (len(self.inputs), rewrite.await_count, public_reply.await_count)
            for indexes in ([0, 2, 3], [0, 1, 2, 3, 4], [0, 2, 4], [0, 2, 3, 4, 5]):
                with self.subTest(indexes=indexes):
                    self._turn(status=400, conversation_id="mixed-chat", action_id=block["action_id"], option_indexes=indexes)
                    self.assertEqual((len(self.inputs), rewrite.await_count, public_reply.await_count), calls_before)
                    self.save.assert_not_called()
                    self.assertEqual(self.checkpoints[block["action_id"]][1], "paused")
            operations = [{"action": "review_place", "payload": {
                "item_id": ANCHOR["item_id"], "poi_id": poi,
                "category": "food" if poi == "restaurant" else "attraction", "duration_minutes": 30,
            }} for poi in ("lake-park", "museum", "gallery", "restaurant")]
            self.responses.extend([
                {"calls": [{"function": {"name": "delegate_task", "arguments": {
                    "task_id": "edit-mixed", "role": "build_edits", "task": "添加用户分组选中的四处地点",
                    "extra_data": {"facts": "", "artifact_refs": []},
                }}}]},
                {"status": "completed", "data": {"kind": "changes", "operations": operations}},
                {"kind": "changes", "message": "按分组添加选中地点。"},
            ])
            result = self._turn(conversation_id="mixed-chat", action_id=block["action_id"], option_indexes=[0, 2, 3, 4])
            self.assertEqual(result["status"], "saved", result)
            self.assertTrue(self.tool_results["preview_review_changes"]["data"]["accepted"])
            additions = {item["place"]["place_id"]: item["category"] for item in result["plan"]["itinerary"]
                         if item["item_id"] != ANCHOR["item_id"]}
            self.assertEqual(additions, {"lake-park": "attraction", "museum": "attraction",
                                         "gallery": "attraction", "restaurant": "food"})
            self.assertEqual(self._turn(conversation_id="mixed-chat", action_id=block["action_id"])["plan_version"],
                             result["plan_version"])
        self.save.assert_called_once()
        self.assertFalse(self.responses)

    def test_dependent_food_search_waits_for_park_clarification(self):
        park = {**PLACE, "place_id": "park-a", "name": "测试公园甲"}
        other = {**PLACE, "place_id": "park-b", "name": "测试公园乙"}
        food = {**PLACE, "place_id": "food-a", "name": "测试餐厅"}

        def search_call(category, task):
            return {"calls": [{"function": {"name": "delegate_task", "arguments": {
                "task_id": f"search-{category}", "role": "search_places", "task": task,
                "search_scope": {"category": category, "day": 1, "selection_count": 1},
                "extra_data": {"facts": "", "artifact_refs": []},
            }}}]}

        self.responses.extend([
            search_call("park", "找公园"),
            {"calls": [{"function": {"name": "search_review_places", "arguments": {"query": "公园"}}}]},
            {"status": "completed", "data": {"place_ids": ["park-a", "park-b"]}},
            {"kind": "clarify", "message": "先确认公园", "question": "选哪座公园作为锚点？",
             "options": [park["name"], other["name"]]},
        ])
        with patch("agent.tools.implementations.review.amap_search_places", side_effect=[
            {"success": True, "places": [park, other]}, {"success": True, "places": [food]},
        ]) as search:
            clarification = self._turn(conversation_id="dependent", message="选好第一天的公园再找附近餐厅")
            self.assertEqual(clarification["status"], "clarify")
            search.assert_called_once()
            self.responses.extend([
                search_call("food", "找测试公园甲附近餐厅"),
                {"calls": [{"function": {"name": "search_review_places", "arguments": {"query": "测试公园甲 餐厅"}}}]},
                {"status": "completed", "data": {"place_ids": ["food-a"]}},
                {"kind": "answer", "message": "已查到附近餐厅"},
            ])
            answer = self._turn(conversation_id="dependent", action_id=clarification["blocks"][0]["action_id"],
                                option_index=0)
            self.assertEqual(answer["status"], "answer")
            self.assertEqual(answer["blocks"][0]["places"][0]["place_id"], "food-a")
            self.assertEqual([call.args[0] for call in search.call_args_list], ["公园", "测试公园甲 餐厅"])
            self.assertEqual(self.inputs[-1]["request_context"]["clarification"]["answer"], park["name"])

    def test_followup_references_receive_prior_public_history_at_rewrite_entry(self):
        """离线核对多轮公开上下文传入 rewrite；不测试真实模型消歧质量。"""
        self.responses.extend([
            {"kind": "answer", "message": "第一天可考虑测试博物馆、测试美术馆。"},
            {"kind": "answer", "message": "请确认要加入的地点和日期。"},
        ])
        with patch("agent.chat.review.workflow.rewrite_request", side_effect=self._rewrite) as rewrite:
            first = self._turn(conversation_id="references-chat", message="第一天有哪些景点？")
            history = [{"role": "user", "content": "第一天有哪些景点？"},
                       {"role": "assistant", "content": first["message"]}]
            self._turn(conversation_id="references-chat", message="把第二个加到那天", history=history)
        self.assertEqual(rewrite.await_count, 2)
        self.assertEqual(rewrite.await_args.kwargs["text"], "把第二个加到那天")
        self.assertEqual(rewrite.await_args.kwargs["history"], tuple(history))
        context = self.inputs[-1]["request_context"]
        self.assertEqual(context["original_text"], "把第二个加到那天")
        self.assertEqual(context["conversation_history"], history)
        self.assertNotIn("original_text", context["rewrite"])
        self.save.assert_not_called()


    def test_partial_candidates_without_valid_groups_are_read_only_in_both_modes(self):
        result = SimpleNamespace(status="partial", message="候选分组尚未完整交付", places=[PLACE],
                                 candidate_groups=[], clarification=None, progress={}, snapshot=None,
                                 continuation={}, operations=[])
        with patch("service.travel.review_interaction.run_review_turn", return_value=result):
            for fields in ({}, {"conversation_id": "missing-groups"}):
                with self.subTest(fields=fields):
                    response = self._turn(message="搜索公园与餐饮", **fields)
                    self.assertEqual(response["status"], "partial")
                    block = next(block for block in response["blocks"] if block["type"] == "place_candidates")
                    self.assertNotIn("action_id", block)
                    self.assertNotIn("selection_mode", block)
            self.assertFalse(self.checkpoints)
            self.save.assert_not_called()
