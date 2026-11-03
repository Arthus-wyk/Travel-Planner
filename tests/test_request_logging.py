import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from service.utils import request_log
from service.utils.model_client import OpenAICompatibleClient


class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_compressed_response_is_readable(self):
        import gzip
        events = []
        body = gzip.compress(b'{"choices":[{"message":{"content":"decoded"}}]}')
        client = OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(
            lambda _: httpx.Response(200, headers={"content-encoding": "gzip"}, content=body)))
        with patch.object(request_log, "write_event", side_effect=lambda k, v: events.append((k, v))):
            await client.chat(model="test", messages=[], tools=[])
        self.assertIn("decoded", json.dumps(events[1][1]["body"]))

    def test_amap_request_response_and_business_error_are_logged(self):
        import io
        from email.message import Message
        from service.integrations import travel_amap as amap
        response = io.BytesIO(b'{"status":"0","info":"DENIED"}')
        response.headers = Message()
        response.headers["Content-Type"] = "application/json"
        events = []
        with patch.object(amap, "read_map_cache", return_value=None), patch.object(amap, "AMAP_API_KEY", "map-private"), patch.object(amap.urllib.request, "urlopen", return_value=response), patch.object(request_log, "write_event", side_effect=lambda k, v: events.append((k, v))), patch.object(amap, "write_event", side_effect=lambda k, v: events.append((k, v))):
            with self.assertRaisesRegex(RuntimeError, "DENIED"):
                amap.request_amap("/v3/place/text", {"keywords": "park"})
        self.assertEqual([k for k, _ in events], ["api.requests", "api.responses", "errors"])
        self.assertEqual(len({v["call_id"] for _, v in events}), 1)
        self.assertNotIn("map-private", json.dumps(events))

    async def test_ollama_logs_full_request_and_ndjson_response(self):
        from service.utils.model_client import OllamaClient
        events = []
        body = '{"message":{"role":"assistant","content":"hello"},"done":false}\n{"message":{"role":"assistant","content":"world"},"done":true}\n'
        client = OllamaClient(host="http://localhost:11434", transport=httpx.MockTransport(lambda _: httpx.Response(200, text=body)))
        try:
            with patch.object(request_log, "write_event", side_effect=lambda k, v: events.append((k, v))):
                stream = await client.chat(model="test", messages=[{"role": "user", "content": "input"}], stream=True)
                output = [chunk.message.content async for chunk in stream]
            self.assertEqual(output, ["hello", "world"])
            self.assertEqual(events[0][1]["url"], "http://localhost:11434/api/chat")
            self.assertEqual(len(events[1][1]["body"]), 2)
        finally:
            await client._client.aclose()

    async def test_cancellation_records_partial_response_and_error(self):
        events = []
        class CancelledStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'partial'
                raise asyncio.CancelledError()
        transport = request_log.LoggedTransport(httpx.MockTransport(lambda _: httpx.Response(200, stream=CancelledStream())))
        with patch.object(request_log, "write_event", side_effect=lambda k, v: events.append((k, v))):
            async with httpx.AsyncClient(transport=transport) as client:
                with self.assertRaises(asyncio.CancelledError):
                    await client.post("https://example.com", json={"messages": []})
        self.assertEqual(events[-1][1]["error_type"], "CancelledError")
        self.assertEqual(events[-2][1]["body"], "partial")

    async def test_inbound_api_logging_does_not_change_body(self):
        from service.api.request_logging import RequestLoggingMiddleware
        events, sent = [], []
        async def app(scope, receive, send):
            request = await receive()
            await send({"type": "http.response.start", "status": 400, "headers": []})
            await send({"type": "http.response.body", "body": request["body"]})
        async def receive():
            return {"type": "http.request", "body": b'{"hello":"world"}'}
        async def send(message):
            sent.append(message)
        with patch("service.api.request_logging.write_event", side_effect=lambda k, v: events.append((k, v))):
            await RequestLoggingMiddleware(app)({"type": "http", "method": "POST", "path": "/test", "headers": []}, receive, send)
        self.assertEqual(sent[-1]["body"], b'{"hello":"world"}')
        self.assertEqual([k for k, _ in events], ["api.requests", "api.responses", "errors"])
        self.assertEqual(events[0][1]["call_id"], events[1][1]["call_id"])

    async def test_websocket_messages_are_separate_and_correlated(self):
        from service.api.request_logging import RequestLoggingMiddleware
        events, sent = [], []
        async def app(scope, receive, send):
            await receive()
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.send", "text": '{"type":"heartbeat"}'})
            await send({"type": "websocket.close", "code": 1000})
        async def receive():
            return {"type": "websocket.connect"}
        async def send(message):
            sent.append(message)
        with patch("service.api.request_logging.write_event", side_effect=lambda k, v: events.append((k, v))):
            await RequestLoggingMiddleware(app)({"type": "websocket", "path": "/ws", "headers": []}, receive, send)
        self.assertEqual(len(sent), 3)
        self.assertEqual([k for k, _ in events], ["api.requests", "api.requests", "api.responses", "api.responses", "api.responses"])
        self.assertEqual(len({v["call_id"] for _, v in events}), 1)

    async def test_actual_model_payload_and_response_are_separate_and_redacted(self):
        events = []
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": {"content": "完整输出"}}], "usage": {"prompt_tokens": 3}})
        client = OpenAICompatibleClient("https://example.com/v1", "private-key", transport=httpx.MockTransport(handler))
        with patch.object(request_log, "write_event", side_effect=lambda kind, data: events.append((kind, data))):
            await client.chat(model="test", messages=[{"role": "user", "content": "完整输入"}], tools=[], format={"type": "object"})
        self.assertEqual([k for k, _ in events], ["model.requests", "model.responses"])
        req, res = [v for _, v in events]
        self.assertEqual(req["call_id"], res["call_id"])
        self.assertEqual(req["body"]["messages"][0]["content"], "完整输入")
        self.assertEqual(req["body"]["response_format"]["type"], "json_schema")
        self.assertEqual(res["body"]["choices"][0]["message"]["content"], "完整输出")
        self.assertNotIn("private-key", json.dumps(req))

    async def test_http_error_records_body_status_and_error(self):
        events = []
        client = OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(429, json={"error": "rate limited"})))
        with patch.object(request_log, "write_event", side_effect=lambda k, v: events.append((k, v))):
            with self.assertRaises(httpx.HTTPStatusError):
                await client.chat(model="test", messages=[], tools=[])
        self.assertEqual([k for k, _ in events], ["model.requests", "model.responses", "errors"])
        self.assertEqual(events[-1][1]["status"], 429)
        self.assertEqual(events[-1][1]["body"], {"error": "rate limited"})

    async def test_stream_keeps_all_chunks_and_network_errors(self):
        events = []
        class BrokenStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                raise httpx.ReadError("connection lost")
        client = OpenAICompatibleClient("https://example.com/v1", "key", transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=BrokenStream())))
        with patch.object(request_log, "write_event", side_effect=lambda k, v: events.append((k, v))):
            stream = await client.chat(model="test", messages=[], tools=[], stream=True)
            with self.assertRaises(httpx.ReadError):
                async for _ in stream:
                    pass
        self.assertIn("partial", json.dumps(events[-2][1]))
        self.assertEqual(events[-1][0], "errors")
        self.assertIn("ReadError", events[-1][1]["traceback"])

    def test_files_are_readable_separate_and_secrets_redacted(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(request_log, "LOG_DIR", Path(directory)):
            request_log.write_event("model.requests", {"messages": [{"content": "中文"}], "authorization": "Bearer secret", "url": "https://x?key=map-secret"})
            request_log.write_event("errors", {"message": "bad"})
            raw = (Path(directory) / "model.requests.log").read_text(encoding="utf-8")
            self.assertIn('\n  "', raw)
            self.assertIn("中文", raw)
            self.assertNotIn("map-secret", raw)
            self.assertNotIn("Bearer secret", raw)
            self.assertTrue((Path(directory) / "errors.log").exists())
            request_log.close_handlers()
