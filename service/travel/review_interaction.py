"""审核页交互内容和草稿续跑。"""

import asyncio
import re
from collections import OrderedDict
from dataclasses import asdict
from time import monotonic
from uuid import uuid4
from typing import Callable

from agent.chat.runtime.execution import ReviewExecution, ReviewStepTimeout
from agent.chat.review.workflow import ClarificationReply, ReviewContext, ReviewRequest, ReviewTurnResult, run_review_turn
from service.storage.planning_jobs import PlanConflict, fingerprint
from service.storage import planning_jobs, review_checkpoints
from service.storage.travel_db import get_plan
from service.travel.map_planning import day_map
from service.travel.review import preview_review_action, review_acceptance
from service.travel.review_public_reply import format_review_reply, public_text
from service.utils.model_client import create_model_client
from service.utils.runtime_log import record_event


class CheckpointSaveTimeout(ReviewStepTimeout):
    """保存可能已提交，依靠持久化断点判定最终结果。"""


class ReviewInteractions:
    """为审核页保留有时效、绑定草稿的交互动作。"""

    def __init__(self):
        """初始化进程内动作表。"""
        self.actions = OrderedDict()

    def _offer(self, scope: tuple, kind: str, value: dict) -> str:
        """签发不可预测的动作标识，限制进程内保留数量。"""
        action_id = uuid4().hex
        self.actions[action_id] = (monotonic() + 1800, scope, kind, value)
        if len(self.actions) > 512:
            self.actions.popitem(last=False)
        return action_id

    def _take(self, action_id: str, scope: tuple) -> tuple[str, dict]:
        """核对草稿后消费动作，避免重复应用。"""
        action = self.actions.get(action_id)
        if action is None or action[0] <= monotonic() or action[1] != scope:
            raise PlanConflict("此操作已过期或草稿已变化，请重新发起请求")
        del self.actions[action_id]
        return action[2], action[3]

    async def run(self, plan_id: str, model: str, payload: dict,
                  on_progress: Callable[[str], None] | None = None) -> dict:
        """处理新请求、澄清回复、地点点击或试算应用。"""
        if "conversation_id" in payload:
            def publish(message: str) -> None:
                """只发送可展示的运行进度。"""
                if on_progress is not None:
                    on_progress(public_text(message, "正在处理路线请求…", set()))

            return await self._run_checkpointed(plan_id, model, payload, ReviewExecution(on_progress=publish))
        version, edits, revision = payload.get("plan_version"), payload.get("operations"), payload.get("draft_revision")
        if (not isinstance(version, str) or not version or not isinstance(edits, list)
                or len(edits) > 1000 or type(revision) is not int or revision < 0):
            raise ValueError("草稿请求格式无效")
        scope = (plan_id, version, revision, fingerprint(edits))
        execution = ReviewExecution(on_progress=on_progress)
        draft = await execution.run(asyncio.to_thread(_load_draft, plan_id, version, edits),
                                    "重建当前草稿" if edits else "读取当前行程")
        message, selection, pending, reply = payload.get("message"), None, None, None
        action_id = payload.get("action_id")
        if action_id is not None:
            if not isinstance(action_id, str):
                raise ValueError("交互动作格式无效")
            retained_action = self.actions.get(action_id)
            kind, value = self._take(action_id, scope)
            if kind == "apply_preview":
                return {**value, "draft_revision": revision}
            message = value["message"]
            selection = value.get("selection")
            if kind == "select_places":
                try:
                    selection = _selected_places(payload, value)
                except ValueError:
                    self.actions[action_id] = retained_action
                    raise
            if kind == "choice":
                pending = value["pending"]
                reply = ClarificationReply(payload.get("option_index"), payload.get("custom_text"))
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ValueError("请输入有效的审核请求")
        history = payload.get("history", [])
        if not isinstance(history, list) or len(history) > 12 or any(
            not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
            or not isinstance(item.get("content"), str) or len(item["content"]) > 1200 for item in history
        ):
            raise ValueError("最近对话格式无效")
        result = await run_review_turn(
            ReviewRequest(message, pending, reply, selection, tuple(
                {"role": item["role"], "content": item["content"]} for item in history
            )),
            ReviewContext(plan_id, version, create_model_client(), model, on_progress=on_progress,
                          draft={"plan": draft, "plan_version": version, "selection": selection}),
        )
        if result.status == "conflict":
            raise PlanConflict(result.message)
        response = {"status": result.status, "message": result.message, "blocks": [], "draft_revision": revision,
                    "progress": result.progress}
        if result.clarification:
            prompt = asdict(result.clarification.prompt)
            action_id = self._offer(scope, "choice", {
                "message": message, "pending": result.clarification, "selection": selection,
            })
            response["blocks"].append({"type": "choice", **prompt, "action_id": action_id})
        if result.places and selection is None and result.status in {"answer", "partial"}:
            cards = [dict(place) for place in {p["place_id"]: p for p in result.places}.values()]
            groups = result.candidate_groups
            block = {"type": "place_candidates", "places": cards, "groups": groups}
            if (draft["planning"]["stage"] == "review" and groups and all(
                    group["selection_count"] is None or len(group["place_ids"]) >= group["selection_count"]
                    for group in groups)):
                block.update(selection_mode="multiple", action_id=self._offer(scope, "select_places", {
                    "message": message, "places": cards, "groups": groups, "mode": "multiple", "exact": None,
                }))
            response["blocks"].append(block)
        if result.status == "proposal":
            if len(edits) + len(result.operations) > 1000:
                raise ValueError("草稿操作过多，请先保存现有修改")
            update = await execution.run(asyncio.to_thread(_draft_update, result), "生成草稿地图")
            if selection:
                return {**update, "draft_revision": revision}
            action_id = self._offer(scope, "apply_preview", update)
            response["blocks"].append({
                "type": "change_preview", "action_id": action_id, "audit": update["audit"],
                "operations": update["operations"], "itinerary": update["plan"]["itinerary"],
            })
        elif result.snapshot and result.snapshot.get("blocking_issues"):
            response["blocks"].append({"type": "review_issues", "issues": result.snapshot["blocking_issues"]})
        return response

    async def _run_checkpointed(self, plan_id: str, model: str, payload: dict,
                                execution: ReviewExecution) -> dict:
        """为页面加载一次最新行程，恢复已认领的选择，或建立新的暂停点。"""
        conversation = payload["conversation_id"]
        revision = payload.get("draft_revision")
        if (not isinstance(conversation, str) or not re.fullmatch(r"[\w-]{1,128}", conversation)
                or type(revision) is not int or revision < 0):
            raise ValueError("对话请求格式无效")
        checkpoint = payload.get("action_id")
        if checkpoint is not None and (not isinstance(checkpoint, str) or not re.fullmatch(r"[a-f0-9]{32}", checkpoint)):
            raise ValueError("选择格式无效")
        state = {}
        if checkpoint:
            claim = asyncio.create_task(asyncio.to_thread(review_checkpoints.claim, checkpoint, plan_id, conversation))
            try:
                status, state = await asyncio.shield(claim)
            except asyncio.CancelledError:
                # The DB thread can acquire ownership after its caller was cancelled.
                status, _ = await asyncio.shield(claim)
                if status == "paused":
                    await asyncio.to_thread(review_checkpoints.finish, checkpoint)
                raise
            if status == "completed":
                if state.get("status") == "saved":
                    latest = await execution.run(asyncio.to_thread(_read_review_plan, plan_id), "核对保存结果")
                    if fingerprint(latest) != state["plan_version"]:
                        raise PlanConflict("这次修改已保存，行程此后又有更新，请刷新查看最新结果")
                return {**state, "draft_revision": revision}
        try:
            plan = await execution.run(asyncio.to_thread(_read_review_plan, plan_id), "读取最新行程")
            client = create_model_client()
            response, committed = await self._continue_checkpoint(
                plan, conversation, client, model, payload, execution, checkpoint, state
            )
            if checkpoint and not committed:
                await asyncio.to_thread(review_checkpoints.finish, checkpoint,
                                        response if response["status"] not in {"failed", "partial"} else None)
            response["draft_revision"] = revision
            return response
        except CheckpointSaveTimeout as exc:
            # 已发起写入的线程可能继续完成，保留认领，避免再次应用。
            return {"status": "save_unknown", "message": str(exc), "blocks": [], "draft_revision": revision}
        except (Exception, asyncio.CancelledError):
            if checkpoint:
                await asyncio.to_thread(review_checkpoints.finish, checkpoint)
            raise

    async def _continue_checkpoint(self, plan: dict, conversation: str, client, model: str,
                                   payload: dict, execution: ReviewExecution, checkpoint: str | None,
                                   state: dict) -> tuple[dict, bool]:
        """根据断点种类续跑，写入时提交本轮已经验收的同一份候选。"""
        kind = state.get("kind")
        version = fingerprint(plan)
        plan_id = plan["plan_id"]
        editable = plan["planning"]["stage"] == "review"
        message = state.get("message") if checkpoint else payload.get("message")
        request_context = state.get("request_context", state.get("intent"))
        selection = state.get("selection")
        if kind in {"select_place", "apply_preview"} and not editable:
            raise PlanConflict("行程已确认，请返回编辑后再修改")
        if kind == "apply_preview":
            update = state["update"]
            if version != state["version"]:
                operations = update["operations"]
                if _operation_targets(plan, operations) != state["targets"]:
                    raise PlanConflict("待修改的内容已变化，请重新提出修改")
                action = "review_changes" if operations else "optimize_routes"
                args = {"plan_version": version, **({"operations": operations} if operations else {})}
                candidate = await execution.run(asyncio.to_thread(preview_review_action, plan, action, args), "重新试算变更")
                acceptance = review_acceptance(plan, candidate)
                if not acceptance["accepted"]:
                    return _failure_response("试算未通过，请调整方案。", acceptance["blocking_issues"]), False
                rebuilt = ReviewTurnResult("proposal", "", version, operations,
                                           {"draft_plan": candidate["plan"], "audit": acceptance["audit"]})
                update = await execution.run(asyncio.to_thread(_draft_update, rebuilt), "生成路线预览")
                return await _preview_checkpoint(plan, conversation, update, "行程有更新，请确认新的试算结果。"), False
            response = await _commit_checkpoint(plan, checkpoint, update, execution)
            response = await _describe_saved(client, model, execution, plan, response)
            return response, True
        if kind == "choice":
            answer = _checkpoint_answer(payload, state["prompt"])
            previous = (request_context or {}).get("clarification")
            if previous:
                request_context = {**request_context, "clarification_answers": [
                    *request_context.get("clarification_answers", []), previous,
                ]}
            request_context = {**(request_context or {}), "clarification": {
                "question": state["prompt"]["question"], "answer": answer,
            }}
        if kind == "select_place":
            selection = _selected_places(payload, state)
        if not isinstance(message, str) or not message.strip() or len(message) > 4000:
            raise ValueError("请输入有效的审核请求")
        history = _review_history(payload)
        result = await run_review_turn(
            ReviewRequest(message, selection=selection, history=history, resume_context=request_context),
            ReviewContext(plan_id, version, client, model, on_progress=execution.on_progress,
                          draft={"plan": plan, "plan_version": version, "selection": selection, "persistent": True}),
        )
        identifiers = _internal_identifiers(plan, result.places)
        response = {"status": result.status, "blocks": [], "progress": result.progress}
        fallback = "已完成路线检查。"
        if result.clarification:
            prompt = asdict(result.clarification.prompt)
            token = await _offer_checkpoint(plan_id, conversation, {
                "kind": "choice", "message": message, "selection": selection,
                "request_context": result.continuation, "prompt": prompt,
            })
            question = public_text(prompt["question"], "请补充这次调整需要的信息。", identifiers)
            options = [public_text(option, "", identifiers) for option in prompt["options"]]
            if not all(options):
                options = []  # 无法安全展示时保留自定义答复。
            response["blocks"].append({"type": "choice", "question": question, "options": options,
                                       "allow_custom": True, "action_id": token})
            fallback = question
        if result.places and selection is None and result.status in {"answer", "partial"}:
            places = list({place["place_id"]: place for place in result.places}.values())
            block = {"type": "place_candidates", "places": [
                {"place_id": p["place_id"], "name": public_text(p.get("name"), "候选地点", identifiers),
                 "address": public_text(p.get("address"), "", identifiers)} for p in places
            ]}
            if editable:
                groups = result.candidate_groups
                if groups and all(group["selection_count"] is None or len(group["place_ids"]) >= group["selection_count"]
                       for group in groups):
                    token = await _offer_checkpoint(plan_id, conversation, {
                        "kind": "select_place", "message": message, "places": places,
                        "groups": groups, "mode": "multiple", "exact": None,
                        "request_context": result.continuation,
                    })
                    block.update(action_id=token, selection_mode="multiple", groups=groups)
                else:
                    response["status"] = "partial"
                    fallback = "部分分组的候选不足，仍需补齐要求数量的地点。"
            response["blocks"].append(block)
            if response["status"] != "partial":
                fallback = "找到了这些地点，请查看候选。"
        if result.status == "proposal":
            update = await execution.run(asyncio.to_thread(_draft_update, result), "生成路线预览")
            if selection and checkpoint:
                response = await _commit_checkpoint(plan, checkpoint, update, execution)
                response = await _describe_saved(client, model, execution, plan, response)
                return response, True
            fallback = "请确认试算后的行程。"
            response = await _preview_checkpoint(plan, conversation, update, fallback)
        elif result.status == "failed":
            issues = (result.snapshot or {}).get("blocking_issues", [])
            fallback = ("行程已确认，可以查询。若需修改，请先返回编辑。"
                        if not editable else "试算未通过，请核对下方问题后再试。"
                        if issues else "这次调整未完成，请确认日期和地点后重试。")
            response = _failure_response(fallback, issues, identifiers)
        if result.progress and response["status"] in {"partial", "failed"}:
            response["progress"] = result.progress
            fallback = (f"已取得 {len(result.progress.get('places', []))} 个候选和 "
                        f"{len(result.progress.get('routes', []))} 段路线结果，本次调整尚未完成。")
            remaining = result.progress.get("remaining", [])
            public_remaining = [public_text(item, "继续完成行程审核与调整。", identifiers) for item in remaining]
            response["blocks"].append({"type": "review_issues", "issues": [
                {"message": item} for item in public_remaining
            ]})
            fallback += " " + "；".join(public_remaining)
        safe = fallback if result.status in {"proposal", "failed", "partial", "clarify"} else public_text(
            result.message, fallback, identifiers
        )
        response["message"] = await format_review_reply(client, model, execution, {
            "状态": response["status"], "说明": safe,
            "地点": [public_text(p["name"], "候选地点", identifiers) for p in result.places],
            "剩余事项": result.progress.get("remaining", []),
        }, safe, identifiers)
        return response, False


async def _offer_checkpoint(plan_id: str, conversation: str, state: dict) -> str:
    """生成绑定到本对话的可恢复凭据。"""
    token = uuid4().hex
    await asyncio.to_thread(review_checkpoints.offer, token, plan_id, conversation, state)
    return token


def _read_review_plan(plan_id: str) -> dict:
    """加载已保存行程，并限定审核助手的可用阶段。"""
    plan, error = get_plan(plan_id)
    if error or plan is None:
        raise ValueError("未找到当前行程")
    if plan.get("planning", {}).get("stage") not in {"review", "confirmed"}:
        raise ValueError("行程尚未进入路线审核")
    return plan


def _checkpoint_answer(payload: dict, prompt: dict) -> str:
    """只接受该暂停点的有效选项或自定义答复。"""
    index, custom = payload.get("option_index"), payload.get("custom_text")
    if type(index) is int and 0 <= index < len(prompt["options"]) and custom is None:
        return prompt["options"][index]
    if index is None and isinstance(custom, str) and 0 < len(custom.strip()) <= 1200:
        return custom.strip()
    raise ValueError("请选择一个有效选项，或填写自定义答复")


def _selected_places(payload: dict, state: dict) -> dict:
    """按照服务端候选和数量约束，取回原始 POI。"""
    indexes = payload.get("option_indexes")
    if (not isinstance(indexes, list) or not indexes or any(
        type(index) is not int or not 0 <= index < len(state["places"]) for index in indexes
    ) or len(set(indexes)) != len(indexes)):
        raise ValueError("请选择列表中的地点")
    mode, exact = state["mode"], state["exact"]
    if (exact is not None and len(indexes) != exact) or (mode == "single" and len(indexes) != 1):
        raise ValueError("请选择要求数量的地点")
    selected = [state["places"][index] for index in indexes]
    selected_ids = {place["place_id"] for place in selected}
    groups = state.get("groups", [])
    allowed_ids = {key for group in groups for key in group["place_ids"]}
    if groups and selected_ids - allowed_ids:
        raise ValueError("请选择分组中的地点")
    for group in groups:
        count = len(selected_ids.intersection(group["place_ids"]))
        expected = group["selection_count"]
        if (expected is None and count == 0) or (expected is not None and count != expected):
            raise ValueError(f"请按「{group['label']}」要求选择地点")
    return {"kind": "select_place", "places": selected, "groups": groups}


def _review_history(payload: dict) -> tuple[dict[str, str], ...]:
    """只保留合法的公开对话文字。"""
    history = payload.get("history", [])
    if not isinstance(history, list) or len(history) > 12 or any(
        not isinstance(item, dict) or item.get("role") not in ("user", "assistant")
        or not isinstance(item.get("content"), str) or len(item["content"]) > 1200 for item in history
    ):
        raise ValueError("最近对话格式无效")
    return tuple({"role": item["role"], "content": item["content"]} for item in history)


def _internal_identifiers(plan: dict, places: list = ()) -> set[str]:
    """收集可能被拼入用户回复的标识。"""
    ids = {plan["plan_id"], fingerprint(plan)}
    for item in plan.get("itinerary", []):
        ids.update((item.get("item_id"), item.get("reference_id"), (item.get("place") or {}).get("place_id")))
    ids.update(place.get("place_id") for place in places)
    return {value for value in ids if isinstance(value, str)}


def _failure_response(message: str, issues: list, identifiers: set[str] = frozenset()) -> dict:
    """给用户报告可公开的审核问题。"""
    return {"status": "failed", "message": message, "blocks": [{"type": "review_issues", "issues": [
        {"message": public_text(issue.get("message"), "有一项行程风险需要核对。", identifiers)} for issue in issues
    ]}] if issues else []}


def _operation_targets(plan: dict, operations: list) -> dict:
    """记录预览依赖的地点、顺序及字段，版本变化后只检查这些数据。"""
    items = {item["item_id"]: item for item in [
        *plan.get("itinerary", []), *plan["planning"].get("unscheduled_items", [])
    ]}
    targets = {}
    for op in operations:
        args, action = op["payload"], op["action"]
        if action == "update_budget":
            targets["budget"] = plan.get("budget")
        for field in ("item_id", "before_item_id", "origin_item_id", "destination_item_id"):
            if args.get(field) in items:
                item = items[args[field]]
                targets[f"item:{item['item_id']}"] = fingerprint(item)
        if args.get("activity_id") and action in {"update_activity", "complete_lodging"}:
            key = args["activity_id"]
            targets[f"activity:{key}"] = fingerprint([
                item for item in items.values() if item.get("reference_id") == key
            ])
        if action == "day_departure":
            day = str(args["day"])
            targets[f"departure:{day}"] = plan["planning"].get("day_departures", {}).get(day)
        if action == "acknowledge_purchase":
            reference = args["reference_id"]
            targets[f"purchase:{reference}"] = fingerprint([
                row for row in plan["planning"].get("purchases", []) if row.get("reference_id") == reference
            ])
        if action == "route_mode":
            pair = (args["origin_item_id"], args["destination_item_id"])
            targets[f"route:{pair[0]}:{pair[1]}"] = fingerprint([
                row for row in plan.get("routes", []) if (row["origin_item_id"], row["destination_item_id"]) == pair
            ])
    return targets


async def _preview_checkpoint(plan: dict, conversation: str, update: dict, message: str) -> dict:
    """持久化候选，页面只显示日程与公开风险。"""
    ids = _internal_identifiers(update["plan"])
    token = await _offer_checkpoint(plan["plan_id"], conversation, {
        "kind": "apply_preview", "version": fingerprint(plan), "update": update,
        "targets": _operation_targets(plan, update["operations"]),
    })
    block = {
        "type": "change_preview", "action_id": token, "operation_count": len(update["operations"]),
        "itinerary": [{"day": item["day"], "time_of_day": item["time_of_day"],
                       "title": public_text(item["title"], "行程地点", ids)} for item in update["plan"]["itinerary"]],
        "audit": {"issues": [{"message": public_text(issue["message"], "有一项行程风险需要核对。", ids)}
                             for issue in update["audit"].get("issues", [])]},
    }
    return {"status": "proposal", "message": message, "blocks": [block]}


async def _commit_checkpoint(plan: dict, checkpoint: str, update: dict, execution: ReviewExecution) -> dict:
    """在同一个事务中写入行程和已完成断点。"""
    response = {"status": "saved", "message": "已保存调整，路线和日程已更新。",
                "audit": update["audit"], "maps": update["maps"], "blocks": []}
    save = asyncio.create_task(asyncio.to_thread(
        planning_jobs.save_review_changes, plan["plan_id"], plan, update["plan"],
        checkpoint_id=checkpoint, response=response,
    ))
    try:
        return await execution.run(asyncio.shield(save), "保存最新行程")
    except asyncio.CancelledError:
        # Release is safe only after this transaction committed or rolled back.
        await asyncio.shield(save)
        raise
    except ReviewStepTimeout as exc:
        def record_late_save(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            error = task.exception()
            record_event("checkpoint_save_finished", {
                "checkpoint_id": checkpoint, "status": "failed" if error else "completed",
                "error": str(error) if error else None,
            })

        save.add_done_callback(record_late_save)
        raise CheckpointSaveTimeout("保存超时，结果待核实，请刷新行程再查看。") from exc


async def _describe_saved(client, model: str, execution: ReviewExecution, before: dict, result: dict) -> dict:
    """保存后按可核实的日程差异生成公开回复。"""
    identifiers = _internal_identifiers(before) | _internal_identifiers(result["plan"])
    previous = {(item.get("reference_id") or item["item_id"], item["category"]): item
                for item in before.get("itinerary", [])}
    changes = []
    for item in result["plan"].get("itinerary", []):
        old = previous.pop((item.get("reference_id") or item["item_id"], item["category"]), None)
        title = public_text(item["title"], "行程地点", identifiers)
        if old is None:
            changes.append(f"第 {item['day']} 天 {item['time_of_day']} 新增「{title}」")
        elif (old["day"], old["time_of_day"], old["title"]) != (item["day"], item["time_of_day"], item["title"]):
            changes.append(f"「{title}」调整到第 {item['day']} 天 {item['time_of_day']}")
    for item in previous.values():
        changes.append(f"移除「{public_text(item['title'], '行程地点', identifiers)}」")
    if before.get("budget") != result["plan"].get("budget"):
        changes.append(f"总预算调整为 {result['plan']['budget']} 元")
    fallback = result["message"] + (" " + "；".join(changes[:4]) + "。" if changes else "")
    message = await format_review_reply(client, model, execution,
                                       {"状态": "已保存", "已确认变更": changes[:12]}, fallback, identifiers)
    return {**result, "message": message}


def _load_draft(plan_id: str, version: str, edits: list) -> dict:
    """使用当前数据库版本和页面操作重建草稿，禁止写库。"""
    plan, error = get_plan(plan_id)
    if error or plan is None:
        raise ValueError("未找到当前行程")
    if fingerprint(plan) != version:
        raise PlanConflict("已保存的行程发生变化，请刷新后重试")
    stage = plan.get("planning", {}).get("stage")
    if stage not in {"review", "confirmed"}:
        raise ValueError("行程尚未进入路线审核")
    if edits and stage != "review":
        raise ValueError("已确认行程只能查询，请先返回编辑行程")
    if not edits:
        return plan
    return preview_review_action(plan, "review_changes", {"plan_version": version, "operations": edits})["plan"]


def _draft_update(result: ReviewTurnResult) -> dict:
    """从已验收候选生成页面所需的行程、路段和各日地图。"""
    plan = result.snapshot["draft_plan"]
    maps = {}
    for day in sorted({item["day"] for item in plan["itinerary"]}):
        try:
            maps[day] = day_map(plan, day)
        except ValueError:
            continue
    return {"status": "draft_updated", "message": "已加入草稿，请在审核页统一保存。",
            "blocks": [], "operations": result.operations, "plan": plan,
            "audit": result.snapshot["audit"], "maps": maps}
