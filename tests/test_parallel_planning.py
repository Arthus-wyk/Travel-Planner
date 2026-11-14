import json
from copy import deepcopy
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from agent.planning_graph import route_agent
from service.travel.parallel import ordered_parallel
from service.travel.place_matching import search_and_match
from service.utils.request_log import context


class ParallelPlanningTests(TestCase):
    def test_worker_limit(self):
        first_wave = Barrier(4, timeout=3)
        lock = Lock()
        active = peak = started = 0

        def work():
            nonlocal active, peak, started
            with lock:
                active += 1
                started += 1
                peak = max(peak, active)
                first = started <= 4
            try:
                if first:
                    first_wave.wait()
            finally:
                with lock:
                    active -= 1

        self.assertEqual(ordered_parallel([work] * 9), [None] * 9)
        self.assertEqual(peak, 4)

    def test_routes_overlap_and_preserve_order_and_timeout_fallback(self):
        second_finished = Event()
        calls = []
        plan = {
            "itinerary": [{"item_id": name, "location": name, "category": "attraction"} for name in "ABC"],
            "routes": [{"origin_item_id": "A", "destination_item_id": "B"},
                       {"origin_item_id": "B", "destination_item_id": "C",
                        "mode_locked": True, "travel_mode": "DRIVE"}],
        }

        def compute(origin, destination, travel_mode):
            calls.append((origin, context.get().get("job_id"), context.get().get("origin_item_id")))
            if origin == "A":
                self.assertTrue(second_finished.wait(2), "second leg must run while the first is waiting")
                return {"success": False, "message": "timeout"}
            if travel_mode == "DRIVE":
                second_finished.set()
            return {"success": True, "duration": "120s" if travel_mode == "DRIVE" else "60s"}

        token = context.set({"job_id": "route-job"})
        try:
            with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch(
                "agent.planning_graph.amap_compute_route", side_effect=compute
            ):
                routes = route_agent({"plan": plan, "action": "draft", "trace": []})["plan"]["routes"]
        finally:
            context.reset(token)
        self.assertEqual([(r["origin_item_id"], r["duration_minutes"]) for r in routes], [("A", 30), ("B", 2)])
        self.assertEqual(routes[0]["source"], "provisional_estimate")
        self.assertEqual(routes[1]["travel_mode"], "DRIVE")
        self.assertCountEqual(calls, [("A", "route-job", "A")] * 3 + [("B", "route-job", "B")] * 3)

    def test_place_tasks_overlap_and_merge_aliases_in_activity_order(self):
        second_finished = Event()
        lodging_searched = Event()
        point = {"place_id": "poi", "name": "西湖", "address": "杭州西湖区", "longitude": 120, "latitude": 30}
        plan = {"destination": "杭州", "start_date": "2026-11-06", "end_date": "2026-11-08", "planning": {
            "activities": [{"id": "a", "name": "西湖", "note": "第一"},
                           {"id": "b", "name": "西湖", "note": "第二", "choice": "must"}],
            "guide_constraints": [{"activity_ids": ["b", "a"]}],
            "unscheduled_items": [{"reference_id": "b"}],
            "lodging_areas": [{"name": "湖滨"}],
        }}

        def chat(messages, **kwargs):
            position = json.loads(messages[1]["content"])["place_position"]
            return SimpleNamespace(tool_calls=[{"function": {
                "name": "search_map_places", "arguments": {"place_position": position, "query": "西湖"}}}])

        observed = []

        def search(query, **kwargs):
            if query == "湖滨 酒店":
                lodging_searched.set()
                raise TimeoutError("lodging request timed out")
            activity_id = context.get().get("activity_id")
            if activity_id == "b":
                second_finished.set()
            else:
                observed.append(second_finished.wait(2) and lodging_searched.wait(2))
            return {"success": True, "places": [point]}

        original = deepcopy(plan)
        with patch("service.travel.place_matching._chat", side_effect=chat), patch(
            "service.travel.place_matching._search", side_effect=search
        ):
            result = search_and_match(plan)["planning"]
        self.assertEqual(observed, [True])
        self.assertEqual(plan, original)
        self.assertEqual([(item["id"], item["note"], item["choice"]) for item in result["activities"]],
                         [("a", "第一；第二", "must")])
        self.assertEqual(result["guide_constraints"][0]["activity_ids"], ["a"])
        self.assertEqual(result["unscheduled_items"][0]["reference_id"], "a")
        self.assertEqual([event["position"] for event in result["place_events"]], [1, 1, 2, 2])
