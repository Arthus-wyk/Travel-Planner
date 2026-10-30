"""OCR and model-based, reviewable extraction of places from travel guides."""

from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from pathlib import Path
from typing import Any


SKILL_PATH = Path(__file__).resolve().parents[2] / ".agents" / "skills" / "guide-recognition" / "SKILL.md"


def recognize_guide(image_bytes: bytes) -> str:
    if not image_bytes or len(image_bytes) > 10 * 1024 * 1024:
        raise ValueError("Guide image must be between 1 byte and 10 MB")
    try:
        from rapidocr_onnxruntime import RapidOCR

        result, _ = RapidOCR()(image_bytes)
    except Exception as exc:
        raise ValueError("Unable to recognize text in this image") from exc
    lines = [str(item[1]).strip() for item in result or [] if len(item) >= 2 and str(item[1]).strip()]
    if not lines:
        raise ValueError("No readable text found in this image")
    return "\n".join(lines)


def _skill_prompt() -> str:
    content = SKILL_PATH.read_text(encoding="utf-8")
    return content.split("---", 2)[-1].strip() + """

保留以上只提取原文信息的原则，扩展 JSON 契约：
places 每项增加 category（attraction 游玩 / food 吃饭 / hotel 酒店 / other 其他）、
address（原文地址线索，没有则空字符串）、day（原文明确指定第几天，没有则 null）、
time_window（原文时间段，HH:MM-HH:MM，没有则空字符串）。不要生成经纬度。
例如“上午”可表达为09:00-12:00，“晚上”为18:00-23:00；精确时间使用相同起止值。
顶层增加 constraints 数组：每项 type 为 day/time_window/order/duration/hotel_change，
places 为原文地点名称列表，source 为原文依据，hard 为是否明确必须遵守；
day 类型含 day 整数，time_window 含 time_window，duration 含 duration_minutes；
order 按先后列出 places。仅提取原文明确要求，不要补充约束。
"""


def _extract_with_model(text: str) -> str:
    from service.utils.model_client import create_model_client, get_model

    async def request() -> str:
        response = await asyncio.wait_for(create_model_client().chat(
            model=get_model(),
            messages=[{"role": "system", "content": _skill_prompt()},
                      {"role": "user", "content": f"攻略原文：\n{text}"}],
            tools=[], stream=False, format="json",
        ), timeout=90)
        return response.message.content or ""

    return asyncio.run(request())


def analyze_guide(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("Guide text is too long")
    if not text.strip():
        return {"text": text, "places": []}
    try:
        data = json.loads(_extract_with_model(text))
    except Exception as exc:
        raise ValueError("Guide extraction model failed; please retry or check model configuration") from exc
    if not isinstance(data, dict) or not isinstance(data.get("places"), list):
        raise ValueError("Guide extraction model returned invalid places")
    places = []
    seen = set()
    for item in data["places"]:
        if not isinstance(item, dict):
            raise ValueError("Guide extraction model returned invalid places")
        name, note = item.get("name"), item.get("note", "")
        if not isinstance(name, str) or not isinstance(note, str):
            raise ValueError("Guide extraction model returned invalid places")
        name = name.strip()
        if not name or len(name) > 80 or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        duration = item.get("duration_minutes")
        if isinstance(duration, bool) or not isinstance(duration, int) or not 30 <= duration <= 600:
            duration = None
        ticket = item.get("ticket_required")
        if not isinstance(ticket, bool):
            ticket = None
        tips = item.get("visit_tips")
        if not isinstance(tips, str):
            tips = ""
        category = item.get("category", "attraction")
        if category not in {"attraction", "food", "hotel", "other"}:
            raise ValueError("Guide extraction model returned invalid category")
        day = item.get("day")
        if day is not None and (isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 365):
            raise ValueError("Guide extraction model returned invalid day")
        address = item.get("address") or ""
        window = item.get("time_window") or ""
        if not isinstance(address, str) or len(address) > 300 or not _valid_window(window):
            raise ValueError("Guide extraction model returned invalid location or time window")
        places.append({"id": "guide-" + sha256(name.casefold().encode("utf-8")).hexdigest()[:12],
                       "name": name, "note": note.strip()[:500], "source": "guide_text",
                       "category": category, "day": day, "time_window": window,
                       "location_hint": address.strip(),
                       "duration_minutes": duration, "ticket_required": ticket,
                       "visit_tips": tips.strip()[:500]})
    constraints = data.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 200:
        raise ValueError("Guide extraction model returned invalid constraints")
    names = {x["name"]: x["id"] for x in places}
    validated = []
    for constraint in constraints:
        if not isinstance(constraint, dict) or constraint.get("type") not in {"day", "time_window", "order", "duration", "hotel_change"}:
            raise ValueError("Guide extraction model returned invalid constraint")
        refs = constraint.get("places", [])
        source = constraint.get("source", "")
        if not isinstance(refs, list) or any(not isinstance(x, str) or x not in names for x in refs) or not isinstance(source, str):
            raise ValueError("Guide constraint references an unknown place")
        kind = constraint["type"]
        value = {"type": kind, "activity_ids": [names[x] for x in refs], "source": source[:500], "hard": constraint.get("hard", True) is not False}
        if kind == "day":
            day = constraint.get("day")
            if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 365:
                raise ValueError("Invalid guide day constraint")
            value["day"] = day
        elif kind == "time_window":
            window = constraint.get("time_window")
            if not window or not _valid_window(window):
                raise ValueError("Invalid guide time constraint")
            value["time_window"] = window
        elif kind == "duration":
            duration = constraint.get("duration_minutes")
            if isinstance(duration, bool) or not isinstance(duration, int) or not 30 <= duration <= 600:
                raise ValueError("Invalid guide duration constraint")
            value["duration_minutes"] = duration
        validated.append(value)
    return {"text": text, "places": places, "constraints": validated}


def _valid_window(value: str) -> bool:
    if value == "":
        return True
    if not isinstance(value, str):
        return False
    from datetime import datetime
    try:
        start, end = value.split("-")
        return all(len(x) == 5 and datetime.strptime(x, "%H:%M") for x in (start, end)) and start <= end
    except ValueError:
        return False
