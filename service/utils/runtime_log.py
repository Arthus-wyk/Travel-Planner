"""每次任务的诊断上下文；ContextVar 隔离并发请求，不修改模型输入。"""

import asyncio
import logging
import json
import re
import traceback
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from time import perf_counter


current_run = ContextVar("travel_log_run", default=None)
logger = logging.getLogger(__name__)
SECRET_KEY = re.compile(r"^(key|authorization|proxy.authorization|.*api[_-]?key|.*token|.*password|.*secret|cookie|set-cookie)$", re.I)
SECRET_TEXT = re.compile(
    r"((?:Bearer|Basic)\s+)[^\s\"',;]+|((?:[\w-]*(?:api[_-]?key|token|password|secret)|authorization|cookie)[\"']?\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|(?:Bearer|Basic)\s+[^\s\"'&,;}]+|[^\s\"'&,;}]+)",
    re.I,
)


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if SECRET_KEY.match(str(key)) else redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except ValueError:
                pass
            else:
                cleaned = redact(parsed)
                return json.dumps(cleaned, ensure_ascii=False) if cleaned != parsed else value
        value = re.sub(r"((?:^|[?&])key=)[^&\s\"']+", r"\1[REDACTED]", value, flags=re.I)
        return SECRET_TEXT.sub(lambda match: (match[1] or match[2]) + "[REDACTED]", value)
    return value


def record_event(event_type, payload):
    if event_type.endswith("error"):
        from service.utils.request_log import write_event
        write_event("errors", {"event": event_type, **payload})
    run = current_run.get()
    if run is not None:
        run.record(event_type, payload)


class RunTrace:
    def __init__(self, transcript):
        self.transcript = transcript
        self.run_id = uuid.uuid4().hex
        self.started = perf_counter()
        self.status = "interrupted"
        self.answer = ""

    def record(self, event_type, payload):
        try:
            self.transcript.record(event_type, payload)
        except OSError:
            # 诊断磁盘失败不能把已经执行的旅行操作变成可重试失败。
            logger.exception("Unable to write runtime diagnostic log")

    def finish(self, status, answer):
        self.status, self.answer = status, answer


@contextmanager
def trace_run(transcript, *, model, text):
    parent = current_run.get()
    if parent is not None or transcript is None:
        yield parent
        return
    run = RunTrace(transcript)
    token = current_run.set(run)
    run.record("run_start", {"model": model, "input": text})
    try:
        yield run
    except BaseException as exc:
        run.record("run_end", {
            "status": "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            "durationMs": round((perf_counter() - run.started) * 1000, 2),
            "errorType": type(exc).__name__, "message": str(exc),
            "traceback": traceback.format_exc(),
        })
        raise
    else:
        run.record("run_end", {
            "status": run.status, "answer": run.answer,
            "durationMs": round((perf_counter() - run.started) * 1000, 2),
        })
    finally:
        current_run.reset(token)
