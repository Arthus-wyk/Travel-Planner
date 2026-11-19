import asyncio
import json
import inspect
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from agent.chat.review.children import create_review_handlers
from agent.chat.runtime.execution import ReviewExecution
from agent.tools.retry import ToolBudget
from agent.chat.schemas.review import PARENT_SCHEMA, child_schema, read_operations
from agent.chat.review.parent import ReviewDelegations, run_parent
from agent.chat.schemas.tasks import TaskContext, TaskResult, TaskSpec
from service.utils.model_client import OpenAICompatibleClient
from service.travel.review_public_reply import format_review_reply
from agent.tools.schemas.outputs import ToolResult, legacy_result
from agent.tools.implementations.review import review_context_for_day
from agent.tools.implementations.maps import service as map_tools
from service.mcp import server as map_business


PLACE = {"place_id": "park", "name": "公园"}


class CompactMapToolsTest(unittest.TestCase):
    def test_search_tools_preserve_usable_candidates_without_raw_data(self):
        place = {**PLACE, "address": "台北市大安区", "latitude": 25.03, "longitude": 121.53,
                 "coordinate_system": "GCJ-02", "types": ["公园"], "rating": 4.5,
                 "cost_per_person": 0, "amap_uri": "https://example.com", "adcode": "710108",
                 "user_rating_count": None, "price_level": None, "business_status": None}
        raw = {"success": True, "query": "公园", "places": [place], "raw_places": [{"blob": "x" * 5000}]}
        for name in ("amap_search_places", "amap_search_hotels"):
            with self.subTest(name=name):
                result = map_tools.compact_map_result(name, raw)
                self.assertNotIn("raw_places", result)
                self.assertNotIn("amap_uri", result["places"][0])
                self.assertNotIn("price_level", result["places"][0])
                self.assertEqual(result["places"][0]["cost_per_person"], 0)
                self.assertEqual(result["places"][0]["longitude"], 121.53)
        self.assertIn("raw_places", raw)

    def test_route_tool_keeps_time_distance_and_unknown_fare(self):
        raw = {"success": True, "origin": "a", "destination": "b", "travel_mode": "TRANSIT",
               "distance_meters": 2000, "duration": "600s", "fare": None,
               "legs": [{"blob": "x" * 5000}], "raw_routes": ["x" * 5000],
               "static_duration": None, "localized_values": None}
        result = map_tools.compact_map_result("amap_compute_route", raw)
        self.assertEqual(set(result), {"success", "origin", "destination", "travel_mode", "distance_meters", "duration", "fare"})
        self.assertIsNone(result["fare"])
        self.assertIn("legs", raw)

    def test_geocode_and_static_map_use_compact_addresses_and_image_reference(self):
        geocode = {"success": True, "geocodes": [{"formatted_address": "台北市", "location": "121.5,25.0",
                   "province": "台湾省", "city": "台北市", "district": "大安区", "raw": "x" * 5000}],
                   "coordinate_system": "GCJ-02", "message": "OK"}
        result = map_tools.compact_map_result("amap_geocode", geocode)
        self.assertNotIn("raw", result["geocodes"][0])
        self.assertEqual(result["geocodes"][0]["location"], "121.5,25.0")
        image = {"success": True, "center": "121.5,25.0", "coordinate_system": "GCJ-02",
                 "image_data_url": "data:image/png;base64," + "x" * 5000, "image_cache_key": "a" * 64}
        result = map_tools.compact_map_result("amap_static_map", image)
        self.assertNotIn("image_data_url", result)
        self.assertEqual(result["image_url"], "/api/maps/images/" + "a" * 64)

    def test_errors_preserve_diagnostics_and_registered_signatures(self):
        failure = {"success": False, "message": "too many requests", "http_status": 429,
                   "infocode": "10003", "retry_after": 1}
        for name in ("amap_search_places", "amap_search_hotels", "amap_compute_route", "amap_geocode", "amap_static_map"):
            self.assertEqual(map_tools.compact_map_result(name, failure), failure)
            self.assertEqual(inspect.signature(getattr(map_tools, name)), inspect.signature(getattr(map_business, name)))

    def test_registered_wrappers_apply_projection_and_business_calls_keep_route_details(self):
        place = {**PLACE, "latitude": 25, "longitude": 121, "amap_uri": "https://example.com"}
        with patch("service.mcp.server.search_places", return_value=[{}]), patch(
            "service.mcp.server.place_summary", return_value=place
        ):
            for name in ("amap_search_places", "amap_search_hotels"):
                result = getattr(map_tools, name)("台北")
                self.assertNotIn("raw_places", result)
                self.assertNotIn("amap_uri", result["places"][0])
            self.assertIn("raw_places", map_business.amap_search_places("台北"))
        route = {"distanceMeters": 100, "duration": "60s", "legs": [{"polyline": "121,25;122,25"}]}
        with patch("service.mcp.server.compute_route", return_value=[route]):
            self.assertNotIn("legs", map_tools.amap_compute_route("a", "b"))
            self.assertIn("legs", map_business.amap_compute_route("a", "b"))



class ReviewContextTest(unittest.TestCase):
    def setUp(self):
        self.snapshot = {
            "plan_version": "v1", "stage": "review", "destination": "台北",
            "start_date": "2027-01-01", "end_date": "2027-01-02",
            "itinerary": [
                {"item_id": "a", "reference_id": "activity-a", "day": 1, "day_offset": 1,
                 "time_of_day": "00:30", "title": "公园", "duration_minutes": 60,
                 "location": "台北市大安区", "category": "attraction", "estimated_cost": 25,
                 "visit_tips": "预约入场", "ticket_required": False,
                 "place": {"name": "公园", "raw": "x" * 5000},
                 "notes": "x" * 5000},
                {"item_id": "b", "day": 1, "title": "酒店", "anchor_role": "end", "category": "hotel_anchor"},
                {"item_id": "c", "day": 2, "title": "餐厅"},
            ],
            "routes": [
                {"origin_item_id": "a", "destination_item_id": "b", "travel_mode": "TRANSIT",
                 "duration": "1200s", "duration_minutes": 20, "fare": 5, "distance_meters": 2000,
                 "legs": [{"raw": "x" * 5000}], "route_options": ["x" * 5000]},
                {"origin_item_id": "b", "destination_item_id": "c", "duration_minutes": 30},
            ],
            "audit": {
                "status": "needs_attention", "metrics": {"budget": 1000}, "suggestions": ["x" * 5000],
                "issues": [
                    {"type": "conflict", "severity": "high", "message": "时间冲突", "target": "activity-a", "raw": "x" * 5000},
                    {"type": "budget", "severity": "medium", "message": "费用待确认", "target": None},
                    {"type": "missing_route", "severity": "high", "message": "第二天缺路线", "target": "c"},
                ],
                "budget": {"items": [{"raw": "x" * 5000}], "estimated_total": 300, "remaining": 700,
                           "paid_total": 100, "has_unknown_ticket_costs": True, "has_unknown_hotel_costs": False,
                           "has_unknown_transport_costs": True, "has_unknown_food_costs": False},
            },
        }

    def test_full_and_day_context_keep_only_decision_fields(self):
        before = deepcopy(self.snapshot)
        for day in (None, 1):
            with self.subTest(day=day):
                result = review_context_for_day(self.snapshot, day)
                item = result["itinerary"][0]
                self.assertNotIn("place", item)
                self.assertNotIn("notes", item)
                self.assertEqual(item["reference_id"], "activity-a")
                self.assertEqual(item["estimated_cost"], 25)
                self.assertIs(item["ticket_required"], False)
                self.assertEqual(item["day_offset"], 1)
                self.assertNotIn("legs", result["routes"][0])
                self.assertNotIn("route_options", result["routes"][0])
                self.assertNotIn("raw", result["audit"]["issues"][0])
                self.assertEqual(result["audit"]["budget"]["limit"], 1000)
                self.assertNotIn("items", result["audit"]["budget"])
                self.assertNotIn("metrics", result["audit"])
                self.assertEqual(result["start_date"], "2027-01-01")
                self.assertEqual(review_context_for_day(result, day), result)
                self.assertLess(len(json.dumps(result)), len(json.dumps(self.snapshot)) / 5)
        self.assertEqual(self.snapshot, before)

    def test_day_scope_and_unknown_budget_keep_their_meaning(self):
        result = review_context_for_day(self.snapshot, 1)
        self.assertEqual([item["item_id"] for item in result["itinerary"]], ["a", "b"])
        self.assertEqual(len(result["routes"]), 1)
        self.assertEqual(len(result["audit"]["issues"]), 2)
        self.assertTrue(result["audit"]["budget"]["has_unknown_ticket_costs"])
        self.assertEqual(result["audit"]["budget"]["estimated_total"], 300)
        self.snapshot["audit"]["budget"] = None
        self.assertIsNone(review_context_for_day(self.snapshot, 9)["audit"]["budget"])


class ToolBudgetTest(unittest.TestCase):
    def test_three_retries_are_free_and_success_resets_failure_chain(self):
        budget = ToolBudget(8)
        for operation in range(8):
            self.assertTrue(budget.begin("search", {"q": f"{operation}-0"})[0])
            budget.finish("search", False)
            for retry in range(3):
                self.assertTrue(budget.begin("search", {"q": f"{operation}-{retry + 1}"})[0])
                budget.finish("search", retry == 2)
        self.assertEqual(budget.remaining, 0)
        self.assertFalse(budget.begin("search", {"q": "extra"})[0])

    def test_four_failures_end_chain_and_identical_retries_are_rejected(self):
        budget = ToolBudget(1)
        self.assertTrue(budget.begin("search", {"q": "a"})[0])
        budget.finish("search", False)
        allowed, reason = budget.begin("search", {"q": "a"})
        self.assertFalse(allowed)
        self.assertIn("调整", reason)
        for query in ("b", "c", "d"):
            self.assertTrue(budget.begin("search", {"q": query})[0])
            budget.finish("search", False)
        self.assertFalse(budget.begin("search", {"q": "e"})[0])


class ChildRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_unvalidated_route_does_not_enter_review_evidence(self):
        responses = iter([
            SimpleNamespace(content="", tool_calls=[{"function": {"name": "compare_review_route",
                "arguments": {"origin_item_id": "a", "destination_item_id": "b", "travel_mode": "WALK"}}}]),
            SimpleNamespace(content=json.dumps({"status": "needs_replan", "reason": "路线不可用",
                                                "data": {"message": "", "remaining": ["补查路线"]}}), tool_calls=[]),
        ])

        async def model(**kwargs):
            return SimpleNamespace(message=next(responses)), kwargs["messages"], None

        read = AsyncMock(return_value=legacy_result("compare_review_route", True, {}))
        tools = [{"function": {"name": "compare_review_route", "parameters": {"type": "object"}}}]
        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            handlers = create_review_handlers(None, "mock", tools, {}, ReviewExecution(1))
            result = await handlers["audit_route"](TaskSpec("route", "audit_route", {}), {}, TaskContext({}, read))
        read.assert_awaited_once()
        self.assertEqual(result.data["routes"], [])
        self.assertEqual(result.data["remaining"], ["补查路线"])

    async def run_child(self, decisions, read, *, limit=8, steps=32):
        self.requests = []
        replies = iter(decisions)

        async def model(**kwargs):
            self.requests.append(kwargs)
            decision = next(replies)
            if decision == "timeout":
                await asyncio.Event().wait()
            if decision == "context_error":
                raise ValueError("Agent must supply a context compressor")
            calls = decision.get("tool_calls", [])
            response = SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(self.complete_result(decision.get("result", {}))), tool_calls=calls))
            return response, kwargs["messages"], None

        tools = [{"type": "function", "function": {"name": "search_review_places", "parameters": {"type": "object"}}}]
        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            handler = create_review_handlers(None, "mock", tools, {"type": "array"},
                                             ReviewExecution(0.2), max_tools=limit, max_steps=steps)
            return await handler["search_places"](TaskSpec("s", "search_places", {}), {}, TaskContext({}, read))

    @staticmethod
    def call(query, retry_of=None):
        arguments = {"query": query}
        if retry_of is not None:
            arguments["_retry_of"] = retry_of
        return {"tool_calls": [{"function": {"name": "search_review_places", "arguments": arguments}}]}

    @staticmethod
    def complete_result(result):
        if "status" in result:
            result = {**result, "data": {"message": "", "remaining": [], "place_ids": [], **result.get("data", {})}}
        return result

    async def test_error_reaches_model_then_adjusted_retry_preserves_normal_budget(self):
        seen = []

        async def read(name, args):
            seen.append(args["query"])
            return (legacy_result(name, False, message="城市不明确", error_type="validation_error")
                    if len(seen) == 1 else legacy_result(name, True, {"places": [PLACE]}))

        result = await self.run_child([self.call("公园"), self.call("杭州公园", retry_of="op-1"), self.call("杭州博物馆"),
                                     {"result": {"status": "completed", "data": {"place_ids": ["park"]}, "reason": ""}}],
                                    read, limit=2)
        self.assertEqual(result.status, "completed")
        self.assertEqual(seen, ["公园", "杭州公园", "杭州博物馆"])
        self.assertIn("城市不明确", json.dumps(self.requests[1]["messages"], ensure_ascii=False))
        self.assertIsInstance(self.requests[0]["response_format"], dict)

    async def test_timeout_and_step_limit_preserve_verified_places(self):
        async def read(name, args):
            return legacy_result(name, True, {"places": [PLACE]})

        for decisions, steps in (([self.call("公园"), "timeout"], 32),
                                 ([self.call("公园"), "context_error"], 32),
                                 ([self.call("公园"), {"result": {"status": "needs_replan", "data": {}, "reason": "尚需餐厅"}}], 1)):
            with self.subTest(steps=steps):
                result = await self.run_child(decisions, read, steps=steps)
                self.assertEqual(result.data["places"], [PLACE])
                self.assertTrue(result.data["remaining"])

    async def test_last_budget_call_still_gets_free_retries_and_finalization(self):
        calls = []

        async def read(name, args):
            calls.append(args)
            return (legacy_result(name, False, message="需补充搜索区域", error_type="validation_error")
                    if len(calls) < 4 else legacy_result(name, True, {"places": [PLACE]}))

        result = await self.run_child([*(self.call(f"区域{i}公园", retry_of="op-1" if i else None) for i in range(4)),
            {"result": {"status": "needs_replan", "data": {}, "reason": "仍需餐厅"}}], read, limit=1)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result.data["places"], [PLACE])
        self.assertEqual(self.requests[-1]["tools"], [])

    async def test_route_results_survive_model_context_failure(self):
        responses = iter([
            SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[{"function": {
                "name": "compare_review_route", "arguments": {"origin_item_id": "a", "destination_item_id": "b", "travel_mode": "WALK"}}}])),
            ValueError("context too long"),
        ])

        async def model(**kwargs):
            reply = next(responses)
            if isinstance(reply, Exception):
                raise reply
            return reply, kwargs["messages"], None

        async def read(name, arguments):
            return legacy_result(name, True, {"origin": "西湖", "destination": "灵隐寺",
                                              "travel_mode": "WALK", "duration": "600s"})

        tools = [{"type": "function", "function": {"name": "compare_review_route", "parameters": {"type": "object"}}}]
        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            handlers = create_review_handlers(None, "mock", tools, {}, ReviewExecution(1))
            result = await handlers["audit_route"](TaskSpec("route", "audit_route", {}), {}, TaskContext({}, read))
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.data["routes"][0]["result"], {
            "origin": "西湖", "destination": "灵隐寺", "travel_mode": "WALK", "duration": "600s",
        })
        self.assertTrue(result.data["remaining"])

    async def test_concurrent_children_have_independent_budgets(self):
        calls = []

        async def read(name, args):
            calls.append(args)
            return legacy_result(name, True, {"places": [PLACE]})

        async def model(**kwargs):
            queried = any(message["role"] == "tool" for message in kwargs["messages"])
            reply = ({"status": "completed", "data": {"place_ids": ["park"]}, "reason": ""}
                     if queried else {})
            response = SimpleNamespace(message=SimpleNamespace(content=json.dumps(self.complete_result(reply)),
                tool_calls=[] if queried else self.call("公园")["tool_calls"]))
            return response, kwargs["messages"], None

        tools = [{"type": "function", "function": {"name": "search_review_places", "parameters": {"type": "object"}}}]
        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            handler = create_review_handlers(None, "mock", tools, {"type": "array"}, ReviewExecution(1), max_tools=1)
            results = await asyncio.gather(*(handler["search_places"](
                TaskSpec(str(i), "search_places", {}), {}, TaskContext({}, read)) for i in range(2)))
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(r.status == "completed" for r in results))


class StructuredTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_map_mcp_registration_preserves_all_input_schemas(self):
        from mcp.server.lowlevel import Server
        from mcp.shared.memory import create_connected_server_and_client_session
        from agent.tools.catalog import register_all

        mcp = Server("compact-map-test")
        register_all(mcp)
        async with create_connected_server_and_client_session(mcp) as session:
            definitions = [tool for tool in (await session.list_tools()).tools if tool.name.startswith("amap_")]
        self.assertEqual(len(definitions), 5)
        for definition in definitions:
            expected = set(inspect.signature(getattr(map_business, definition.name)).parameters)
            self.assertEqual(set(definition.inputSchema["properties"]), expected)

    async def test_public_reply_context_error_returns_preserved_fallback(self):
        with patch("service.travel.review_public_reply.request_model_with_retry",
                   side_effect=ValueError("Agent must supply a context compressor")):
            result = await format_review_reply(None, "test", ReviewExecution(1),
                {"状态": "partial"}, "已找到公园，仍需核实餐厅。", set())
        self.assertEqual(result, "已找到公园，仍需核实餐厅。")

    async def test_refusal_truncation_and_invalid_schema_result_are_explicit_errors(self):
        schema = {"type": "object", "properties": {"value": {"type": "string"}},
                  "required": ["value"], "additionalProperties": False}
        for choice in ({"message": {"refusal": "refused"}},
                       {"message": {"content": "{}"}, "finish_reason": "length"},
                       {"message": {"content": "{}"}, "finish_reason": "stop"}):
            client = OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"choices": [choice]})))
            with self.assertRaises(ValueError):
                await client.chat(model="test", messages=[], tools=[], format=schema)

    def test_review_schemas_close_every_object_and_require_all_properties(self):
        from agent.tools.schemas.review import _BATCH

        def check(schema):
            if isinstance(schema, list):
                for item in schema:
                    check(item)
            elif isinstance(schema, dict):
                self.assertNotIn("oneOf", schema)
                if schema.get("type") == "object":
                    self.assertIs(schema.get("additionalProperties"), False)
                    self.assertEqual(set(schema["required"]), set(schema["properties"]))
                for value in schema.values():
                    if isinstance(value, (list, dict)):
                        check(value)

        for schema in (PARENT_SCHEMA, *(child_schema(role, _BATCH["properties"]["operations"])
                       for role in ("audit_route", "search_places", "build_edits"))):
            check(schema)

    def test_operation_optional_nulls_restore_existing_business_contract(self):
        from agent.tools.schemas.review import _BATCH

        operations = [{"action": "review_place", "payload": {
            "item_id": "a", "poi_id": "p", "category": "attraction", "duration_minutes": None,
            "visit_tips": None, "replace": None,
        }}]
        self.assertEqual(read_operations(operations, _BATCH["properties"]["operations"])[0]["payload"],
                         {"item_id": "a", "poi_id": "p", "category": "attraction"})


class ParentRecoveryTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def delegate(task_id, task, retry_of=None):
        arguments = {"task_id": task_id, "role": "search_places", "task": task,
                     "search_scope": {"category": "attraction", "day": None, "selection_count": None},
                     "extra_data": {"facts": "", "artifact_refs": []}}
        if retry_of:
            arguments["_retry_of"] = retry_of
        return {"function": {"name": "delegate_task", "arguments": arguments}}

    async def test_parent_retries_have_independent_budget_and_require_adjustment(self):
        calls = []

        async def handler(task, dependencies, context):
            calls.append(task.input)
            return TaskResult("failed", reason="地点不明确") if len(calls) < 4 else TaskResult("completed")

        delegations = ReviewDelegations({"search_places": handler}, TaskContext({}, None))
        decisions = iter([
            [self.delegate("search", "条件0")],
            [self.delegate("search", "条件0", "op-1")],
            *([self.delegate("search", f"条件{i}", "op-1")] for i in range(1, 4)),
            [self.delegate("extra", "另一个任务")],
            [],
        ])
        requests = []

        async def model(**kwargs):
            requests.append(deepcopy(kwargs))
            response = SimpleNamespace(message=SimpleNamespace(
                content=json.dumps({"kind": "answer", "message": "结果", "question": None,
                                    "options": [], "candidate_groups": []}), tool_calls=next(decisions)))
            return response, kwargs["messages"], None

        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            await run_parent(client=None, model="mock", request_context={}, snapshot={}, delegations=delegations,
                             max_steps=12, max_tools=1, execution=ReviewExecution(1))
        self.assertEqual(delegations.results["search"].status, "completed")
        self.assertEqual(len(calls), 4)
        self.assertNotIn("extra", delegations.results)
        feedback = json.dumps([request["messages"] for request in requests], ensure_ascii=False)
        self.assertIn("调整", feedback)
        self.assertIn("预算", feedback)

    async def test_parent_timeout_preserves_child_places_routes_and_remaining(self):
        async def handler(task, dependencies, context):
            return TaskResult("failed", {"places": [PLACE], "routes": [{"duration": "600s"}],
                                         "remaining": ["补齐餐厅"]}, "步骤上限")

        delegations = ReviewDelegations({"search_places": handler}, TaskContext({}, None))
        requested = False

        async def model(**kwargs):
            nonlocal requested
            if requested:
                await asyncio.Event().wait()
            requested = True
            response = SimpleNamespace(message=SimpleNamespace(content="", tool_calls=[self.delegate("search", "搜索")]))
            return response, kwargs["messages"], None

        with patch("agent.chat.runtime.nodes.llm.request_model_with_retry", model):
            decision = await run_parent(client=None, model="mock", request_context={}, snapshot={}, delegations=delegations,
                                        max_steps=4, execution=ReviewExecution(0.03))
        self.assertEqual(decision.kind, "partial")
        self.assertEqual(decision.places, [PLACE])
        self.assertEqual(decision.progress["routes"], [{"duration": "600s"}])
        self.assertEqual(decision.progress["remaining"], ["补齐餐厅"])


if __name__ == "__main__":
    unittest.main()
