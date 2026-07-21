from __future__ import annotations

import re
import os
from pathlib import Path


# ============================================================
# 日志路径约定
# ============================================================
#
# 这里集中管理所有日志文件路径，避免业务代码里到处拼字符串。
# 默认根目录遵循 Claude/Codex 一类本地客户端常见约定：
#
#   ~/.claude/
#
# 其中：
# - projects/ 下面按“项目路径安全化名字”分目录保存聊天流水。
# - stats-cache.json 保存 /usage 的扫描缓存。
# - tengu-api-events.jsonl 保存 API 请求层的诊断事件。

CLAUDE_HOME = Path(os.environ.get("TASK_PROGRESS_LOG_HOME", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_HOME / "projects"
STATS_CACHE_FILE = CLAUDE_HOME / "stats-cache.json"
API_EVENTS_FILE = CLAUDE_HOME / "tengu-api-events.jsonl"


def safe_project_name(project_path: Path) -> str:
    """把本机项目路径转换成适合作为目录名的稳定字符串。

    Windows 路径里会有冒号、反斜杠；Unix 路径里会有斜杠。
    这些字符都不适合直接作为单级目录名，所以统一替换成短横线。
    同一个项目路径每次都会得到同一个名字，便于长期追加会话日志。
    """

    resolved = str(project_path.resolve())
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", resolved).strip("-")
    return safe or "default-project"


def project_log_dir(project_path: Path) -> Path:
    """返回当前项目的聊天流水目录。"""

    return PROJECTS_DIR / safe_project_name(project_path)


def transcript_file(project_path: Path, session_id: str) -> Path:
    """返回某个会话对应的 .jsonl 流水文件路径。"""

    return project_log_dir(project_path) / f"{session_id}.jsonl"
