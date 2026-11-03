"""Readable, correlated wire logs; independent of chat transcript lifetime."""
import base64
import json
import logging
import traceback
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from time import perf_counter
from uuid import uuid4

import httpx

from service.utils.jsonl_log import to_jsonable
from service.utils.runtime_log import current_run, redact

LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
context = ContextVar("request_log_context", default={})
_handlers = {}
_lock = RLock()


def close_handlers():
    with _lock:
        for handler in _handlers.values():
            handler.close()
        _handlers.clear()


def write_event(kind, data):
    # One record per block, with embedded JSON expanded for easy reading.
    def readable(value):
        if isinstance(value, dict):
            return {k: readable(v) for k, v in value.items()}
        if isinstance(value, list):
            return [readable(v) for v in value]
        if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                return readable(json.loads(value))
            except ValueError:
                pass
        return value
    try:
        run = current_run.get()
        record = {"timestamp": datetime.now(timezone.utc).isoformat(), **context.get(),
                  "run_id": run.run_id if run else None, **data}
        body = json.dumps(readable(redact(to_jsonable(record))), ensure_ascii=False, indent=2)
        with _lock:
            path = LOG_DIR / (kind + ".log")
            if path not in _handlers:
                LOG_DIR.mkdir(parents=True, exist_ok=True)
                _handlers[path] = RotatingFileHandler(path, maxBytes=20_000_000, backupCount=5, encoding="utf-8")
            _handlers[path].emit(logging.LogRecord(kind, logging.INFO, "", 0, body + "\n", (), None))
    except Exception:
        logging.getLogger(__name__).exception("Unable to write request diagnostic log")


def body_value(body, headers=None):
    if headers and headers.get("content-encoding"):
        try:
            body = httpx.Response(200, headers=headers, content=body).content
        except (httpx.DecodingError, ValueError):
            pass
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return {"encoding": "base64", "data": base64.b64encode(body).decode("ascii")}
    try:
        return json.loads(text)
    except ValueError:
        lines = [line.removeprefix("data:").strip() for line in text.splitlines() if line.strip()]
        try:
            return [json.loads(line) if line != "[DONE]" else line for line in lines] if lines else text
        except ValueError:
            return text


def instrument_client(client):
    # Preserve HTTPX's configured proxy mounts, TLS and connection limits.
    client._transport = LoggedTransport(client._transport)
    for pattern, transport in client._mounts.items():
        if transport is not None:
            client._mounts[pattern] = LoggedTransport(transport)
    return client


def log_error(exc, **fields):
    write_event("errors", {**fields, "error_type": type(exc).__name__, "message": str(exc),
                           "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))})


class LoggedTransport(httpx.AsyncBaseTransport):
    """Observe actual serialized HTTP requests and all response bytes, including streams."""
    def __init__(self, transport):
        self.transport = transport

    async def handle_async_request(self, request):
        details = {"call_id": uuid4().hex, "method": request.method, "url": str(request.url)}
        started = perf_counter()
        write_event("model.requests", {**details, "headers": redact(dict(request.headers)),
                                        "body": body_value(await request.aread())})
        try:
            response = await self.transport.handle_async_request(request)
        except BaseException as exc:
            log_error(exc, **details, duration_ms=round((perf_counter() - started) * 1000, 2))
            raise
        response.stream = LoggedStream(response.stream, details, response.status_code, dict(response.headers), started)
        if response.is_stream_consumed:
            response.stream.body.extend(response.content)
            response.stream.finish(True)
        return response

    async def aclose(self):
        await self.transport.aclose()


class LoggedStream(httpx.AsyncByteStream):
    def __init__(self, stream, details, status, headers, started):
        self.stream, self.details, self.status = stream, details, status
        self.headers, self.started = headers, started
        self.body = bytearray()
        self.logged = False

    def finish(self, complete, exc=None):
        if self.logged:
            return
        self.logged = True
        result = {**self.details, "status": self.status, "headers": redact(self.headers),
                  "duration_ms": round((perf_counter() - self.started) * 1000, 2),
                  "complete": complete, "body": body_value(bytes(self.body), self.headers)}
        write_event("model.responses", result)
        if exc is not None:
            log_error(exc, **result)
        elif self.status >= 400 or not complete:
            write_event("errors", {**result, "message": "HTTP error" if complete else "Response stream closed before completion"})

    async def __aiter__(self):
        try:
            async for chunk in self.stream:
                self.body.extend(chunk)
                yield chunk
        except BaseException as exc:
            self.finish(False, exc)
            raise
        else:
            self.finish(True)

    async def aclose(self):
        try:
            await self.stream.aclose()
        finally:
            self.finish(False)
