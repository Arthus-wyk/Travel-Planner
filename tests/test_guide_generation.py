"""Generation tool contracts exercised through the real HTTP model adapter."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch, MagicMock
import httpx
from fastapi.testclient import TestClient

from agent.planning.planning_graph import run_planning_action, default_choices
from agent.planning.planning_graph import _logged_node
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
    def test_old_checkpoint_is_rebuilt(self):
        checkpoint = {"schema_version": 3, "resume_stage": "itinerary_agent"}
        with patch.object(planning_jobs, "read_checkpoint", return_value=checkpoint), patch.object(planning_jobs, "update_job"), patch.object(planning_jobs, "commit_result"), patch("service.travel.generation_jobs.run_planning_action", return_value={"plan": {"planning": {}}}) as run:
            execute_job("plan", "job", {}, {}, retry=True)
        self.assertIsNone(run.call_args.kwargs["resume"])

    def test_manual_regeneration_ignores_checkpoint(self):
        from service.travel.generation_jobs import CHECKPOINT_SCHEMA_VERSION
        checkpoint = {"schema_version": CHECKPOINT_SCHEMA_VERSION, "resume_stage": "itinerary_agent",
                      "format_attempts": 2, "attempts": 5, "revision_attempts": 1, "feedback": [{"message": "missing"}]}
        with patch.object(planning_jobs, "read_checkpoint", return_value=checkpoint), patch.object(planning_jobs, "update_job"), patch.object(planning_jobs, "commit_result"), patch("service.travel.generation_jobs.run_planning_action", return_value={"plan": {"planning": {}}}) as run:
            execute_job("plan", "job", {}, {})
        self.assertIsNone(run.call_args.kwargs["resume"])

    def test_explicit_retry_resumes_matching_checkpoint(self):
        from service.travel.generation_jobs import CHECKPOINT_SCHEMA_VERSION, execute_job
        payload = {"text": "西湖"}
        checkpoint = {"schema_version": CHECKPOINT_SCHEMA_VERSION, "resume_stage": "itinerary_agent",
                      "format_attempts": 3, "feedback": ["retry feedback"]}
        with patch.object(planning_jobs, "read_checkpoint", return_value=checkpoint) as read, \
             patch.object(planning_jobs, "update_job"), patch.object(planning_jobs, "commit_result"), \
             patch("service.travel.generation_jobs.run_planning_action", return_value={"plan": {"planning": {}}}) as run:
            execute_job("plan", "job", {}, payload, retry=True)
        read.assert_called_once_with("plan", planning_jobs.fingerprint({"plan": {}, "payload": payload}))
        self.assertEqual(run.call_args.kwargs["resume"], {**checkpoint, "payload": payload, "format_attempts": 0})

    def test_retry_api_passes_resume_intent_to_background_job(self):
        from pathlib import Path
        from fastapi.testclient import TestClient
        from service.api import server

        plan = {"plan_id": "plan"}
        payload = {"text": "西湖"}
        with patch.object(planning_jobs, "create_job", return_value=("job", plan)) as create, \
             patch.object(server, "execute_job") as execute, \
             TestClient(server.create_app(Path("mcp_server.py"), "test")) as client:
            response = client.post("/api/plans/plan/generation?retry=true", json=payload)
        self.assertEqual(response.status_code, 200)
        create.assert_called_once_with("plan", payload, retry=True)
        execute.assert_called_once_with("plan", "job", plan, payload, retry=True)

    def test_checkpoint_next_stage_keeps_retry_and_audit_loop(self):
        self.assertEqual(next_stage("activity_agent", {}), "search_places")
        self.assertEqual(next_stage("itinerary_agent", {"proposal_error": "invalid"}), "itinerary_agent")
        self.assertEqual(next_stage("itinerary_agent", {"proposal_error": ""}), "route_agent")
        self.assertEqual(next_stage("route_agent", {}), "schedule_agent")
        self.assertEqual(next_stage("schedule_agent", {}), "audit_agent")
        self.assertEqual(next_stage("audit_agent", {"action": "generate_from_guide", "attempts": 3, "audit": {"issues": []}}), "done")

    def test_unlocated_place_does_not_reinvoke_ordering_model(self):
        from agent.planning.planning_graph import after_audit
        state = {"action": "generate_from_guide", "revision_attempts": 1,
                 "audit": {"issues": [{"type": "unplaced_activities", "severity": "high"}]}}
        self.assertEqual(after_audit(state), "done")

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
        with patch("agent.planning.planning_graph.audit_plan", return_value={"issues": [], "guide_constraints": []}), patch("agent.planning.planning_graph.generate_itinerary_proposal") as generate:
            outcome = run_planning_action(plan, "generate_from_guide", {"text": "西湖"}, progress=progress, resume=resume)
        self.assertEqual(outcome["trace"][-1], "audit_agent")
        progress.assert_called_once()
        self.assertEqual(progress.call_args.args[0], "audit_agent")
        generate.assert_not_called()

    def test_graph_node_does_not_log_state(self):
        with patch("agent.planning.planning_graph.logging.getLogger") as get_logger:
            result = _logged_node("guide_agent", lambda state: {
                "guide_text": "西湖", "trace": ["guide_agent"],
            })({"payload": {"image_base64": "large-image", "nested": {"api_key": "secret"}}})
        self.assertEqual(result["guide_text"], "西湖")
        get_logger.assert_not_called()

    def test_model_output_logs_content_and_tool_calls(self):
        from types import SimpleNamespace
        from service.travel.generation_model_log import log_model_output
        message = SimpleNamespace(content="模型生成内容", tool_calls=[{"function": {"name": "search_map_places"}}])
        with patch("service.travel.generation_model_log.write_event") as write:
            log_model_output("place_search", message)
        kind, data = write.call_args.args
        self.assertEqual(kind, "model.responses")
        self.assertEqual(data["body"]["content"], "模型生成内容")
        self.assertEqual(data["body"]["tool_calls"][0]["function"]["name"], "search_map_places")

    def client(self, handler):
        return OpenAICompatibleClient("https://example.com/v1", "test", transport=httpx.MockTransport(handler))

    def test_full_graph_searches_map_directly_before_itinerary_model(self):
        requests = []
        def handler(request):
            body = json.loads(request.content); requests.append(body)
            self.assertFalse(body.get("tools"))
            self.assertNotIn("real-poi", str(body))
            message = {"content": json.dumps({"days": [
                {"day": 1, "visits": []}, {"day": 2, "visits": [{"position": 1}]},
                {"day": 3, "visits": []}]})}
            return httpx.Response(200, json={"choices": [{"message": message}]})
        plan = base_plan()
        with patch("service.utils.model_client.create_model_client", return_value=self.client(handler)), patch("service.utils.model_client.get_model", return_value="test"), patch("service.travel.planning.analyze_guide", return_value={"text": "西湖", "places": [{"id": "internal", "name": "西湖", "category": "attraction"}], "constraints": []}), patch("service.travel.place_matching.amap_search_places", return_value={"success": True, "places": [POINT, {**POINT, "place_id": "other", "name": "西湖公园", "types": ["风景名胜"]}]}) as search, patch("agent.planning.planning_graph.AMAP_API_KEY", ""):
            outcome = run_planning_action(plan, "generate_from_guide", {"text": "西湖"})
        search.assert_called_once_with("西湖", region_code="杭州", max_results=5)
        self.assertEqual(outcome["plan"]["planning"]["selected_transport"], plan["planning"]["selected_transport"])
        self.assertEqual(outcome["trace"], [
            "coordinator", "guide_agent", "activity_agent", "place_search_agent", "place_match_agent",
            "default_choices", "prepare_draft", "itinerary_agent", "route_agent", "schedule_agent", "audit_agent",
        ])
        self.assertEqual(outcome["plan"]["planning"]["stage"], "review")
        activity = outcome["plan"]["planning"]["activities"][0]
        self.assertEqual(activity["place"]["place_id"], "real-poi")
        self.assertEqual(activity["place_status"], "auto_confirmed")
        self.assertEqual(len(requests), 1)
        self.assertTrue(any(x["type"] == "missing_hotel" for x in outcome["audit"]["issues"]))
        placeholders = [x for x in outcome["plan"]["itinerary"] if x.get("lodging_pending")]
        self.assertEqual([(x["day"], x["anchor_role"]) for x in placeholders],
                         [(1, "end"), (2, "start"), (2, "end"), (3, "start")])

    def test_saved_manual_location_is_not_searched(self):
        plan = base_plan(); plan["planning"]["activities"] = [{"id": "p", "name": "西湖", "place": POINT, "place_status": "manual_confirmed"}]
        with patch("service.travel.place_matching.amap_search_places") as search:
            self.assertEqual(search_and_match(plan)["planning"]["activities"][0]["place"], POINT)
        search.assert_not_called()

    def test_review_saves_a_hotel_pair_and_inserts_stops_in_display_order(self):
        plan = base_plan()
        plan["planning"]["guide_auto"] = True
        plan = planning.draft_itinerary(plan)
        hotel = "manual-" + "a" * 32
        meal = "manual-" + "b" * 32
        attraction = "manual-" + "c" * 32
        operations = [
            {"action": "review_place", "payload": {"item_id": "guided-1-end", "replace": True,
             "activity_id": hotel, "category": "hotel", "poi_id": "hotel", "visit_tips": "晚到"}},
            {"action": "review_place", "payload": {"item_id": "guided-2-start",
             "activity_id": meal, "category": "food", "poi_id": "meal"}},
            {"action": "review_place", "payload": {"item_id": f"guided-2-{meal}",
             "activity_id": attraction, "category": "attraction", "poi_id": "park", "duration_minutes": 120}},
        ]
        with patch("service.travel.map_planning.lookup_review_place", side_effect=lambda poi: {
                **POINT, "place_id": poi, "name": poi, "address": f"杭州 {poi}"}), patch(
                "agent.planning.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning.planning_graph.amap_compute_route",
                side_effect=lambda *args, **kwargs: {"success": True, "duration": "300s", "legs": []}):
            result = run_planning_action(plan, "review_changes", {"operations": operations})["plan"]
        day_two = [x for x in result["itinerary"] if x["day"] == 2]
        self.assertEqual([x.get("reference_id") for x in day_two], [hotel, meal, attraction, None])
        self.assertEqual([x["duration_minutes"] for x in day_two[1:3]], [60, 120])
        self.assertEqual([x["time_of_day"] for x in day_two[:3]], ["08:00", "08:05", "09:10"])
        routes = {x["origin_item_id"]: x for x in result["routes"]}
        self.assertEqual([routes[x["item_id"]]["duration_minutes"] for x in day_two[:2]], [5, 5])
        self.assertEqual(day_two[0]["visit_tips"], "晚到")
        self.assertEqual(planning.itinerary_sheet(result)["hotels"][0]["nights"], 1)
        self.assertTrue(any(x.get("lodging_pending") for x in result["itinerary"] if x["day"] == 3))

    def test_review_can_complete_lodging_with_one_selected_hotel_without_duplicate_stops(self):
        plan = base_plan()
        plan["planning"]["guide_auto"] = True
        plan = planning.draft_itinerary(plan)
        hotel_id = "manual-" + "a" * 32
        meal_id = "manual-" + "b" * 32
        meal = {"action": "review_place", "payload": {"item_id": "guided-1-outbound",
            "activity_id": meal_id, "category": "food", "poi_id": "meal"}}
        addition = {"action": "review_place", "payload": {"item_id": f"guided-1-{meal_id}",
            "activity_id": hotel_id, "category": "hotel", "poi_id": "hotel"}}
        completion = {"action": "complete_lodging", "payload": {"activity_id": hotel_id}}
        with patch("service.travel.map_planning.lookup_review_place", return_value=POINT), patch("agent.planning.planning_graph.AMAP_API_KEY", ""):
            result = run_planning_action(plan, "review_changes", {"operations": [meal, addition, completion]})["plan"]
            repeated = run_planning_action(result, "review_changes", {"operations": [completion]})["plan"]
        hotels = [x for x in result["itinerary"] if x.get("reference_id") == hotel_id]
        self.assertEqual([(x["day"], x["category"], x.get("anchor_role")) for x in hotels], [
            (1, "hotel_arrival", None), (1, "hotel_anchor", "end"), (2, "hotel_anchor", "start"),
            (2, "hotel_anchor", "end"), (3, "hotel_anchor", "start"), (3, "hotel", "checkout")])
        self.assertEqual(result["itinerary"], repeated["itinerary"])
        self.assertEqual([x["category"] for x in result["itinerary"] if x["day"] == 1],
                         ["home_anchor", "intercity_transport", "hotel_arrival", "food", "hotel_anchor"])
        self.assertEqual(result["itinerary"][-1]["category"], "home_anchor")
        self.assertEqual(result["itinerary"][-3]["anchor_role"], "checkout")

    def test_matching_uses_first_compatible_candidate_without_a_model_call(self):
        candidates = [{**POINT, "place_id": "wrong-type", "types": ["购物服务"]},
                      {**POINT, "place_id": "wrong-city", "address": "上海市黄浦区"},
                      POINT, {**POINT, "place_id": "second", "name": "西湖公园"}]
        plan = base_plan()
        plan["planning"]["activities"] = [{"id": "p", "name": "西湖", "category": "attraction"}]
        with patch(
            "service.travel.place_matching.amap_search_places", return_value={"success": True, "places": candidates}
        ):
            item = search_and_match(plan)["planning"]["activities"][0]
        self.assertEqual(item["place_candidates"], candidates[2:])
        self.assertEqual(item["place"], POINT)
        self.assertEqual(item["place_status"], "auto_confirmed")

    def test_excluded_hotel_is_not_automatically_selected(self):
        plan = base_plan()
        plan["planning"].update(hotel_candidates=[{"id": "h", "name": "酒店", "place": POINT}], selected_hotel="h", activities=[{"id": "h", "category": "hotel", "choice": "exclude"}])
        self.assertIsNone(default_choices({"plan": plan, "trace": []})["plan"]["planning"]["selected_hotel"])

    def test_image_text_is_not_replaced_by_stale_text_in_prepare(self):
        with patch("agent.planning.planning_graph.recognize_guide", return_value="图片中的西湖"), patch("service.travel.planning.analyze_guide", return_value={"text": "图片中的西湖", "places": [{"id": "p", "name": "西湖"}], "constraints": []}) as analyze, patch("agent.planning.planning_graph.search_and_match", side_effect=lambda plan: plan), patch("agent.planning.planning_graph.generate_itinerary_proposal", return_value={"days": [{"day": day, "visits": []} for day in range(1, 4)], "unscheduled": [{"activity_id": "p", "reason": "位置待确认"}], "assumptions": [], "constraint_results": []}), patch("agent.planning.planning_graph.AMAP_API_KEY", ""):
            with self.assertRaises(GuideNeedsInput) as raised:
                run_planning_action(base_plan(), "generate_from_guide", {"image_base64": "eA==", "text": "旧攻略"})
        analyze.assert_called_once_with("图片中的西湖")
        self.assertEqual(raised.exception.plan["planning"]["guide_text"], "图片中的西湖")

    def test_no_valid_poi_keeps_place_unresolved(self):
        plan = base_plan()
        plan["planning"]["activities"] = [{"id": "p", "name": "西湖", "category": "attraction"}]
        with patch(
            "service.travel.place_matching.amap_search_places", return_value={"success": True, "places": []}
        ):
            item = search_and_match(plan)["planning"]["activities"][0]
        self.assertNotIn("place", item)
        self.assertEqual(item["place_status"], "failed")
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


class DegradationTests(TestCase):
    def test_search_retries_only_transient_errors(self):
        from service.travel.place_matching import _search
        for message, count in [('INVALID_PARAMS', 1), ('Amap network request failed', 2)]:
            with self.subTest(message=message), patch('service.travel.place_matching.amap_search_places', return_value={'success': False, 'message': message}) as search:
                _search('lake')
                self.assertEqual(search.call_count, count)

    def test_exhausted_qps_preserves_place_and_lodging_failures(self):
        plan = base_plan()
        plan["planning"].update(
            activities=[{"id": "p", "name": "西湖", "category": "attraction"}],
            lodging_areas=[{"name": "湖滨"}],
        )
        failure = {"success": False, "infocode": "10021", "message": "高德限流（10021 CUQPS_HAS_EXCEEDED_THE_LIMIT）"}
        with patch("service.integrations.travel_amap.sleep"), patch(
            "service.travel.place_matching.amap_search_places", return_value=failure,
        ) as search:
            state = search_and_match(plan)["planning"]
        self.assertEqual(search.call_count, 6)
        self.assertEqual(state["activities"][0]["place_error"], failure["message"])
        self.assertEqual(state["lodging_search_errors"], {"湖滨": failure["message"]})

    def test_ordering_exhaustion_preserves_every_located_place(self):
        from agent.planning.planning_graph import prepare_draft, itinerary_agent
        plan = base_plan()
        plan['planning']['guide_auto'] = True
        plan['planning']['activities'] = [
            {'id': 'a', 'name': 'A', 'category': 'attraction', 'choice': 'must', 'place': POINT, 'day': 2},
            {'id': 'b', 'name': 'B', 'category': 'attraction', 'choice': 'optional', 'place': POINT},
            {'id': 'c', 'name': 'C', 'category': 'attraction', 'choice': 'optional'}]
        state = {'plan': plan, 'action': 'generate_from_guide', 'trace': []}
        state.update(prepare_draft(state))
        state['format_attempts'] = 2
        with patch('agent.planning.planning_graph.generate_itinerary_proposal', side_effect=ValueError('bad')):
            result = itinerary_agent(state)
        planning_state = result['plan']['planning']
        days = planning_state['generation']['days']
        self.assertCountEqual([v['activity_id'] for d in days for v in d['visits']], ['a', 'b'])
        self.assertIn({'activity_id': 'a'}, days[1]['visits'])
        self.assertTrue(any(x['type'] == 'ordering_fallback' for x in planning_state['generation_issues']))
        self.assertTrue(any(x['target'] == 'c' for x in planning_state['generation_issues']))

    def test_needs_input_saves_partial_without_replacing_itinerary(self):
        plan = base_plan()
        partial = deepcopy(plan)
        partial['planning']['activities'] = [{'id': 'a', 'name': 'A'}]
        partial['itinerary'] = []
        exc = GuideNeedsInput('confirm location')
        exc.plan = partial
        with patch.object(planning_jobs, 'read_checkpoint', return_value=None), patch.object(planning_jobs, 'update_job'), patch.object(planning_jobs, 'commit_result') as commit, patch('service.travel.generation_jobs.run_planning_action', side_effect=exc):
            execute_job(plan['plan_id'], 'job', plan, {})
        self.assertEqual(commit.call_args.kwargs['needs_input'], 'confirm location')
        self.assertEqual(commit.call_args.args[3]['itinerary'], plan['itinerary'])
        self.assertEqual(commit.call_args.args[3]['planning']['activities'][0]['id'], 'a')


class RouteOptionsTests(TestCase):
    def test_generated_routes_include_all_modes_time_and_unknown_fares(self):
        from agent.planning.planning_graph import route_agent
        plan = {'itinerary': [
            {'item_id': 'a', 'category': 'attraction', 'location': 'A', 'place': POINT},
            {'item_id': 'b', 'category': 'attraction', 'location': 'B', 'place': POINT}],
            'routes': [{'origin_item_id': 'a', 'destination_item_id': 'b'}]}
        def compute(*args, travel_mode):
            return {'success': True, 'duration': '121s', 'fare': None if travel_mode != 'TRANSIT' else 2, 'legs': []}
        with patch('agent.planning.planning_graph.AMAP_API_KEY', 'test'), patch('agent.planning.planning_graph.amap_compute_route', side_effect=compute) as compute_route:
            result = route_agent({'plan': plan, 'action': 'generate_from_guide', 'trace': []})
        options = result['plan']['routes'][0]['route_options']
        self.assertEqual(set(options), {'WALK', 'TRANSIT', 'DRIVE'})
        self.assertTrue(all(option['duration_minutes'] == 3 for option in options.values()))
        self.assertEqual(options['WALK']['fare'], 0)
        self.assertEqual(options['TRANSIT']['fare'], 2)
        self.assertIsNone(options['DRIVE']['fare'])
        self.assertEqual(compute_route.call_count, 3)

    def test_batch_selection_uses_existing_route_options(self):
        from agent.planning.planning_graph import coordinator
        plan = base_plan()
        plan['planning']['stage'] = 'review'
        plan['routes'] = [{'origin_item_id': 'a', 'destination_item_id': 'b'}]
        with patch('agent.planning.planning_graph.update_route_mode', return_value=plan) as update:
            coordinator({'plan': plan, 'action': 'route_modes', 'payload': {'selections': [
                {'origin_item_id': 'a', 'destination_item_id': 'b', 'travel_mode': 'WALK'}]}})
        update.assert_called_once_with(plan, 'a', 'b', 'WALK')


class ReviewBatchTests(TestCase):
    def test_repeated_moves_resolve_changed_item_ids_and_apply_details(self):
        from agent.planning.planning_graph import coordinator
        plan = base_plan()
        plan['planning']['guide_auto'] = True
        plan['planning']['activities'] = [{'id': 'a', 'name': 'A', 'category': 'attraction', 'choice': 'must', 'place': POINT}]
        plan = planning.draft_itinerary(plan, {'days': [{'day': n, 'visits': [{'activity_id': 'a'}] if n == 1 else []} for n in range(1, 4)], 'assumptions': [], 'constraint_results': []})
        original = deepcopy(plan)
        item_id = next(x['item_id'] for x in plan['itinerary'] if x.get('reference_id') == 'a')
        operations = [
            {'action': 'move_activity', 'payload': {'item_id': item_id, 'target_day': 2}},
            {'action': 'move_activity', 'payload': {'item_id': item_id, 'target_day': 3}},
            {'action': 'update_activity', 'payload': {'activity_id': 'a', 'duration_minutes': 60, 'ticket_required': None, 'estimated_cost': 0, 'visit_tips': 'test', 'time_of_day': '11:00'}},
            {'action': 'update_budget', 'payload': {'budget': 1234}}]
        result = coordinator({'plan': plan, 'action': 'review_changes', 'payload': {'operations': operations}})['plan']
        visit = next(x for x in result['itinerary'] if x.get('reference_id') == 'a')
        self.assertEqual(visit['day'], 3)
        self.assertEqual(visit['duration_minutes'], 60)
        self.assertEqual(result['budget'], 1234)
        self.assertEqual(plan, original)
        with self.assertRaises(ValueError):
            coordinator({'plan': plan, 'action': 'review_changes', 'payload': {'operations': operations + [{'action': 'confirm', 'payload': {}}]}})
        self.assertEqual(plan, original)


class ReviewSaveTests(TestCase):
    def test_stale_browser_draft_is_rejected_before_execution(self):
        with patch.object(server, 'get_plan', return_value=({'budget': 2000}, None)), patch.object(server, 'run_planning_action') as run:
            with TestClient(server.create_app(Path('mcp_server.py'), 'test')) as client:
                response = client.post('/api/plans/p/planning/review_changes', json={'base_plan': {'budget': 1000}, 'operations': []})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    def test_save_returns_persisted_version_and_uses_atomic_commit(self):
        original = {'budget': 1000.0}
        persisted = {'budget': 1200.0, 'updated_at': 'new'}
        with patch.object(server, 'get_plan', side_effect=[(original, None), (persisted, None)]), patch.object(server, 'run_planning_action', return_value={'plan': {'budget': 1200}, 'trace': [], 'audit': {'issues': []}}), patch.object(planning_jobs, 'save_review_changes') as commit, patch.object(server, 'save_plan') as legacy:
            with TestClient(server.create_app(Path('mcp_server.py'), 'test')) as client:
                response = client.post('/api/plans/p/planning/review_changes', json={'base_plan': {'budget': 1000}, 'operations': []})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['plan'], persisted)
        commit.assert_called_once()
        legacy.assert_not_called()
