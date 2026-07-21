from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from service.utils.jsonl_log import utc_now_iso
from service.utils.log_paths import PROJECTS_DIR, STATS_CACHE_FILE


def load_usage_stats(*, force_refresh: bool = False) -> dict[str, Any]:
    """加载 /usage 统计。

    第三本“账”不是逐条写出来的日志，而是扫描第一本聊天流水后得到的汇总。
    为了避免每次都扫全量历史，这里会把文件签名和汇总结果缓存到
    ~/.claude/stats-cache.json。
    """

    files = sorted(PROJECTS_DIR.glob("*/*.jsonl")) if PROJECTS_DIR.exists() else []
    signature = _build_signature(files)

    if not force_refresh:
        cached = _read_cache()
        if cached.get("signature") == signature:
            return cached["stats"]

    stats = _scan_transcripts(files)
    STATS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATS_CACHE_FILE.write_text(
        json.dumps(
            {
                "generatedAt": utc_now_iso(),
                "signature": signature,
                "stats": stats,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return stats


def format_usage_stats(stats: dict[str, Any]) -> str:
    """把统计结果格式化成适合命令行查看的中文摘要。"""

    model_lines = []
    for model, usage in sorted(stats["models"].items()):
        model_lines.append(
            "- "
            f"{model}: input={usage['inputTokens']}, "
            f"output={usage['outputTokens']}, "
            f"total={usage['totalTokens']}, "
            f"calls={usage['apiCalls']}"
        )

    daily_lines = [
        f"- {day}: messages={item['messages']}, tools={item['toolCalls']}"
        for day, item in sorted(stats["dailyActivity"].items())
    ]

    if not model_lines:
        model_lines.append("- 暂无模型 token 记录")

    if not daily_lines:
        daily_lines.append("- 暂无活跃记录")

    longest = stats["longestSession"]
    longest_text = (
        f"{longest['sessionId']} ({longest['messages']} messages)"
        if longest
        else "暂无"
    )

    return "\n".join(
        [
            "Usage summary",
            f"总会话数: {stats['totalSessions']}",
            f"总消息数: {stats['totalMessages']}",
            f"工具调用次数: {stats['toolCallCount']}",
            f"最长会话: {longest_text}",
            "",
            "每个模型 token 用量:",
            *model_lines,
            "",
            "每天活跃情况:",
            *daily_lines,
        ]
    )


def _read_cache() -> dict[str, Any]:
    if not STATS_CACHE_FILE.exists():
        return {}

    try:
        return json.loads(STATS_CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _build_signature(files: list[Path]) -> list[dict[str, Any]]:
    """用路径、大小、修改时间判断缓存是否仍然可用。"""

    signature = []
    for file in files:
        stat = file.stat()
        signature.append(
            {
                "path": str(file),
                "size": stat.st_size,
                "mtimeNs": stat.st_mtime_ns,
            }
        )
    return signature


def _scan_transcripts(files: list[Path]) -> dict[str, Any]:
    sessions: dict[str, Counter[str]] = defaultdict(Counter)
    daily: dict[str, Counter[str]] = defaultdict(Counter)
    models: dict[str, Counter[str]] = defaultdict(Counter)

    total_messages = 0
    tool_calls = 0

    for file in files:
        for record in _iter_jsonl(file):
            session_id = str(record.get("sessionId") or file.stem)
            event_type = record.get("type")
            day = _record_day(record)

            if event_type == "message":
                total_messages += 1
                sessions[session_id]["messages"] += 1
                daily[day]["messages"] += 1

            elif event_type == "tool_use":
                tool_calls += 1
                sessions[session_id]["toolCalls"] += 1
                daily[day]["toolCalls"] += 1

            elif event_type == "api_summary":
                usage = record.get("api")
                if usage:
                    _add_model_usage(models, usage)

    longest = None
    for session_id, counter in sessions.items():
        candidate = {
            "sessionId": session_id,
            "messages": counter["messages"],
            "toolCalls": counter["toolCalls"],
        }
        if longest is None or candidate["messages"] > longest["messages"]:
            longest = candidate

    return {
        "generatedAt": utc_now_iso(),
        "totalSessions": len(sessions),
        "totalMessages": total_messages,
        "toolCallCount": tool_calls,
        "models": {
            model: {
                "inputTokens": counter["inputTokens"],
                "outputTokens": counter["outputTokens"],
                "totalTokens": counter["totalTokens"],
                "cacheReadTokens": counter["cacheReadTokens"],
                "apiCalls": counter["apiCalls"],
            }
            for model, counter in models.items()
        },
        "dailyActivity": {
            day: {
                "messages": counter["messages"],
                "toolCalls": counter["toolCalls"],
            }
            for day, counter in daily.items()
        },
        "longestSession": longest,
    }


def _iter_jsonl(file: Path) -> list[dict[str, Any]]:
    records = []
    with file.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _record_day(record: dict[str, Any]) -> str:
    timestamp = str(record.get("timestamp") or "")
    try:
        return datetime.fromisoformat(timestamp).date().isoformat()
    except ValueError:
        return "unknown"


def _add_model_usage(
    models: dict[str, Counter[str]],
    usage: dict[str, Any],
) -> None:
    model = str(usage.get("model") or "unknown")
    counter = models[model]
    counter["inputTokens"] += int(usage.get("inputTokens") or 0)
    counter["outputTokens"] += int(usage.get("outputTokens") or 0)
    counter["totalTokens"] += int(usage.get("totalTokens") or 0)
    counter["cacheReadTokens"] += int(usage.get("cacheReadTokens") or 0)
    counter["apiCalls"] += 1
