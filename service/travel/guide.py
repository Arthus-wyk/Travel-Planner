"""OCR and model-based, reviewable extraction of places from travel guides."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from hashlib import sha256
from pathlib import Path
from typing import Any

import httpx

from service.config import get_env_or_dotenv


SKILL_PATH = Path(__file__).resolve().parents[2] / ".agents" / "skills" / "guide-recognition" / "SKILL.md"


def _object(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


_TEXT = {"type": "string"}
_DAY = {"type": "integer", "minimum": 1, "maximum": 365}
_DURATION = {"type": "integer", "minimum": 30, "maximum": 600}
_PLACE_PROPERTIES = {
    "name": {"type": "string", "minLength": 1, "maxLength": 80},
    "category": {"type": "string", "enum": ["attraction", "food", "hotel", "lodging_area", "other"]},
    "meal_type": {"enum": ["main", "dessert", None]},
    "day": {"anyOf": [_DAY, {"type": "null"}]},
    "duration_minutes": {"anyOf": [_DURATION, {"type": "null"}]},
    "ticket_required": {"type": ["boolean", "null"]},
    "note": {"type": "string", "maxLength": 500},
}
_CONSTRAINT = _object({
    "type": {"type": "string", "enum": ["order", "hotel_change"]},
    "places": {"type": "array", "items": _TEXT},
    "source": {"type": "string", "maxLength": 500},
})
GUIDE_SCHEMA = _object({
    "places": {"type": "array", "items": _object(_PLACE_PROPERTIES)},
    "constraints": {"type": "array", "maxItems": 200, "items": _CONSTRAINT},
})


class GuideOutputError(ValueError):
    def __init__(self, reason: str, output: str | None = None):
        super().__init__(reason)
        self.output = output


class GuideNeedsInput(ValueError):
    pass


def split_guide_days(text: str) -> tuple[list[dict], str]:
    """Keep OCR/pasted lines verbatim and separate explicitly dated sections."""
    sections = {}
    day = None
    digits = "零一二三四五六七八九"
    for line in text.splitlines():
        heading = line.lstrip(" \t#>*-")
        match = re.match(r"(?:第\s*([\d零一二两三四五六七八九十]{1,3})\s*[天日]|(?:day\s*|d)(\d{1,3})(?![\da-z]))", heading, re.I)
        if match:
            number = (match[1] or match[2]).replace("两", "二")
            if number.isdecimal():
                day = int(number)
            elif "十" in number:
                tens, _, ones = number.partition("十")
                day = None
                if (tens or "一") in digits[1:] and (ones or "零") in digits and len(tens) <= 1 and len(ones) <= 1:
                    day = 10 * digits.index(tens or "一") + digits.index(ones or "零")
            else:
                day = digits.index(number) if len(number) == 1 and number in digits else None
            if day is not None and not 1 <= day <= 365:
                day = None
        elif re.match(r"(?:备选|备用(?:信息)?|住宿(?:建议|推荐)|通用(?:提示|信息)|行前准备)(?:\s*[:：]|\s*$)", heading):
            day = None
        sections.setdefault(day, []).append(line)
    days = [{"day": day, "text": "\n".join(lines).strip()} for day, lines in sorted(
        (day, lines) for day, lines in sections.items() if day is not None
    )]
    return days, "\n".join(sections.get(None, [])).strip()


def recognize_guide(image_bytes: bytes) -> str:
    if not image_bytes or len(image_bytes) > 10 * 1024 * 1024:
        raise ValueError("Guide image must be between 1 byte and 10 MB")
    from rapidocr_onnxruntime import RapidOCR

    for attempt in range(1, 4):
        try:
            result, _ = RapidOCR()(image_bytes)
            break
        except Exception as exc:
            if attempt == 3:
                raise ValueError("Unable to recognize text in this image") from exc
            from service.utils.request_log import log_error
            log_error(exc, stage="guide_ocr", attempt=attempt)
    lines = [str(item[1]).strip() for item in result or [] if len(item) >= 2 and str(item[1]).strip()]
    if not lines:
        raise ValueError("No readable text found in this image")
    return "\n".join(lines)


def _skill_prompt() -> str:
    content = SKILL_PATH.read_text(encoding="utf-8")
    return content.split("---", 2)[-1].strip() + (
        "\n每个地点输出 meal_type。仅 food 根据本次到店上下文分类：午餐、晚餐、简餐等"
        "用 main，饭后甜品、蛋糕、冰淇淋等加餐用 dessert。咖啡馆如用于吃简餐仍用 main。"
        "把判断依据保留在 note。语境不足或不是 food 时用 null。"
    )


def _extract_with_model(text: str, feedback: str = "", *, history=()) -> str:
    from service.utils.model_client import create_model_client, get_model

    async def request() -> str:
        model = get_model()
        siliconflow_glm = (
            model == "zai-org/GLM-5.3-Flash"
            and get_env_or_dotenv("LLM_PROVIDER").strip().lower() == "siliconflow"
        )
        messages = [{"role": "system", "content": _skill_prompt() + "\nJSON Schema:\n" + json.dumps(GUIDE_SCHEMA, ensure_ascii=False)},
                    {"role": "user", "content": f"攻略原文：\n{text}"}]
        for output, reason in history:
            if output is not None:
                messages.append({"role": "assistant", "content": output})
            messages.append({"role": "user", "content": f"上次输出校验失败：{reason}\n请修正后重新输出完整 JSON。"})
        if feedback and not history:
            messages.append({"role": "user", "content": f"上次输出校验失败：{feedback}\n请修正后重新输出完整 JSON。"})
        response = await asyncio.wait_for(create_model_client().chat(
            model=model,
            messages=messages,
            tools=[], stream=False, format="json" if siliconflow_glm else GUIDE_SCHEMA,
        ), timeout=180)
        from jsonschema import ValidationError, validate
        content = response.message.content or ""
        if response.message.tool_calls:
            raise GuideOutputError("不得调用工具", content)
        if siliconflow_glm and not content and getattr(response, "finish_reason", None) == "stop":
            # This endpoint can put the completed JSON in the reasoning field.
            reasoning = getattr(response.message, "reasoning_content", None)
            if isinstance(reasoning, dict):
                content = json.dumps(reasoning, ensure_ascii=False)
            elif isinstance(reasoning, str):
                content = reasoning
        try:
            validate(json.loads(content), GUIDE_SCHEMA)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise GuideOutputError(f"JSON 或 Schema 无效：{exc}", content) from exc
        return content

    return asyncio.run(request())


def analyze_guide(text: str) -> dict[str, Any]:
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("Guide text is too long")
    if not text.strip():
        return {"text": text, "places": []}
    feedback = ""
    # 每次攻略提取独立保留回复和校验反馈，后续请求按原顺序回放。
    history = []
    for attempt in range(1, 4):
        try:
            raw = _extract_with_model(text, feedback, history=tuple(history))
        except GuideOutputError as exc:
            from service.utils.request_log import log_error
            log_error(exc, stage="guide_extraction", attempt=attempt)
            feedback = str(exc)[:1000]
            history.append((exc.output, feedback))
            logging.getLogger("travel.generation").warning("guide_extraction_invalid attempt=%s reason=%s", attempt, feedback)
            continue
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
            if attempt < 3:
                logging.getLogger("travel.generation").warning("guide_extraction_timeout attempt=%s", attempt)
                continue
            raise ValueError("攻略提取模型响应超时（180 秒），请检查模型服务状态后重试") from exc
        except Exception as exc:
            if attempt < 3:
                from service.utils.request_log import log_error
                log_error(exc, stage="guide_extraction", attempt=attempt)
                continue
            raise ValueError("Guide extraction model failed; please retry or check model configuration") from exc
        try:
            return _parse_guide_data(json.loads(raw), text)
        except ValueError as exc:
            from service.utils.request_log import log_error
            log_error(exc, stage="guide_extraction", attempt=attempt)
            feedback = str(exc)[:1000]
            history.append((raw, feedback))
            logging.getLogger("travel.generation").warning("guide_extraction_invalid attempt=%s reason=%s", attempt, feedback)
    raise GuideNeedsInput(f"攻略地点提取连续 3 次校验失败：{feedback}")


def _parse_guide_data(data: Any, text: str) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) - {"places", "constraints"} or not isinstance(data.get("places"), list):
        raise ValueError("Guide extraction model returned invalid places")
    places = []
    lodging_areas = []
    seen = set()
    for item in data["places"]:
        if not isinstance(item, dict) or set(item) - set(_PLACE_PROPERTIES):
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
        category = item.get("category", "attraction")
        if category not in {"attraction", "food", "hotel", "lodging_area", "other"}:
            raise ValueError("Guide extraction model returned invalid category")
        meal_type = item.get("meal_type")
        if meal_type not in ("main", "dessert", None) or (meal_type is not None and category != "food"):
            raise ValueError("Guide extraction model returned invalid meal type")
        if category == "lodging_area":
            lodging_areas.append({"name": name, "note": note.strip()[:500]})
            continue
        day = item.get("day")
        if day is not None and (isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 365):
            raise ValueError("Guide extraction model returned invalid day")
        places.append({"id": "guide-" + sha256(name.casefold().encode("utf-8")).hexdigest()[:12],
                       "name": name, "note": note.strip()[:500], "source": "guide_text",
                       "category": category, "day": day,
                       "location_hint": "",
                       "duration_minutes": duration, "ticket_required": ticket,
                       "visit_tips": note.strip()[:500]})
        if category == "food":
            places[-1]["meal_type"] = meal_type
    for match in re.finditer(r'(?:优先住|建议住|推荐住|也可以选|也可选)\s*[「“"]([^」”"\n]{2,50})[」”"]', text):
        name = match.group(1).strip()
        if name not in {x["name"] for x in lodging_areas}:
            lodging_areas.append({"name": name, "note": "原文住宿区域建议"})
    constraints = data.get("constraints", [])
    if not isinstance(constraints, list) or len(constraints) > 200:
        raise ValueError("Guide extraction model returned invalid constraints")
    names = {x["name"].casefold(): x["id"] for x in places}
    validated = []
    for constraint in constraints:
        if not isinstance(constraint, dict) or set(constraint) - {"type", "places", "source", "hard", "day", "time_window", "duration_minutes"} or constraint.get("type") not in {"day", "time_window", "order", "duration", "hotel_change"}:
            raise ValueError("Guide extraction model returned invalid constraint")
        refs = constraint.get("places", [])
        source = constraint.get("source", "")
        if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs) or not isinstance(source, str):
            raise ValueError("Guide extraction model returned invalid constraint references")
        area_names = {x["name"].casefold() for x in lodging_areas}
        if any(x.strip().casefold() in area_names for x in refs):
            continue
        unknown = [x for x in refs if x.strip().casefold() not in names]
        if unknown:
            raise ValueError(f"Guide constraint references an unknown place: {unknown[0]}")
        kind = constraint["type"]
        value = {"type": kind, "activity_ids": [names[x.strip().casefold()] for x in refs], "source": source[:500], "hard": constraint.get("hard", True) is not False}
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
    return {"text": text, "places": places, "lodging_areas": lodging_areas, "constraints": validated}


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
