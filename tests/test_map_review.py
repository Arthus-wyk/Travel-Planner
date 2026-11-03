"""Map review behavior without MySQL or external API calls."""
import base64
from copy import deepcopy
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from agent.planning_graph import run_planning_action
from service.api import server
from service.travel import planning
from service.travel.map_planning import day_map, resolve_activity_place


def prepared_plan():
    plan = planning.build_candidates(planning.create_draft({
        "destination": "杭州", "departure_city": "上海", "start_date": "2026-11-06",
        "trip_days": 3, "travelers": 2, "budget": 9000,
    }, "map-test"))
    candidates = plan["planning"]["transport_candidates"]
    plan = planning.select_candidates(plan, {
        "outbound": next(x["id"] for x in candidates if x["direction"] == "outbound"),
        "return": next(x["id"] for x in candidates if x["direction"] == "return"),
        "hotel": plan["planning"]["hotel_candidates"][0]["id"],
    })
    for name in ("西湖", "灵隐寺", "雷峰塔"):
        plan = planning.add_manual_activity(plan, name, "杭州" + name)
    return plan


class MapReviewTests(unittest.TestCase):
    def test_recommendations_preserve_user_metadata_after_poi_rename(self):
        plan = prepared_plan()
        activity = plan["planning"]["activities"][0]
        activity.update(name="杭州西湖风景名胜区", choice="must", duration_minutes=180,
                        estimated_cost=80, ticket_required=True, visit_tips="提前预约",
                        place={"longitude": 120.1, "latitude": 30.2})
        with patch.object(planning, "analyze_guide", return_value={"text": "西湖", "places": [{"id": activity["id"], "name": "西湖", "source": "guide"}]}):
            changed = planning.build_activities(plan, "西湖")
        result = next(x for x in changed["planning"]["activities"] if x["id"] == activity["id"])
        for key in ("choice", "duration_minutes", "estimated_cost", "ticket_required", "visit_tips", "name", "place"):
            self.assertEqual(result[key], activity[key])

    def test_station_transfers_use_arrival_station_and_persist_on_redraft(self):
        plan = planning.draft_itinerary(prepared_plan())
        station = next(x for x in plan["itinerary"] if x["category"] == "intercity_transport")
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route", return_value={"success": True, "duration": "1200s", "distance_meters": 1000, "legs": [{"polyline": "120,30;120.1,30.1"}]}) as compute:
            changed = run_planning_action(plan, "transport_stops", {"item_id": station["item_id"], "departure_location": "上海虹桥站", "arrival_location": "杭州东站"})["plan"]
        self.assertTrue(any(c.args[0] == "杭州东站" for c in compute.call_args_list))
        incoming = next(r for r in changed["routes"] if r["destination_item_id"] == station["item_id"])
        self.assertEqual(incoming["destination"], "上海虹桥站")
        outgoing = next(r for r in changed["routes"] if r["origin_item_id"] == station["item_id"])
        self.assertEqual(outgoing["origin"], "杭州东站")
        self.assertEqual(outgoing["duration_minutes"], 20)
        redrafted = planning.draft_itinerary(changed)
        self.assertEqual(next(x for x in redrafted["itinerary"] if x["item_id"] == station["item_id"])["arrival_location"], "杭州东站")

    def test_pending_pool_survives_reload_and_moves_between_days(self):
        plan = planning.draft_itinerary(prepared_plan())
        original = next(x for x in plan["itinerary"] if x["category"] == "attraction")
        pending = planning.move_activity(plan, original["item_id"], None)
        self.assertEqual(pending["planning"]["unscheduled_items"][0]["reference_id"], original["reference_id"])
        self.assertFalse(any(x.get("reference_id") == original["reference_id"] for x in pending["itinerary"]))
        changed = planning.move_activity(deepcopy(pending), original["item_id"], 3)
        self.assertFalse(changed["planning"]["unscheduled_items"])
        moved = next(x for x in changed["itinerary"] if x.get("reference_id") == original["reference_id"])
        self.assertEqual(moved["day"], 3)
        self.assertFalse(changed["planning"].get("unplaced_activities"))
        self.assertTrue(any(r["destination_item_id"] == moved["item_id"] for r in changed["routes"]))
        self.assertEqual(len([x for x in changed["itinerary"] if x["category"] == "attraction"]), 3)
        with self.assertRaises(ValueError):
            planning.move_activity(plan, "guided-1-start", None)

    def test_route_mode_is_retained_after_reordering_and_real_time_reflow(self):
        plan = planning.draft_itinerary(prepared_plan())
        # A second-day hotel departure at 08:00 with a two-hour trip moves the visit to 10:00.
        route = next(r for r in plan["routes"] if r["origin_item_id"] == "guided-2-start")
        def route_result(origin, destination, travel_mode):
            return {"success": True, "duration": {"WALK": "7200s", "TRANSIT": "1800s", "DRIVE": "600s"}[travel_mode],
                    "distance_meters": 1000, "legs": [{"polyline": "120,30;120.1,30.1"}]}
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route", side_effect=route_result) as compute:
            result = run_planning_action(plan, "route_mode", {**route, "travel_mode": "WALK"})
        changed = result["plan"]
        visit = next(x for x in changed["itinerary"] if x["item_id"] == route["destination_item_id"])
        self.assertEqual(visit["time_of_day"], "10:00")
        self.assertTrue(any(call.kwargs["travel_mode"] == "WALK" for call in compute.call_args_list))
        self.assertEqual(set(next(r for r in changed["routes"] if r["origin_item_id"] == route["origin_item_id"])["route_options"]), {"WALK", "TRANSIT", "DRIVE"})
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route") as api:
            faster = run_planning_action(changed, "route_mode", {**route, "travel_mode": "DRIVE"})["plan"]
        api.assert_not_called()
        self.assertEqual(next(x for x in faster["itinerary"] if x["item_id"] == visit["item_id"])["time_of_day"], "09:30")
        self.assertEqual(next(r for r in faster["routes"] if r["origin_item_id"] == route["origin_item_id"])["travel_mode"], "DRIVE")
        again = planning.update_activity_details(changed, visit["reference_id"], {"duration_minutes": 90, "ticket_required": False, "estimated_cost": 0, "visit_tips": "", "time_of_day": "11:15"})
        self.assertEqual(next(x for x in again["itinerary"] if x["item_id"] == visit["item_id"])["time_of_day"], "11:15")
        self.assertEqual(next(r for r in again["routes"] if r["origin_item_id"] == "guided-2-start")["travel_mode"], "WALK")
        with self.assertRaises(ValueError):
            planning.update_route_mode(plan, route["origin_item_id"], route["destination_item_id"], "FLY")

    def test_failed_route_clears_old_geometry_and_flags_estimate(self):
        plan = planning.draft_itinerary(prepared_plan())
        route = plan["routes"][0]
        with patch("agent.planning_graph.AMAP_API_KEY", "test"), patch("agent.planning_graph.amap_compute_route", return_value={"success": False, "message": "No route"}):
            changed = run_planning_action(plan, "route_mode", {**route, "travel_mode": "WALK"})["plan"]
        failed = next(r for r in changed["routes"] if not r["intercity"])
        self.assertEqual(failed["legs"], [])
        self.assertEqual(failed["source"], "provisional_estimate")

    def test_resolve_selected_poi_preserves_stable_activity_identity(self):
        plan = prepared_plan()
        activity = plan["planning"]["activities"][0]
        with patch("service.travel.map_planning.request_amap", return_value={"pois": [{"id": "B1", "name": "杭州西湖风景区", "location": "120.1,30.2", "address": "西湖区", "cityname": "杭州市"}]}), patch("service.travel.map_planning.geocode", return_value=[{"location": "120.2,30.3"}]), patch("service.travel.map_planning.static_map", return_value={"image_cache_key": "a" * 64}):
            changed = resolve_activity_place(plan, activity["id"], "B1")
        selected = changed["planning"]["activities"][0]
        self.assertEqual(selected["id"], activity["id"])
        self.assertEqual(selected["place"]["longitude"], 120.1)
        self.assertEqual(selected["place"]["geocodes"][0]["location"], "120.2,30.3")
        drafted = planning.draft_itinerary(changed)
        item = next(x for x in drafted["itinerary"] if x.get("reference_id") == selected["id"])
        self.assertEqual(item["place"]["image_cache_key"], "a" * 64)

    def test_map_contains_planned_geometry_and_changes_with_day(self):
        plan = planning.draft_itinerary(prepared_plan())
        route = next(r for r in plan["routes"] if r["origin_item_id"] == "guided-2-start")
        route["legs"] = [{"polyline": "120.1,30.1;120.2,30.2"}]
        with patch("service.travel.map_planning.resolve_location", return_value={"location": "120.1,30.1"}), patch("service.travel.map_planning.static_map", return_value={"image_cache_key": "a" * 64}) as render:
            first = day_map(plan, 2)
            self.assertIn("120.2,30.2", first["paths"][0])
            second = day_map(plan, 3)
        self.assertNotEqual([x["item_id"] for x in first["points"]], [x["item_id"] for x in second["points"]])
        with self.assertRaises(ValueError):
            day_map(plan, 0)

    def test_dynamic_map_preserves_full_route_without_requesting_image(self):
        plan = planning.draft_itinerary(prepared_plan())
        route = next(r for r in plan["routes"] if r["origin_item_id"] == "guided-2-start")
        line = ";".join(f"{120 + n / 10000},30.2" for n in range(500))
        route["legs"] = [{"polyline": line}]
        with patch("service.travel.map_planning.resolve_location", return_value={"location": "120.1,30.2"}), patch("service.travel.map_planning.static_map") as render:
            result = day_map(plan, 2)
        self.assertIn(line, result["paths"])
        render.assert_not_called()

    def test_first_day_map_renders(self):
        plan = planning.draft_itinerary(prepared_plan())
        with patch("service.travel.map_planning.resolve_location", return_value={"location": "120.1,30.2"}), patch("service.travel.map_planning.static_map", return_value={"image_cache_key": "a" * 64}):
            self.assertTrue(day_map(plan, 1)["success"])

    def test_http_search_image_and_move_persist_without_image_payload_in_plan(self):
        plan = planning.draft_itinerary(prepared_plan())
        plans = {plan["plan_id"]: plan}
        with patch.object(server, "get_plan", side_effect=lambda key: (deepcopy(plans.get(key)), None)), patch.object(server, "save_plan", side_effect=lambda p: plans.update({p["plan_id"]: p})), patch.object(server, "amap_search_places", return_value={"success": True, "places": [{"place_id": "B1"}]}), patch.object(server, "read_map_cache", return_value="data:image/png;base64," + base64.b64encode(b"image").decode()), patch("agent.planning_graph.AMAP_API_KEY", ""):
            with TestClient(server.create_app(Path("mcp_server.py"), "test")) as client:
                result = client.post("/api/plans/map-test/planning/places/search", json={"query": "西湖"})
                self.assertEqual(result.json()["places"][0]["place_id"], "B1")
                self.assertEqual(client.post("/api/plans/map-test/planning/places/search", json={"query": ""}).status_code, 400)
                image = client.get("/api/maps/images/" + "a" * 64)
                self.assertEqual(image.content, b"image")
                self.assertEqual(image.headers["content-type"], "image/png")
                self.assertEqual(client.get("/api/maps/images/invalid").status_code, 404)
                item = next(x for x in plan["itinerary"] if x["category"] == "attraction")
                moved = client.post("/api/plans/map-test/planning/move_activity", json={"item_id": item["item_id"], "target_day": None})
                self.assertEqual(moved.status_code, 200)
                self.assertEqual(len(plans["map-test"]["planning"]["unscheduled_items"]), 1)
