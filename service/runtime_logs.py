"""只读取当前项目的诊断流水，兼容没有 runId 的旧会话。"""

import hashlib
import json
import re
from collections import deque
from functools import lru_cache
from pathlib import Path
from datetime import datetime

from service.generation_logs import generation_events
from service.utils.log_paths import project_log_dir
from service.utils.runtime_log import redact


@lru_cache(maxsize=100)
def _transcript_events(path, inode, modified, size, project_path):
    with path.open(encoding="utf-8", errors="replace") as stream:
        lines = deque(stream, maxlen=5001)
    events = []
    for line in list(lines)[-5000:]:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("projectPath") != project_path:
            continue
        event = redact(event)
        event.setdefault("sessionId", path.stem)
        event.setdefault("runId", "legacy-" + hashlib.sha256(path.stem.encode()).hexdigest()[:24])
        events.append(event)
    return events, len(lines) > 5000


def _events(project_path, conversation_id=""):
    if conversation_id and not re.fullmatch(r"[\w-]{1,128}", conversation_id):
        raise ValueError("Invalid conversation_id")
    folder = project_log_dir(project_path).resolve()
    files = list(folder.glob("*.jsonl")) if folder.exists() else []
    files = [p for p in files if p.resolve().parent == folder and not p.is_symlink()]
    if conversation_id:
        files = [p for p in files if p.stem == conversation_id]
    snapshots = []
    for path in files:
        try:
            snapshots.append((path, path.stat()))
        except FileNotFoundError:
            continue
    snapshots.sort(key=lambda item: item[1].st_mtime_ns, reverse=True)
    # shortcut: 本地诊断仅扫描最近 100 个会话、每会话最后 5000 行；更大规模改用索引存储。
    truncated = len(files) > 100
    events = []
    for path, stat in snapshots[:100]:
        try:
            batch, limited = _transcript_events(path, stat.st_ino, stat.st_mtime_ns, stat.st_size,
                                                str(Path(project_path).resolve()))
        except FileNotFoundError:
            truncated = True
            continue
        events.extend(batch)
        truncated |= limited
    return events, truncated


def _turn_summaries(events):
    runs = {}
    for event in events:
        key = event["runId"]
        run = runs.setdefault(key, {
            "runId": key, "sessionId": event["sessionId"],
            "startedAt": event.get("timestamp", ""), "model": "",
            "status": "legacy" if key.startswith("legacy-") else "running",
            "input": "", "eventCount": 0, "durationMs": None,
        })
        if event.get("source") == "generation":
            run.update(source="generation", jobId=key, planId=event.get("planId", ""))
            if not run["input"]:
                run["input"] = f"行程生成 · {run['planId']}"
            if event.get("model"):
                run["model"] = event["model"]
            if event.get("type") == "generation_error" and event.get("stage") == "generation_job":
                run["status"] = "failed"
        run["updatedAt"] = max(run.get("updatedAt", ""), event.get("timestamp", ""))
        run["eventCount"] += 1
        if event.get("type") == "run_start":
            run.update(model=event.get("model", ""), input=event.get("input", ""))
        if event.get("type") == "message" and event.get("message", {}).get("role") == "user" and not run["input"]:
            run["input"] = event["message"].get("content", "")
        if event.get("type") == "run_end":
            run.update(status=event.get("status", "failed"), durationMs=event.get("durationMs"))
            if event.get("source") == "generation":
                run["durationMs"] = round((datetime.fromisoformat(event["timestamp"]) - datetime.fromisoformat(run["startedAt"])).total_seconds() * 1000, 2)
    return sorted(runs.values(), key=lambda item: (item["startedAt"], item["runId"]), reverse=True)


def _summaries(events):
    """Group chat turns by session while preserving original run IDs in events."""
    groups = {}
    for turn in reversed(_turn_summaries(events)):
        key = (turn.get("source", "chat"), turn.get("jobId") or turn["sessionId"])
        if key not in groups:
            groups[key] = {**turn, "turnCount": 1}
            continue
        group = groups[key]
        group["turnCount"] += 1
        group["eventCount"] += turn["eventCount"]
        group["durationMs"] = (group["durationMs"] or 0) + (turn["durationMs"] or 0)
        group.update(status=turn["status"], model=turn["model"] or group["model"],
                     updatedAt=max(group["updatedAt"], turn["updatedAt"]))
    return sorted(groups.values(), key=lambda item: (
        item["startedAt"] if item.get("source") == "generation" else item["updatedAt"], item["runId"]
    ), reverse=True)


def list_runs(project_path, *, conversation_id="", job_id="", status="", since="", until="", page=1, page_size=20):
    if page < 1 or not 1 <= page_size <= 100:
        raise ValueError("Invalid page or page_size")
    if conversation_id and not re.fullmatch(r"[\w-]{1,128}", conversation_id):
        raise ValueError("Invalid conversation_id")
    if job_id and not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid job_id")
    if status and status not in {"completed", "failed", "needs_input", "cancelled", "interrupted", "limit_reached", "running", "legacy"}:
        raise ValueError("Invalid status")
    for value in (since, until):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Date must use YYYY-MM-DD (UTC)")
    events, truncated = ([], False) if job_id else _events(project_path, conversation_id)
    if not conversation_id:
        generation, limited = generation_events(project_path, job_id, summaries_only=True)
        events.extend(generation)
        truncated |= limited
    runs = [item for item in _summaries(events)
            if (not status or item["status"] == status)
            and (not since or item["updatedAt"][:10] >= since)
            and (not until or item["startedAt"][:10] <= until)]
    total = len(runs)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(page, page_count)
    start = (page - 1) * page_size
    return {"runs": runs[start:start + page_size], "truncated": truncated,
            "page": page, "page_size": page_size, "total": total, "page_count": page_count}


def read_run(project_path, run_id, *, conversation_id=""):
    if not re.fullmatch(r"(?:[a-f0-9]{32}|legacy-[a-f0-9]{24})", run_id):
        raise ValueError("Invalid run_id")
    events, truncated = _events(project_path, conversation_id)
    if not conversation_id:
        generation, limited = generation_events(project_path, run_id)
        events.extend(generation)
        truncated |= limited
    selected = next((event for event in events if event["runId"] == run_id), None)
    if selected is None:
        return None
    events = [event for event in events if (
        event["runId"] == run_id if selected.get("source") == "generation"
        else event.get("source") != "generation" and event["sessionId"] == selected["sessionId"]
    )]
    return {"run": _summaries(events)[0], "events": events, "truncated": truncated}


def event_entries(events, search=""):
    requests = {event.get("call_id") or event.get("requestId"): event for event in events
                if event.get("type") in {"model_request", "api_request"} and (event.get("call_id") or event.get("requestId"))}
    completed_models = {event.get("call_id") or event.get("requestId") for event in events
                        if event.get("type") == "model_response"}
    occurrences = {}
    search = search.casefold()
    for event in events:
        call_id = event.get("call_id") or event.get("requestId")
        if event.get("type") == "model_request" and call_id and call_id in completed_models:
            continue
        paired_request = requests.get(call_id)
        identity = tuple(event.get(key) for key in ("type", "timestamp", "call_id", "requestId", "stage"))
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        if search and search not in json.dumps([event, paired_request], ensure_ascii=False).casefold():
            continue
        event_id = hashlib.sha256(repr((identity, occurrence)).encode()).hexdigest()[:32]
        summary = {key: event[key] for key in ("type", "timestamp", "stage", "durationMs", "status",
                                               "tool", "attempt", "usage", "isError") if key in event}
        summary["eventId"] = event_id
        summary["pairedRequest"] = event.get("type") == "model_response" and paired_request is not None
        if event.get("type") == "tool_result" and re.search(
            r'"success"\s*:\s*false|"status"\s*:\s*"failed"', str(event.get("result") or "")
        ):
            summary["isError"] = True
        yield summary, event, requests.get(event.get("call_id") or event.get("requestId"))
