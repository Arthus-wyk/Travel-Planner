"""Adapt retained generation diagnostics to the existing runtime timeline."""

import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from service.utils.runtime_log import redact


MAX_STREAM_BYTES = 8 * 1024 * 1024
JOB_ID = re.compile(r"[a-f0-9]{32}")
LIFECYCLE = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) \w+ "
    r"(started|checkpoint|resuming|completed|failed|needs_input) "
    r"plan_id=(\S+) job_id=([a-f0-9]{32})(.*)$"
)


def _stream_tails(folder, name):
    parts, truncated, remaining = [], False, MAX_STREAM_BYTES
    # shortcut: 每组只读当前文件和 5 个备份的最新 8 MiB，更长历史需日志索引。
    for suffix in ("", ".1", ".2", ".3", ".4", ".5"):
        path = folder / (name + suffix)
        if path.is_symlink() or not path.is_file() or path.resolve().parent != folder:
            continue
        try:
            stat = path.stat()
            length = min(stat.st_size, remaining)
            truncated |= length < stat.st_size
            if length:
                parts.append((path.name, _read_tail(path, stat.st_ino, stat.st_mtime_ns, stat.st_size, length)))
                remaining -= length
        except OSError:
            truncated = True
    return parts, truncated


@lru_cache(maxsize=24)
def _read_tail(path, inode, modified, size, length):
    with path.open("rb") as stream:
        stream.seek(size - length)
        return stream.read(length).decode("utf-8", errors="replace").replace("\r\n", "\n")


@lru_cache(maxsize=12)
def _wire_records(text, kind, filename, summaries_only=False):
    records = []
    for block in text.split("\n\n"):
        try:
            record = json.loads(block)
        except ValueError:
            continue
        if not isinstance(record, dict) or not JOB_ID.fullmatch(str(record.get("job_id", ""))):
            continue
        if kind.startswith("api.") and record.get("direction") != "outbound":
            continue
        try:
            if summaries_only:
                event = _metadata(record["job_id"], record.get("plan_id", ""))
                event["timestamp"] = _wire_timestamp(record)
                if kind.startswith("model.") and isinstance(record.get("body"), dict):
                    model = record["body"].get("model")
                    if isinstance(model, str):
                        event["model"] = model
                if kind == "errors":
                    event.update(type="generation_error", stage=record.get("stage"))
                records.append(redact(event))
            else:
                records.append(_wire_event(record, kind, filename))
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
    return records


def _metadata(job_id, plan_id):
    return {"runId": job_id, "sessionId": plan_id, "jobId": job_id, "planId": plan_id, "source": "generation"}


def _wire_timestamp(record):
    return datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def _wire_event(record, kind, filename):
    event = {**record, **_metadata(record["job_id"], record.get("plan_id", "")), "sourceFile": filename}
    event["timestamp"] = _wire_timestamp(record)
    event["durationMs"] = record.get("duration_ms")
    body = record.get("body")
    if kind.startswith("model."):
        event["type"] = "model_request" if kind.endswith("requests") else "model_response"
        body = body if isinstance(body, dict) else {}
        event["model"] = body.get("model") if isinstance(body.get("model"), str) else ""
        if event["type"] == "model_request":
            event["content"] = body.get("messages", [])
        else:
            message, choices = body.get("message"), body.get("choices")
            if not isinstance(message, dict) and isinstance(choices, list) and choices and isinstance(choices[0], dict):
                message = choices[0].get("message")
            event["content"] = message.get("content", "") if isinstance(message, dict) else ""
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            event["usage"] = {"inputTokens": usage.get("prompt_tokens", body.get("prompt_eval_count", 0)),
                              "outputTokens": usage.get("completion_tokens", body.get("eval_count", 0))}
            event["usage"] = {key: value if type(value) is int and value >= 0 else 0 for key, value in event["usage"].items()}
    elif kind == "errors":
        event["type"] = "generation_error"
    else:
        event["type"] = "api_request" if kind.endswith("requests") else "api_response"
        event["content"] = record.get("params") if event["type"] == "api_request" else body
    event["isError"] = kind == "errors" or (isinstance(record.get("status"), int) and record["status"] >= 400) or (
        isinstance(body, dict) and (body.get("status") == "0" or "error" in body)) or record.get("complete") is False
    return redact(event)


def generation_events(project_path, job_id="", *, summaries_only=False):
    folder = Path(project_path).resolve() / "logs"
    if folder.is_symlink() or not folder.is_dir():
        return [], False
    folder = folder.resolve()
    events, truncated = [], False
    for kind in ("model.requests", "model.responses", "api.requests", "api.responses", "errors"):
        parts, limited = _stream_tails(folder, kind + ".log")
        truncated |= limited
        for filename, text in parts:
            events.extend(event for event in _wire_records(text, kind, filename, summaries_only)
                          if not job_id or event["jobId"] == job_id)
    parts, limited = _stream_tails(folder, "generation.log")
    truncated |= limited
    for filename, text in parts:
        for line in text.splitlines():
            match = LIFECYCLE.fullmatch(line)
            if not match:
                continue
            timestamp, action, plan_id, task_id, tail = match.groups()
            if job_id and task_id != job_id:
                continue
            try:
                timestamp = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S,%f").astimezone(timezone.utc).isoformat()
            except ValueError:
                continue
            fields = dict(re.findall(r"(\w+)=(\S+)", tail))
            event = {**_metadata(task_id, plan_id), **fields, "timestamp": timestamp, "sourceFile": filename,
                     "type": "generation_progress", "message": line}
            if action == "started":
                event.update(type="run_start", input=f"行程生成 · {plan_id}")
            elif action in {"completed", "failed", "needs_input"}:
                event.update(type="run_end", status=action)
            elif action == "resuming":
                event["stage"] = "resuming"
            events.append(redact(event))
    events.sort(key=lambda event: event["timestamp"])
    return events, truncated
