"""Discover and select planning candidates."""
from __future__ import annotations
import base64
from copy import deepcopy
from typing import Any
from service import config
from service.integrations.travel_amap import retry_amap_call
from service.mcp.server import amap_search_hotels, amap_search_places
from service.travel.guide import GuideNeedsInput, recognize_guide
from service.travel.place_matching import search_and_match
from service.travel.planning import build_activities, build_candidates, build_hotel_activities
from service.travel.map_planning import locate_activities, has_coordinates
from agent.planning.schemas.state import PlanningState


def _hotel_places(plan: dict[str, Any]) -> list[dict]:
    if not config.AMAP_API_KEY:
        return []
    result = retry_amap_call(amap_search_hotels, plan["destination"], max_results=3)
    return result.get("places", []) if result.get("success") else []


def _activity_places(plan: dict[str, Any]) -> list[dict]:
    places = []
    if config.AMAP_API_KEY:
        for interest in (plan.get("interests") or ["景点"])[:3]:
            result = retry_amap_call(amap_search_places, f"{plan['destination']} {interest}", max_results=3)
            if result.get("success"):
                places.extend(result.get("places", []))
    if not places:
        for interest in (plan.get("interests") or ["城市漫步", "当地文化"])[:3]:
            places.append({"name": f"{plan['destination']}·{interest}",
                           "address": plan["destination"], "source": "simulated_suggestion",
                           "note": "主题建议，请选择具体地点后再确认路线"})
    return places


def transport_agent(state: PlanningState) -> dict:
    """生成往返交通候选及初始规划选项。"""
    return {"plan": build_candidates(state["plan"]), "trace": state["trace"] + ["transport_agent"]}


def hotel_agent(state: PlanningState) -> dict:
    """搜索住宿候选，并把酒店加入可选择的活动列表。"""
    places = _hotel_places(state["plan"])
    plan = build_hotel_activities(state["plan"], places or None)
    return {"plan": plan, "trace": state["trace"] + ["hotel_agent"]}


def guide_agent(state: PlanningState) -> dict:
    """读取攻略文字；上传图片时先识别图片中的文字。"""
    payload = state.get("payload") or {}
    if payload.get("image_base64"):
        try:
            image = base64.b64decode(payload["image_base64"], validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid guide image encoding") from exc
        text = recognize_guide(image)
    else:
        text = str(payload.get("text") or "")
    if not text.strip():
        raise ValueError("Guide image or corrected text is required")
    return {"guide_text": text, "trace": state["trace"] + ["guide_agent"]}


def activity_agent(state: PlanningState) -> dict:
    """从攻略和兴趣整理活动、地点及建议，并保存攻略草稿。"""
    saved = state["plan"].get("planning", {})
    text = state.get("guide_text", state.get("payload", {}).get("text", saved.get("draft_text", saved.get("guide_text", ""))))
    plan = build_activities(state["plan"], text, _activity_places(state["plan"]))
    plan["planning"]["draft_text"] = text
    return {"plan": plan, "trace": state["trace"] + ["activity_agent"]}


def place_agent(state: PlanningState) -> dict:
    """为活动定位地图地点；未配置高德密钥时保留原计划。"""
    plan = locate_activities(state["plan"]) if config.AMAP_API_KEY else state["plan"]
    return {"plan": plan, "trace": state["trace"] + ["place_agent"]}


def generate_guide(state: PlanningState) -> dict:
    """自动生成入口：识别攻略并提取活动；没有可识别地点时要求人工补充。"""
    text = guide_agent(state)["guide_text"]
    fresh = deepcopy(state['plan'])
    for item in fresh.get('planning', {}).get('activities', []):
        if item.get('source') not in {'高德 POI', 'manual'}:
            for key in ('place', 'place_candidates', 'place_status', 'place_error', 'match_reason'):
                item.pop(key, None)
    plan = build_activities(fresh, text)
    if not plan["planning"].get("guide_places"):
        raise GuideNeedsInput("攻略中没有可识别的地点")
    plan["planning"]["guide_auto"] = True
    plan["planning"]["draft_text"] = text
    return {"plan": plan, "trace": state["trace"] + ["guide_agent", "activity_agent"]}


def search_places(state: PlanningState) -> dict:
    """批量搜索攻略地点并匹配地图候选，记录定位状态。"""
    return {"plan": search_and_match(state["plan"]), "trace": state["trace"] + ["place_search_agent", "place_match_agent"]}


def default_choices(state: PlanningState) -> dict:
    """从已定位的住宿候选中选择默认酒店，并记录自动选择的假设。"""
    plan = deepcopy(state["plan"])
    planning = plan["planning"]
    excluded = {x["id"] for x in planning.get("activities", []) if x.get("choice") == "exclude"}
    hotels = [x for x in planning.get("hotel_candidates", []) if has_coordinates(x.get("place")) and x["id"] not in excluded]
    chosen = next((x for x in hotels if x["id"] == planning.get("selected_hotel")), None)
    if not chosen and hotels:
        guide_ids = {x["id"] for x in planning.get("activities", []) if x.get("source") == "guide_text"}
        guide_ids.update(x["id"] for x in planning.get("activities", []) if x.get("source") == "lodging_search")
        chosen = sorted(hotels, key=lambda x: (x["id"] not in guide_ids, x.get("estimated_total") or 0))[0]
        planning["lodging_assumption"] = "按攻略住宿和已定位候选自动建议住宿，可修改"
    planning["selected_hotel"] = chosen["id"] if chosen else None
    return {"plan": plan, "trace": state["trace"] + ["default_choices"]}
