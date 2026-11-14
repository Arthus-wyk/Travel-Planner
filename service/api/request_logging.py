"""ASGI observation without consuming or replacing request/response streams."""
from asyncio import to_thread
from time import perf_counter
from uuid import uuid4

from service.config import get_env_or_dotenv
from service.utils.request_log import body_value, context, log_error, write_event


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app
        self.capture_body = get_env_or_dotenv("TRAVEL_LOG_HTTP_BODY") == "1"

    async def __call__(self, scope, receive, send):
        # 日志查询不回写自身响应，避免刷新挤掉正在查看的历史诊断。
        if scope["type"] == "http" and scope.get("path", "").startswith("/api/logs/"):
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            details = {"call_id": uuid4().hex, "protocol": "websocket", "path": scope["path"]}
            token = context.set({**context.get(), "request_id": details["call_id"]})
            await to_thread(write_event, "api.requests", {**details, "event": "connect", "query": scope.get("query_string", b"").decode("latin-1"),
                                          "headers": {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}})
            async def ws_receive():
                message = await receive()
                await to_thread(write_event, "api.requests", {**details, "message": message})
                return message
            async def ws_send(message):
                if message["type"] == "websocket.send":
                    payload = message.get("text", "").encode() if "text" in message else message.get("bytes", b"")
                    record = {"type": message["type"], "body_bytes": len(payload)}
                    if self.capture_body and len(payload) <= 16_384:
                        record.update(message)
                else:
                    record = message
                await to_thread(write_event, "api.responses", {**details, "message": record})
                await send(message)
            try:
                return await self.app(scope, ws_receive, ws_send)
            except BaseException as exc:
                await to_thread(log_error, exc, **details)
                raise
            finally:
                context.reset(token)
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        details = {"call_id": uuid4().hex, "direction": "inbound", "method": scope["method"],
                   "path": scope["path"], "query": scope.get("query_string", b"").decode("latin-1")}
        token = context.set({**context.get(), "request_id": details["call_id"]})
        started = perf_counter()
        request_body, response_body = bytearray(), bytearray()
        request_logged = False
        response_complete = False
        response_headers = {}
        status = None
        request_bytes = response_bytes = 0

        def body_fields(body, size):
            fields = {"body_bytes": size}
            if self.capture_body:
                fields.update(body=body_value(bytes(body)) if size == len(body) else None,
                              body_truncated=size > len(body))
            return fields

        async def observed_receive():
            nonlocal request_bytes, request_logged
            message = await receive()
            if message["type"] == "http.request":
                chunk = message.get("body", b"")
                request_bytes += len(chunk)
                if self.capture_body and request_bytes <= 16_384:
                    request_body.extend(chunk)
                request_logged = not message.get("more_body", False)
            return message

        async def observed_send(message):
            nonlocal status, response_headers, response_complete, response_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])}
            elif message["type"] == "http.response.body":
                chunk = message.get("body", b"")
                response_bytes += len(chunk)
                if self.capture_body and response_bytes <= 16_384:
                    response_body.extend(chunk)
                response_complete = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            await to_thread(log_error, exc, **details)
            raise
        finally:
            elapsed = round((perf_counter() - started) * 1000, 2)
            def log_exchange():
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
                size = request_bytes
                if not request_logged and headers.get("content-length", "").isdigit():
                    size = int(headers["content-length"])
                write_event("api.requests", {**details, "headers": headers, **body_fields(request_body, size),
                                              "body_complete": request_logged or not size and scope["method"] in {"GET", "HEAD", "OPTIONS"}})
                result = {**details, "status": status, "headers": response_headers,
                          **body_fields(response_body, response_bytes), "complete": response_complete,
                          "duration_ms": elapsed}
                write_event("api.responses", result)
                if status is not None and status >= 400:
                    write_event("errors", {**result, "message": "HTTP API error"})
            try:
                await to_thread(log_exchange)
            finally:
                context.reset(token)
