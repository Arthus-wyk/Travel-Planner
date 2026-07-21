from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """生成 UTC ISO 时间戳，所有日志统一使用这个格式。"""

    return datetime.now(timezone.utc).isoformat()


def to_jsonable(value: Any) -> Any:
    """把 SDK / MCP 对象递归转换成 JSON 可以安全写入的结构。

    模型返回的 tool_call、MCP 返回对象等通常不是纯 dict/list。
    直接 json.dumps 会失败；这里按“尽量保留字段”的原则转换：
    1. 基础类型原样返回。
    2. dict/list/tuple 递归转换。
    3. 有 model_dump 的对象按 Pydantic 风格导出。
    4. 普通对象读取 __dict__。
    5. 兜底使用 repr，至少保证日志不会因为序列化失败而中断主流程。
    """

    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return to_jsonable(model_dump())
        except Exception:
            pass

    if hasattr(value, "__dict__"):
        return {
            key: to_jsonable(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }

    return repr(value)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """向 JSONL 文件追加一条记录。

    JSONL 的好处是写入非常简单：每一行都是完整 JSON。
    即使程序中途退出，已经写完的行仍然可以被后续 /usage 扫描。
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        json.dump(to_jsonable(record), file, ensure_ascii=False)
        file.write("\n")
