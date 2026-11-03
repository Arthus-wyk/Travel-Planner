"""ASGI observation without consuming or replacing request/response streams."""
from time import perf_counter
from uuid import uuid4

from service.utils.request_log import body_value, context, log_error, write_event


class RequestLoggingMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            details = {"call_id": uuid4().hex, "protocol": "websocket", "path": scope["path"]}
            token = context.set({**context.get(), "request_id": details["call_id"]})
            write_event("api.requests", {**details, "event": "connect", "query": scope.get("query_string", b"").decode("latin-1"),
                                          "headers": {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}})
            async def ws_receive():
                message = await receive()
                write_event("api.requests", {**details, "message": message})
                return message
            async def ws_send(message):
                write_event("api.responses", {**details, "message": message})
                await send(message)
            try:
                return await self.app(scope, ws_receive, ws_send)
            except BaseException as exc:
                log_error(exc, **details)
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

        def log_request(complete):
            nonlocal request_logged
            if not request_logged:
                request_logged = True
                write_event("api.requests", {**details, "headers": {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]},
                                              "body": body_value(bytes(request_body)), "body_complete": complete})

        async def observed_receive():
            message = await receive()
            if message["type"] == "http.request":
                request_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    log_request(True)
            return message

        async def observed_send(message):
            nonlocal status, response_headers, response_complete
            if message["type"] == "http.response.start":
                status = message["status"]
                response_headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])}
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
                response_complete = not message.get("more_body", False)
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            log_error(exc, **details)
            raise
        finally:
            log_request(not request_body and scope["method"] in {"GET", "HEAD", "OPTIONS"})
            result = {**details, "status": status, "headers": response_headers,
                      "body": body_value(bytes(response_body)), "complete": response_complete,
                      "duration_ms": round((perf_counter() - started) * 1000, 2)}
            write_event("api.responses", result)
            if status is not None and status >= 400:
                write_event("errors", {**result, "message": "HTTP API error"})
            context.reset(token)
