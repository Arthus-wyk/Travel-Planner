import io
import json
import unittest
from email.message import Message
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from service.integrations import travel_amap as amap
from service.mcp import server


class AmapTests(unittest.TestCase):
    def setUp(self):
        self.cache = {}
        for item in (
            patch.object(amap, "read_map_cache", side_effect=self.cache.get),
            patch.object(amap, "write_map_cache", side_effect=lambda key, value, ttl: self.cache.update({key: value})),
        ):
            item.start()
            self.addCleanup(item.stop)

    def response(self, body, mime="application/json"):
        response = io.BytesIO(body)
        response.headers = Message()
        response.headers["Content-Type"] = mime
        return response

    def test_request_checks_amap_status_and_redacts_key(self):
        with patch.object(amap, "AMAP_API_KEY", "secret"), patch.object(amap.urllib.request, "urlopen", return_value=self.response(b'{"status":"0","infocode":"10001","info":"INVALID_USER_KEY"}')) as call:
            with self.assertRaisesRegex(RuntimeError, "10001"):
                amap.request_amap("/v3/place/text", {"keywords": "西湖"})
            self.assertEqual(parse_qs(urlsplit(call.call_args.args[0]).query)["key"], ["secret"])
            self.assertNotIn("secret", amap.amap_error(RuntimeError("url?key=secret"))["message"])

    def test_missing_key_and_network_errors(self):
        with patch.object(amap, "AMAP_API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "AMAP_API_KEY"):
                amap.request_amap("/v3/place/text", {})
        for error in (URLError("url?key=secret"), HTTPError("url?key=secret", 403, "Denied", {}, None)):
            with patch.object(amap, "AMAP_API_KEY", "secret"), patch.object(amap.urllib.request, "urlopen", side_effect=error):
                result = server.amap_geocode("杭州")
                self.assertFalse(result["success"])
                self.assertNotIn("secret", result["message"])

    def test_poi_payload_and_summary(self):
        args = ["西湖", 30, "zh-CN", "杭州", 30.2, 120.1, 5000]
        payload, error = amap.build_places_payload(*args)
        self.assertIsNone(error)
        with patch.object(amap, "request_amap", return_value={"pois": []}) as call:
            amap.search_places(payload)
            self.assertEqual(call.call_args.args[0], "/v3/place/around")
            self.assertEqual(payload["offset"], 20)
        args[5] = None
        self.assertFalse(amap.build_places_payload(*args)[1]["success"])
        place = amap.place_summary({"id": "B1", "name": "西湖", "pname": "浙江省", "cityname": "杭州市", "address": "西湖区", "location": "120.1,30.2", "biz_ext": {"rating": "4.8"}})
        self.assertEqual(place["latitude"], 30.2)
        self.assertEqual(place["rating"], 4.8)
        self.assertEqual(place["address"], "浙江省杭州市西湖区")

    def test_poi_cost_is_per_person_only(self):
        place = amap.place_summary({"id": "food", "name": "知味观", "location": "120.1,30.2",
                                    "biz_ext": {"cost": "68"}})
        self.assertEqual(place["cost_per_person"], 68.0)

    def test_geocoding_and_empty_result(self):
        with patch.object(amap, "request_amap", return_value={"geocodes": []}) as call:
            self.assertFalse(server.amap_geocode("西湖", "杭州")["success"])
            self.assertEqual(call.call_args.args[1], {"address": "西湖", "city": "杭州"})
        with patch.object(amap, "request_amap") as call:
            self.assertFalse(server.amap_geocode("")["success"])
            call.assert_not_called()

    def test_all_route_modes_normalize_distance_and_duration(self):
        for mode, endpoint in {"DRIVE": "driving", "WALK": "walking", "BICYCLE": "bicycling", "TWO_WHEELER": "electrobike", "TRANSIT": "transit/integrated"}.items():
            with self.subTest(mode=mode), patch.object(amap, "resolve_location", side_effect=[{"location": "120,30", "citycode": "0571"}, {"location": "121,31", "citycode": "021"}]), patch.object(amap, "request_amap", return_value={"route": {"transits" if mode == "TRANSIT" else "paths": [{"distance": "1500", "cost": {"duration": "600"}}]}}) as call:
                result = server.amap_compute_route("杭州", "上海", mode)
                self.assertTrue(result["success"])
                self.assertEqual((result["distance_meters"], result["duration"]), (1500, "600s"))
                self.assertEqual(call.call_args.args[0], "/v5/direction/" + endpoint)
                self.assertEqual(call.call_args.args[1]["show_fields"], "cost,polyline")
                if mode == "TRANSIT":
                    self.assertEqual(call.call_args.args[1]["city2"], "021")

    def test_route_exposes_only_returned_fares(self):
        with patch.object(amap, "resolve_location", return_value={"location": "120,30", "citycode": "0571"}), patch.object(amap, "request_amap", return_value={"route": {"transits": [{"distance": "1000", "cost": {"duration": "600", "transit_fee": "3"}}]}}):
            route = server.amap_compute_route("a", "b", "TRANSIT")
        self.assertEqual(route["fare"], 3.0)
        with patch.object(amap, "resolve_location", return_value={"location": "120,30", "citycode": "0571"}), patch.object(amap, "request_amap", return_value={"route": {"paths": [{"distance": "1000", "cost": {"duration": "300"}}], "taxi_cost": "16"}}):
            route = server.amap_compute_route("a", "b", "DRIVE")
        self.assertEqual(route["fare"], 16.0)

    def test_invalid_fare_does_not_hide_valid_route(self):
        with patch.object(amap, "resolve_location", return_value={"location": "120,30", "citycode": "0571"}), patch.object(amap, "request_amap", return_value={"route": {"transits": [{"distance": "1000", "cost": {"duration": "600", "transit_fee": "[]"}}]}}):
            route = server.amap_compute_route("a", "b", "TRANSIT")
        self.assertTrue(route["success"])
        self.assertIsNone(route["fare"])

    def test_shortest_path_within_one_mode_is_returned(self):
        with patch.object(amap, "resolve_location", return_value={"location": "120,30", "citycode": "0571"}), patch.object(amap, "request_amap", return_value={"route": {"paths": [
            {"distance": "1000", "cost": {"duration": "900"}},
            {"distance": "1300", "cost": {"duration": "600"}}]}}):
            route = server.amap_compute_route("a", "b", "WALK")
        self.assertEqual((route["duration"], route["distance_meters"]), ("600s", 1300))

    def test_coordinates_and_transit_city_resolution(self):
        with patch.object(amap, "request_amap", side_effect=[{"regeocode": {"addressComponent": {"citycode": "0571"}}}, {"regeocode": {"addressComponent": {"citycode": "0571"}}}, {"route": {"transits": []}}]) as call:
            result = server.amap_compute_route("120,30", "120.1,30.1", "TRANSIT")
            self.assertFalse(result["success"])
            self.assertEqual(call.call_args.args[1]["city1"], "0571")
        for coordinate in ("181,30", "120,91", "nan,30"):
            with self.assertRaises(ValueError):
                amap.coordinates(coordinate)

    def test_avoidance_and_missing_duration(self):
        self.assertFalse(server.amap_compute_route("a", "b", avoid_ferries=True)["success"])
        payload, error, *_ = amap.build_route_payload("a", "b", "DRIVE", "zh-CN", "METRIC", True, True, False)
        self.assertIsNone(error)
        self.assertEqual(payload["strategy"], 43)
        with patch.object(amap, "resolve_location", return_value={"location": "120,30"}), patch.object(amap, "request_amap", return_value={"route": {"paths": [{"distance": "100"}]}}):
            self.assertFalse(server.amap_compute_route("a", "b")["success"])

    def test_static_map_image_and_invalid_inputs(self):
        with patch.object(amap, "AMAP_API_KEY", "secret"), patch.object(amap.urllib.request, "urlopen", return_value=self.response(b"png-bytes", "image/png")) as call:
            result = server.amap_static_map("120,30", markers=["120.1,30.1"])
            self.assertEqual(result["image_data_url"], "data:image/png;base64,cG5nLWJ5dGVz")
            self.assertNotIn("secret", json.dumps(result))
            params = parse_qs(urlsplit(call.call_args.args[0]).query)
            self.assertEqual(params["size"], ["600*400"])
            self.assertIn("120.100000,30.100000", params["markers"][0])
        for arguments in ({"zoom": 18}, {"width": 1025}, {"height": True}, {"markers": ["120,30"] * 11}):
            with self.subTest(arguments=arguments), patch.object(amap, "request_amap") as call:
                self.assertFalse(server.amap_static_map("120,30", **arguments)["success"])
                call.assert_not_called()
        self.cache.clear()
        with patch.object(amap, "AMAP_API_KEY", "secret"), patch.object(amap.urllib.request, "urlopen", return_value=self.response(b'{"status":"0","info":"DENIED"}')):
            self.assertFalse(server.amap_static_map("120,30")["success"])

    def test_identical_requests_reuse_persisted_cache(self):
        with patch.object(amap, "AMAP_API_KEY", "secret"), patch.object(amap.urllib.request, "urlopen", return_value=self.response(b'{"status":"1","geocodes":[{"location":"120,30"}]}')) as network:
            first = amap.geocode("西湖", "杭州")
            second = amap.geocode("西湖", "杭州")
            self.assertEqual(first, second)
            network.assert_called_once()
            self.assertNotIn("secret", json.dumps(self.cache))
