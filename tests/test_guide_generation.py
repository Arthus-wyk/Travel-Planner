"""Generation tool contracts exercised through the real HTTP model adapter."""
import json
from copy import deepcopy
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch, MagicMock
import httpx
from fastapi.testclient import TestClient

from agent.planning_graph import run_planning_action, default_choices
from agent.planning_graph import _logged_node
from service.travel import planning
from service.travel.generation_jobs import next_stage
from service.travel.guide import GuideNeedsInput
from service.travel.place_matching import search_and_match
from service.utils.model_client import OpenAICompatibleClient
from service.storage import planning_jobs
from service.travel.generation_jobs import execute_job
from service.api import server

POINT = {"place_id": "real-poi", "name": "西湖景区", "address": "杭州市西湖区",
         "longitude": 120.1, "latitude": 30.2, "types": ["风景名胜"]}


def base_plan():
    plan = planning.build_candidates(planning.create_draft({"destination": "杭州", "departure_city": "上海",
        "start_date": "2026-11-06", "trip_days": 3, "travelers": 1, "budget": 9000}, "generation-test"))
    candidates = plan["planning"]["transport_candidates"]
    return planning.select_candidates(plan, {d: next(x["id"] for x in candidates if x["direction"] == d) for d in ("outbound", "return")})


class GenerationTests(TestCase):
    def test_checkpoint_next_stage_keeps_retry_and_audit_loop(self):
        self.assertEqual(next_stage("activity_agent", {}), "search_places")
        self.assertEqual(next_stage("itinerary_agent", {"proposal_error": "invalid"}), "itinerary_agent")
        self.assertEqual(next_stage("itinerary_agent", {"proposal_error": ""}), "route_agent")
        self.assertEqual(next_stage("route_agent", {}), "schedule_agent")
        self.assertEqual(next_stage("schedule_agent", {}), "audit_agent")
        self.assertEqual(next_stage("audit_agent", {"action": "generate_from_guide", "attempts": 3, "audit": {"issues": []}}), "done")

    def test_failed_stage_preserves_last_checkpoint_for_retry(self):
        plan = base_plan()
        with patch.object(planning_jobs, "read_checkpoint", return_value=None), patch.object(planning_jobs, "save_checkpoint") as save, patch.object(planning_jobs, "update_job") as update, patch.object(planning_jobs, "commit_result") as commit, patch("service.travel.generation_jobs.run_planning_action") as run:
            def fail_after_checkpoint(_plan, _action, _payload, progress, resume):
                progress("prepare_draft", {"trace": ["prepare_draft"], "plan": plan, "attempts": 0})
                raise ValueError("model failed")
            run.side_effect = fail_after_checkpoint
            execute_job(plan["plan_id"], "job", plan, {"text": "西湖", "image_base64": "secret-image"})
        self.assertEqual(save.call_args.args[2]["resume_stage"], "itinerary_agent")
        self.assertEqual(save.call_args.args[2]["payload"], {})
        self.assertEqual(update.call_args.args[2], "failed")
        commit.assert_not_called()

    def test_invalid_guide_after_retries_requests_manual_input(self):
        plan = base_plan()
        with patch.object(planning_jobs, "read_checkpoint", return_value=None), patch.object(planning_jobs, "update_job") as update, patch.object(planning_jobs, "commit_result") as commit, patch("service.travel.generation_jobs.run_planning_action", side_effect=GuideNeedsInput("请手动修正地点")):
            execute_job(plan["plan_id"], "job", plan, {"text": "西湖"})
        self.assertEqual(update.call_args.args[2], "needs_input")
        commit.assert_not_called()

    def test_graph_resumes_at_saved_node(self):
        plan = base_plan()
        resume = {"plan": plan, "action": "generate_from_guide", "payload": {"text": "西湖"},
                  "trace": ["coordinator", "prepare_draft", "itinerary_agent", "route_agent"],
                  "attempts": 3, "resume_stage": "audit_agent"}
        progress = MagicMock()
        with patch("agent.planning_graph.audit_plan", return_value={"issues": [], "guide_constraints": []}), patch("agent.planning_graph.generate_itinerary_proposal") as generate:
            outcome = run_planning_action(plan, "generate_from_guide", {"text": "西湖"}, progress=progress, resume=resume)
        self.assertEqual(outcome["trace"][-1], "audit_agent")
        progress.assert_called_once()
        self.assertEqual(progress.call_args.args[0], "audit_agent")
        generate.assert_not_called()

    def test_graph_node_logs_output_without_image_or_key(self):
        with patch("agent.planning_graph.logging.getLogger") as get_logger:
            result = _logged_node("guide_agent", lambda state: {
                "guide_text": "西湖", "image_base64": "large-image",
                "nested": {"api_key": "secret"},
            })({})
        self.assertEqual(result["guide_text"], "西湖")
        message = get_logger.return_value.info.call_args.args[3]
        self.assertEqual(get_logger.return_value.info.call_args.args[2], "guide_agent")
        self.assertIn("西湖", message)
        self.assertNotIn("large-image", message)
        self.assertNotIn("secret", message)

    def client(self, handler):
        return OpenAICompatibleClient("https://example.com/v1", "test", transport=httpx.MockTransport(handler))

    def test_full_graph_executes_real_function_call_then_schema_match(self):
        requests = []
        def handler(request):
            body = json.loads(request.content); requests.append(body)
            if body.get("tools"):
                self.assertEqual(body["tool_choice"]["function"]["name"], "search_map_places")
                self.assertFalse(body["parallel_tool_calls"])
                message = {"tool_calls": [{"function": {"name": "search_map_places", "arguments": json.dumps({"place_position": 1, "query": "西湖"})}}]}
            elif "candidate_position" in body["response_format"]["json_schema"]["schema"]["properties"]:
                self.assertNotIn("real-poi", str(body))
                message = {"content": json.dumps({"candidate_position": 1, "reason": "地图类别是景区"})}
            else:
                self.assertNotIn("real-poi", str(body))
                message = {"content": json.dumps({"days": [
                    {"day": 1, "visits": []}, {"day": 2, "visits": [{"position": 1, "travel_mode": "WALK"}]},
                    {"day": 3, "visits": []}], "unscheduled": []})}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        with patch("service.utils.model_client.create_model_client", return_value=self.client(handler)), patch("service.utils.model_client.get_model", return_value="test"), patch("service.travel.place_matching.create_model_client", return_value=self.client(handler)), patch("service.travel.place_matching.get_model", return_value="test"), patch("service.travel.planning.analyze_guide", return_value={"text": "西湖", "places": [{"id": "internal", "name": "西湖", "category": "attraction"}], "constraints": []}), patch("service.travel.place_matching.amap_search_places", return_value={"success": True, "places": [POINT, {**POINT, "place_id": "shop", "name": "西湖商场", "types": ["购物服务"]}]}) as search, patch("agent.planning_graph.AMAP_API_KEY", ""):
            outcome = run_planning_action(base_plan(), "generate_from_guide", {"text": "西湖"})
        search.assert_called_once_with("西湖", region_code="杭州", max_results=5)
        self.assertEqual(outcome["plan"]["planning"]["stage"], "review")
        activity = outcome["plan"]["planning"]["activities"][0]
        self.assertEqual(activity["place"]["place_id"], "real-poi")
        self.assertEqual(activity["place_status"], "model_matched")
        self.assertTrue(any(x["type"] == "missing_hotel" for x in outcome["audit"]["issues"]))

    def test_missing_wrong_or_invalid_tool_call_never_executes_map(self):
        messages = [{"content": "我查过了"}, {"tool_calls": [{"function": {"name": "other", "arguments": "{}"}}]},
                    {"content": "已经找到了", "tool_calls": [{"function": {"name": "search_map_places", "arguments": '{"place_position":1,"query":"西湖"}'}}]},
                    {"tool_calls": [{"function": {"name": "search_map_places", "arguments": '{"place_position":99,"query":"西湖"}'}}]}]
        for message in messages:
            with self.subTest(message=message), patch("service.travel.place_matching.create_model_client", return_value=self.client(lambda _: httpx.Response(200, json={"choices": [{"message": message}]}))), patch("service.travel.place_matching.get_model", return_value="test"), patch("service.travel.place_matching.amap_search_places") as search:
                plan = base_plan(); plan["planning"]["activities"] = [{"id": "p", "name": "西湖"}]
                result = search_and_match(plan)
                search.assert_not_called()
                self.assertEqual(result["planning"]["activities"][0]["place_status"], "failed")
                self.assertEqual(result["planning"]["place_events"][-1]["event"], "error")

    def test_saved_manual_location_is_not_searched(self):
        plan = base_plan(); plan["planning"]["activities"] = [{"id": "p", "name": "西湖", "place": POINT, "place_status": "manual_confirmed"}]
        with patch("service.travel.place_matching._chat") as chat:
            self.assertEqual(search_and_match(plan)["planning"]["activities"][0]["place"], POINT)
        chat.assert_not_called()

    def test_excluded_hotel_is_not_automatically_selected(self):
        plan = base_plan()
        plan["planning"].update(hotel_candidates=[{"id": "h", "name": "酒店", "place": POINT}], selected_hotel="h", activities=[{"id": "h", "category": "hotel", "choice": "exclude"}])
        self.assertIsNone(default_choices({"plan": plan, "trace": []})["plan"]["planning"]["selected_hotel"])

    def test_image_text_is_not_replaced_by_stale_text_in_prepare(self):
        with patch("agent.planning_graph.recognize_guide", return_value="图片中的西湖"), patch("service.travel.planning.analyze_guide", return_value={"text": "图片中的西湖", "places": [{"id": "p", "name": "西湖"}], "constraints": []}) as analyze, patch("agent.planning_graph.search_and_match", side_effect=lambda plan: plan), patch("agent.planning_graph.generate_itinerary_proposal", return_value={"days": [{"day": day, "visits": []} for day in range(1, 4)], "unscheduled": [{"activity_id": "p", "reason": "位置待确认"}], "assumptions": [], "constraint_results": []}), patch("agent.planning_graph.AMAP_API_KEY", ""):
            result = run_planning_action(base_plan(), "generate_from_guide", {"image_base64": "eA==", "text": "旧攻略"})
        analyze.assert_called_once_with("图片中的西湖")
        self.assertEqual(result["plan"]["planning"]["guide_text"], "图片中的西湖")

    def test_null_or_out_of_range_match_keeps_unresolved(self):
        from types import SimpleNamespace
        tool = SimpleNamespace(tool_calls=[{"function": {"name": "search_map_places", "arguments": {"place_position": 1, "query": "西湖"}}}])
        for choice in (None, 99, 2):
            with self.subTest(choice=choice), patch("service.travel.place_matching._chat", side_effect=[tool, SimpleNamespace(content=json.dumps({"candidate_position": choice, "reason": "核对类别"}), tool_calls=[])]), patch("service.travel.place_matching.amap_search_places", return_value={"success": True, "places": [POINT, {**POINT, "name": "西湖商场", "types": ["购物服务"]}]}):
                plan = base_plan(); plan["planning"]["activities"] = [{"id": "p", "name": "西湖", "category": "attraction"}]
                self.assertNotIn("place", search_and_match(plan)["planning"]["activities"][0])

    def test_no_hotel_draft_supports_moving_resolved_stop(self):
        plan = base_plan(); plan["planning"].update(guide_auto=True, activities=[{"id": "p", "name": "西湖", "place": POINT, "choice": "optional", "duration_minutes": 90}])
        proposal = {"stops": [{"activity_id": "p", "day": 2, "time_of_day": "10:00", "duration_minutes": 90}], "unscheduled": []}
        draft = planning.draft_itinerary(plan, proposal)
        visit = next(x for x in draft["itinerary"] if x.get("reference_id") == "p")
        moved = planning.move_activity(draft, visit["item_id"], 1)
        self.assertEqual(next(x for x in moved["itinerary"] if x.get("reference_id") == "p")["day"], 1)

    def test_commit_rejects_concurrent_plan_edit_before_update(self):
        original = base_plan(); changed = deepcopy(original); changed["budget"] += 1
        conn = MagicMock(); conn.__enter__.return_value = conn; cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.return_value = changed
        with patch.object(planning_jobs, "connect", return_value=conn), patch.object(planning_jobs, "row_to_plan", side_effect=lambda x: x):
            with self.assertRaisesRegex(ValueError, "已修改"):
                planning_jobs.commit_result(original["plan_id"], "job", original, original)
        conn.rollback.assert_called_once()
        self.assertFalse(any("UPDATE travel_plans" in c.args[0] for c in cur.execute.call_args_list))

    def test_failed_worker_leaves_plan_unchanged(self):
        with patch("service.travel.generation_jobs.run_planning_action", side_effect=ValueError("bad")), patch.object(planning_jobs, "read_checkpoint", return_value=None), patch.object(planning_jobs, "update_job") as update, patch.object(planning_jobs, "commit_result") as commit:
            execute_job("p", "j", {}, {})
        commit.assert_not_called()
        self.assertEqual(update.call_args.args[2], "failed")

    def test_restart_marks_unfinished_jobs_failed(self):
        conn = MagicMock(); conn.__enter__.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        with patch.object(planning_jobs, "ensure_jobs"), patch.object(planning_jobs, "connect", return_value=conn):
            planning_jobs.recover_jobs()
        self.assertIn("status IN ('queued','running')", cur.execute.call_args.args[0])

    def test_commit_updates_plan_and_job_in_same_transaction(self):
        original = base_plan(); conn = MagicMock(); conn.__enter__.return_value = conn
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [original, {"job_id": "j", "status": "running"}]
        with patch.object(planning_jobs, "connect", return_value=conn), patch.object(planning_jobs, "row_to_plan", side_effect=lambda x: x):
            planning_jobs.commit_result(original["plan_id"], "j", original, original)
        conn.begin.assert_called_once(); conn.commit.assert_called_once(); conn.rollback.assert_not_called()
        self.assertTrue(any("UPDATE travel_plans" in c.args[0] for c in cur.execute.call_args_list))

    def test_websocket_sends_persisted_job_snapshot(self):
        with patch.object(server.planning_jobs, "read_job", return_value={"job_id": "j", "status": "running", "stage": "itinerary_agent"}):
            with TestClient(server.create_app(Path("mcp_server.py"), "test")) as client:
                with client.websocket_connect("/api/plans/p/generation/ws") as socket:
                    packet = socket.receive_json()
                    self.assertEqual(packet["type"], "generation_status")
                    self.assertEqual(packet["job"]["stage"], "itinerary_agent")
                    socket.close()

    def test_generation_api_deduplicated_job_is_not_executed_again(self):
        with patch.object(server.planning_jobs, "create_job", return_value=("job", None)), patch.object(server, "execute_job") as execute, patch.object(server.planning_jobs, "read_job", return_value={"job_id": "job", "status": "running"}):
            with TestClient(server.create_app(Path("mcp_server.py"), "test")) as client:
                self.assertEqual(client.post("/api/plans/p/generation", json={"text": "西湖"}).json()["job_id"], "job")
                self.assertEqual(client.get("/api/plans/p/generation").json()["job"]["status"], "running")
        execute.assert_not_called()
