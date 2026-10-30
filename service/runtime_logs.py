"""只读取当前项目的诊断流水，兼容没有 runId 的旧会话。"""

import hashlib
import json
import re
from collections import deque
from pathlib import Path

from service.utils.log_paths import project_log_dir
from service.utils.runtime_log import redact


def _events(project_path, conversation_id=""):
    if conversation_id and not re.fullmatch(r"[\w-]{1,128}", conversation_id):
        raise ValueError("Invalid conversation_id")
    folder = project_log_dir(project_path).resolve()
    files = list(folder.glob("*.jsonl")) if folder.exists() else []
    files = [p for p in files if p.resolve().parent == folder and not p.is_symlink()]
    if conversation_id:
        files = [p for p in files if p.stem == conversation_id]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    # shortcut: 本地诊断仅扫描最近 100 个会话、每会话最后 5000 行；更大规模改用索引存储。
    truncated = len(files) > 100
    events = []
    for path in files[:100]:
        with path.open(encoding="utf-8", errors="replace") as stream:
            lines = deque(stream, maxlen=5001)
        truncated |= len(lines) > 5000
        for line in list(lines)[-5000:]:
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("projectPath") != str(Path(project_path).resolve()):
                continue
            event = redact(event)
            event.setdefault("sessionId", path.stem)
            event.setdefault("runId", "legacy-" + hashlib.sha256(path.stem.encode()).hexdigest()[:24])
            events.append(event)
    return events, truncated


def _summaries(events):
    runs = {}
    for event in events:
        key = event["runId"]
        run = runs.setdefault(key, {
            "runId": key, "sessionId": event["sessionId"],
            "startedAt": event.get("timestamp", ""), "model": "",
            "status": "legacy" if key.startswith("legacy-") else "running",
            "input": "", "eventCount": 0, "durationMs": None,
        })
        run["eventCount"] += 1
        if event.get("type") == "run_start":
            run.update(model=event.get("model", ""), input=event.get("input", ""))
        if event.get("type") == "message" and event.get("message", {}).get("role") == "user" and not run["input"]:
            run["input"] = event["message"].get("content", "")
        if event.get("type") == "run_end":
            run.update(status=event.get("status", "failed"), durationMs=event.get("durationMs"))
    return sorted(runs.values(), key=lambda item: item["startedAt"], reverse=True)


def list_runs(project_path, *, conversation_id="", status="", since="", until=""):
    events, truncated = _events(project_path, conversation_id)
    if status and status not in {"completed", "failed", "cancelled", "interrupted", "limit_reached", "running", "legacy"}:
        raise ValueError("Invalid status")
    for value in (since, until):
        if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("Date must use YYYY-MM-DD (UTC)")
    runs = [item for item in _summaries(events)
            if (not status or item["status"] == status)
            and (not since or item["startedAt"][:10] >= since)
            and (not until or item["startedAt"][:10] <= until)]
    return {"runs": runs[:200], "truncated": truncated or len(runs) > 200}


def read_run(project_path, run_id, *, conversation_id=""):
    if not re.fullmatch(r"(?:[a-f0-9]{32}|legacy-[a-f0-9]{24})", run_id):
        raise ValueError("Invalid run_id")
    events, truncated = _events(project_path, conversation_id)
    events = [event for event in events if event["runId"] == run_id]
    if not events:
        return None
    return {"run": _summaries(events)[0], "events": events, "truncated": truncated}
