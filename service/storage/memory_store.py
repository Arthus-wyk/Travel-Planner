"""记忆存储：长期 Markdown 画像和中期 JSONL；不调用模型或决定召回策略。"""

from __future__ import annotations

import re
import json
from datetime import datetime
from pathlib import Path
from typing import Any


PROFILE_SECTIONS = {
    "travel_interests": "旅行兴趣",
    "budget": "预算偏好",
    "trip_length": "出行天数",
    "hotel": "酒店偏好",
    "departure_city": "出发城市",
    "pace": "旅行节奏",
    "restaurant": "餐厅偏好",
    "constraints": "约束与禁忌",
    "notes": "其他备注",
}

PROFILE_DIR = Path("data") / "memory"
PROFILE_PATH = PROFILE_DIR / "travel_preference_profile.md"


def read_preference_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    """确保画像存在后读取，并返回原文与结构化数据。"""
    ensure_preference_profile(path)
    content = path.read_text(encoding="utf-8")
    return {
        "success": True,
        "path": str(path),
        "profile_markdown": content,
        "profile": parse_preference_markdown(content),
    }


def update_preference_profile(
    updates: dict[str, Any],
    *,
    note: str = "",
    path: Path = PROFILE_PATH,
) -> dict[str, Any]:
    """校验类别、去重追加偏好并重写 Markdown，不覆盖既有偏好。"""
    ensure_preference_profile(path)
    profile = parse_preference_markdown(path.read_text(encoding="utf-8"))

    invalid = sorted(set(updates) - set(PROFILE_SECTIONS))
    if invalid:
        return {
            "success": False,
            "message": f"Invalid preference categories: {', '.join(invalid)}.",
            "valid_categories": PROFILE_SECTIONS,
        }

    changed: dict[str, list[str]] = {}
    for category, raw_values in updates.items():
        values = normalize_values(raw_values)
        if not values:
            continue
        current = profile.setdefault(category, [])
        added = []
        for value in values:
            if value not in current:
                current.append(value)
                added.append(value)
        if added:
            changed[category] = added

    rendered = render_preference_markdown(profile, note=note)
    path.write_text(rendered, encoding="utf-8")

    return {
        "success": True,
        "path": str(path),
        "changed": changed,
        "profile_markdown": rendered,
        "profile": profile,
    }


def ensure_preference_profile(path: Path = PROFILE_PATH) -> None:
    """首次使用时创建画像目录和模板文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return
    empty = {category: [] for category in PROFILE_SECTIONS}
    path.write_text(render_preference_markdown(empty), encoding="utf-8")


def parse_preference_markdown(content: str) -> dict[str, list[str]]:
    """按固定二级标题解析 Markdown，忽略空占位项。"""
    profile = {category: [] for category in PROFILE_SECTIONS}
    heading_to_key = {title: key for key, title in PROFILE_SECTIONS.items()}
    current_key: str | None = None

    for line in content.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            current_key = heading_to_key.get(heading.group(1).strip())
            continue
        if current_key is None:
            continue
        item = re.match(r"^-\s+(.+?)\s*$", line)
        if item:
            value = item.group(1).strip()
            if value and value != "暂无":
                profile[current_key].append(value)

    return profile


def render_preference_markdown(profile: dict[str, list[str]], *, note: str = "") -> str:
    """按固定顺序渲染画像，保证人可读且下次可再次解析。"""
    lines = [
        "# 长期旅行偏好画像",
        "",
        f"最后更新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "这个文件供旅行规划 agent 在需要时读取，用于保持长期偏好一致。",
        "",
    ]
    for key, title in PROFILE_SECTIONS.items():
        lines.extend([f"## {title}", ""])
        values = profile.get(key) or []
        if values:
            lines.extend(f"- {value}" for value in values)
        else:
            lines.append("- 暂无")
        lines.append("")

    if note:
        lines.extend(["## 更新备注", "", f"- {note}", ""])

    return "\n".join(lines).rstrip() + "\n"


def normalize_values(value: Any) -> list[str]:
    """将字符串、数字或嵌套列表统一成非空字符串列表。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [str(value)]
    if isinstance(value, list):
        values: list[str] = []
        for item in value:
            values.extend(normalize_values(item))
        return values
    return [str(value).strip()] if str(value).strip() else []


MID_TERM_PATH = Path("data") / "memory" / "mid_term_memory.jsonl"


def read_mid_term(limit: int = 30) -> list[dict[str, Any]]:
    if not MID_TERM_PATH.exists():
        return []
    items: list[dict[str, Any]] = []
    for line in MID_TERM_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def append_mid_term(items: list[dict[str, Any]], *, plan_id: str | None) -> int:
    """仅保存经过提取层筛选的显式记忆，保持原 JSONL 路径和格式。"""
    if not items:
        return 0
    MID_TERM_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MID_TERM_PATH.open("a", encoding="utf-8") as handle:
        for item in items:
            handle.write(
                json.dumps({**item, "plan_id": plan_id}, ensure_ascii=False) + "\n"
            )
    return len(items)
