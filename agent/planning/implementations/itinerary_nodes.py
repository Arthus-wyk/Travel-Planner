"""Prepare and revise itinerary proposals."""
from __future__ import annotations
from copy import deepcopy
from hashlib import sha256
import json
from agent.planning.implementations.itinerary import (
    ItineraryInputError,
    ItineraryOutputError,
    generate_itinerary_proposal,
)
from service import config
from service.utils.model_client import get_model
from service.travel.guide import GuideNeedsInput
from service.travel.planning import build_activities, draft_itinerary
from service.travel.map_planning import locate_activities, has_coordinates
from agent.planning.schemas.state import PlanningState


def prepare_draft(state: PlanningState) -> dict:
    """准备行程输入：同步攻略、筛出未定位地点并重置本轮提案次数与反馈。"""
    plan, payload = state["plan"], state.get("payload", {})
    text = plan["planning"].get("guide_text", "") if state["action"] == "generate_from_guide" else payload.get("text", plan["planning"].get("guide_text", ""))
    if not isinstance(text, str) or len(text) > 20000:
        raise ValueError("攻略文字最长20000字")
    if text != plan["planning"].get("guide_text", ""):
        plan = build_activities(plan, text)
        if config.AMAP_API_KEY:
            plan = locate_activities(plan)
    pending_ids = {x.get("reference_id") for x in plan["planning"].get("unscheduled_items", []) if x.get("manual_pending")}
    forced = []
    for item in plan["planning"].get("activities", []):
        if item.get("choice") == "exclude" or item.get("category") == "hotel":
            continue
        if item["id"] in pending_ids or not has_coordinates(item.get("place")):
            forced.append({"activity_id": item["id"], "reason": item.get("place_error") or "位置尚未确认，暂不安排"})
    snapshot = {key: plan.get(key) for key in ("destination", "departure_city", "start_date", "end_date", "travelers", "budget", "pace", "interests", "constraints")}
    snapshot["planning"] = {key: value for key, value in plan["planning"].items() if key not in {"generation", "stage", "unscheduled_items", "unplaced_activities", "needs_replan"}}
    digest = sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    eligible = [x for x in plan['planning'].get('activities', [])
                if x.get('choice') != 'exclude' and x.get('category') != 'hotel']
    if not any(has_coordinates(x.get('place')) and x['id'] not in pending_ids for x in eligible):
        reasons = '；'.join(dict.fromkeys(entry['reason'] for entry in forced))
        exc = GuideNeedsInput(f'地图未能可靠匹配攻略中的游玩地点：{reasons}' if reasons else '地图未能可靠匹配攻略中的游玩地点')
        exc.plan = plan
        raise exc
    return {"plan": plan, "attempts": 0, "format_attempts": 0, "revision_attempts": 0, "revision_failed": False,
            "feedback": [], "proposal_error": "", "itinerary_retry_history": [], "forced_unscheduled": forced,
            "input_hash": digest, "trace": state["trace"] + ["prepare_draft"]}


def itinerary_agent(state: PlanningState) -> dict:
    """让模型安排每日地点；格式错误写入反馈并重试，最多生成三次提案。"""
    attempts = state.get("attempts", 0) + 1
    format_attempts = state.get("format_attempts", 0) + 1
    model_plan = deepcopy(state["plan"])
    forced = state.get("forced_unscheduled", [])
    forced_ids = {x["activity_id"] for x in forced}
    for activity in model_plan["planning"].get("activities", []):
        if activity["id"] in forced_ids:
            activity["choice"] = "exclude"
    # 同一次排序任务保留首轮输入和失败回复，成功或降级后清空校验重试记录。
    history = state.get("itinerary_retry_history", [])
    feedback = state.get("feedback", [])
    try:
        if history:
            proposal = generate_itinerary_proposal(model_plan, feedback, history=history)
        else:
            proposal = generate_itinerary_proposal(model_plan, feedback)
    except ItineraryInputError:
        raise
    except ValueError as exc:
        from service.utils.request_log import log_error
        log_error(exc, stage="itinerary_agent", attempt=attempts, format_attempt=format_attempts)
        if format_attempts >= 3:
            if state.get("revision_attempts", 0) and (state["plan"]["planning"].get("generation") or {}).get("contract_version") == 3:
                plan = deepcopy(state["plan"])
                plan["planning"].setdefault("generation_issues", []).append({"type": "revision_failed", "severity": "high",
                    "message": "自动调整未能满足全部要求，已保留上一份完整草稿及风险，请调整日期或停留时长。", "target": None})
                return {"plan": plan, "proposal_error": "", "revision_failed": True, "itinerary_retry_history": [],
                        "trace": state["trace"] + ["itinerary_agent"]}
            from datetime import date
            days = (date.fromisoformat(model_plan['end_date']) - date.fromisoformat(model_plan['start_date'])).days + 1
            places = [x for x in model_plan['planning'].get('activities', [])
                      if x.get('choice') != 'exclude' and x.get('category') != 'hotel']
            entries = [{'day': day, 'visits': []} for day in range(1, days + 1)]
            for index, item in enumerate(places):
                day = item.get('day')
                if type(day) is not int or not 1 <= day <= days:
                    day = index * days // len(places) + 1
                entries[day - 1]['visits'].append({'activity_id': item['id']})
            plan = draft_itinerary(state['plan'], {'days': entries, 'assumptions': [], 'constraint_results': []})
            plan['planning']['generation'].update(input_hash=state['input_hash'], revisions=attempts - 1, model=get_model())
            plan['planning'].setdefault('generation_issues', []).append({
                'type': 'ordering_fallback', 'severity': 'high', 'target': None,
                'message': '排序模型连续三次失败，已按指定日期和原文顺序生成规则草稿，请核对每日安排。'})
            return {'plan': plan, 'attempts': attempts, 'format_attempts': 0,
                    'proposal_error': '', 'itinerary_retry_history': [], 'trace': state['trace'] + ['itinerary_agent']}
        output = exc.output if isinstance(exc, ItineraryOutputError) else None
        return {"attempts": attempts, "format_attempts": format_attempts, "proposal_error": str(exc),
                "itinerary_retry_history": [*history, [output, str(exc)[:1000]]],
                "trace": state["trace"] + ["itinerary_agent"]}
    plan = draft_itinerary(state["plan"], proposal)
    plan["planning"]["generation"].update(input_hash=state["input_hash"], revisions=attempts - 1, model=get_model())
    return {"plan": plan, "attempts": attempts, "format_attempts": 0,
            "proposal_error": "", "itinerary_retry_history": [], "trace": state["trace"] + ["itinerary_agent"]}


def after_itinerary(state: PlanningState) -> str:
    if state.get("revision_failed"):
        return "audit_agent"
    return "itinerary_agent" if state.get("proposal_error") else "route_agent"
