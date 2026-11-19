"""独立运行一轮路线审核，由应用统一提交一次。"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable

from jsonschema import ValidationError, validate

from agent.tools.dispatcher import ToolExecutor, validate_arguments
from agent.tools.mcp import private_tool_session
from pydantic import ValidationError as ModelValidationError
from agent.chat.runtime.rewrite import rewrite_request
from agent.chat.review.children import create_review_handlers
from agent.tools.retry import REVIEW_MODEL_STEPS, REVIEW_TOOL_LIMIT
from agent.chat.runtime.execution import REVIEW_STEP_TIMEOUT, ReviewExecution, ReviewStepTimeout
from agent.chat.review.parent import Clarification, ReviewDelegations, run_parent
from agent.chat.schemas.tasks import TaskContext, TaskHandler
from agent.tools.context import ToolContext
from agent.tools.schemas.errors import ErrorCode
from agent.tools.schemas.outputs import legacy_result
from agent.tools.implementations.review import review_context_for_day, review_registry


@dataclass(frozen=True)
class PendingClarification:
    plan_id: str
    plan_version: str
    original_message: str
    prompt: Clarification
    answers: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ClarificationReply:
    option_index: int | None = None
    custom_text: str | None = None


@dataclass(frozen=True)
class ReviewRequest:
    """用户原始请求及由交互服务验证的续跑输入。"""
    message: str
    pending: PendingClarification | None = None
    reply: ClarificationReply | None = None
    selection: dict | None = None
    history: tuple[dict[str, str], ...] = ()
    resume_context: dict | None = None


@dataclass(frozen=True)
class ReviewContext:
    """服务器绑定的行程、工具权限、草稿与执行预算。"""
    plan_id: str
    plan_version: str
    client: Any
    model: str
    tool_context: ToolContext = field(default_factory=ToolContext)
    save_requested: bool = False
    handlers: dict[str, TaskHandler] | None = None
    max_steps: int = REVIEW_MODEL_STEPS
    max_tools: int = REVIEW_TOOL_LIMIT
    max_tasks: int = 12
    concurrency: int = 3
    timeout_seconds: float = REVIEW_STEP_TIMEOUT
    draft: dict | None = None
    on_progress: Callable[[str], None] | None = None


@dataclass
class ReviewTurnResult:
    """供调用方展示或应用到页面草稿的单轮结果。"""
    status: str
    message: str
    plan_version: str
    operations: list = field(default_factory=list)
    snapshot: dict | None = None
    proposal_kind: str | None = None
    clarification: PendingClarification | None = None
    places: list = field(default_factory=list)
    continuation: dict | None = None
    progress: dict = field(default_factory=dict)
    candidate_groups: list = field(default_factory=list)


# ============合并澄清答复 ============
def _clarification_answers(request: ReviewRequest, context: ReviewContext) -> tuple[tuple[str, str], ...]:
    pending, reply = request.pending, request.reply
    if pending is None and reply is None:
        return ()
    if pending is None or reply is None or (
        pending.plan_id, pending.plan_version, pending.original_message
    ) != (context.plan_id, context.plan_version, request.message):
        raise ValueError("澄清答复与本轮请求或行程版本不匹配")
    index, custom = reply.option_index, reply.custom_text
    if type(index) is int and 0 <= index < len(pending.prompt.options) and custom is None:
        answer = pending.prompt.options[index]
    elif index is None and isinstance(custom, str) and custom.strip():
        answer = custom.strip()
    else:
        raise ValueError("请选择一个有效选项，或填写自定义答复")
    return (*pending.answers, (pending.prompt.question, answer))


# ============运行审核单轮 ============
async def run_review_turn(request: ReviewRequest, context: ReviewContext) -> ReviewTurnResult:
    if (not request.message.strip() or not context.plan_id or not context.plan_version
            or min(context.max_steps, context.max_tools, context.max_tasks,
                   context.concurrency, context.timeout_seconds) <= 0):
        raise ValueError("审核请求、行程版本或执行预算无效")
    read_registry = (review_registry(context.plan_id, draft=context.draft)
                     if context.draft is not None else review_registry(context.plan_id))
    async with private_tool_session(read_registry, ToolContext(actor_id=context.tool_context.actor_id)) as bound:
        return await _run_review_turn(request, context, bound, read_registry)


async def _run_review_turn(request: ReviewRequest, context: ReviewContext, read_registry, source_registry) -> ReviewTurnResult:
    reader = ToolExecutor(read_registry)
    read_context = ToolContext(actor_id=context.tool_context.actor_id)
    execution = ReviewExecution(context.timeout_seconds, context.on_progress)

    # ============读取数据 ============
    async def read(name, args):
        """为每次读工具单独计时，并报告实际调用的能力。"""
        labels = {
            "get_review_context": "读取行程",
            "search_review_places": "查找地点",
            "search_review_hotels": "查找住宿",
            "compare_review_route": "比较交通路线",
            "preview_review_changes": "试算调整后的行程",
        }
        label = labels.get(name, "调用审核工具")
        query = args.get("query")
        if name in {"search_review_places", "search_review_hotels"} and isinstance(query, str) and query.strip():
            label = f"查找「{query.strip()[:80]}」"
        if execution.on_progress:
            execution.on_progress(f"正在{label}…")
        return await reader.execute(name, args, read_context, allowed=read_registry.names,
                                    timeout_seconds=execution.timeout_seconds)

    # ============准备上下文 ============
    async def prepare():
        initial = await read("get_review_context", {})
        if not initial.success:
            return ReviewTurnResult("failed", initial.message, context.plan_version)
        snapshot = initial.data
        if snapshot["plan_version"] != context.plan_version:
            return ReviewTurnResult("conflict", "行程已变化，请刷新后重试",
                                    snapshot["plan_version"], snapshot=snapshot)
        answers = _clarification_answers(request, context)
        if request.resume_context is not None:
            request_context = {
                **request.resume_context,
                "selection": request.selection,
                # 同步刷新最新传入的对话历史，避免沿用旧轮次快照
                "conversation_history": list(request.history) if request.history else request.resume_context.get(
                    "conversation_history", []),
            }
        else:
            rewritten = await rewrite_request(
                client=context.client, model=context.model, text=request.message,
                history=request.history, current_plan=_review_summary(snapshot),
                selection=request.selection, execution=execution,
            )
            request_context = {"original_text": request.message, "rewrite": rewritten,
                               "conversation_history": list(request.history),
                               "clarification_answers": [], "selection": request.selection}
        prior_answers = [(item["question"], item["answer"])
                         for item in request_context.get("clarification_answers", [])]
        current = request_context.get("clarification")
        if current:
            prior_answers.append((current["question"], current["answer"]))
        combined = tuple(dict.fromkeys([*prior_answers, *answers]))
        request_context["clarification_answers"] = [
            {"question": q, "answer": a} for q, a in combined
        ]
        decision = await _plan(request_context, context, snapshot, source_registry, execution)
        return snapshot, decision, request_context

    try:
        prepared = await prepare()
    except ReviewStepTimeout as exc:
        return ReviewTurnResult("failed", str(exc), context.plan_version)

    if isinstance(prepared, ReviewTurnResult):
        return prepared
    snapshot, decision, request_context = prepared
    if decision.kind not in {"changes", "optimize"}:
        pending = None
        question = decision.clarification
        if question is None and decision.kind == "clarify" and context.draft and context.draft.get("persistent"):
            question = Clarification(decision.message or "请补充这次修改需要的信息。", ())
        if question:
            pending = PendingClarification(context.plan_id, context.plan_version, request.message,
                                           question, tuple((item["question"], item["answer"])
                                                           for item in request_context["clarification_answers"]))
        return ReviewTurnResult(decision.kind, question.question if question else decision.message, context.plan_version,
                                snapshot=snapshot, places=decision.places, clarification=pending,
                                continuation=request_context, progress=decision.progress,
                                candidate_groups=decision.candidate_groups)
    result = await _finish_proposal(decision, context, snapshot, read, execution)
    result.progress = deepcopy(decision.progress)
    result.places = decision.places or result.progress.get("places", [])
    if result.status in {"failed", "conflict"} and result.progress:
        result.progress["remaining"] = [*result.progress.get("remaining", []), "完成行程提案的试算与提交"]
        result.progress.setdefault("failures", []).append({"stage": "finalize", "reason": result.message})
    return result


# ============生成审核方案 ============
def _review_summary(snapshot: dict) -> dict:
    """给模型提供城市、日期范围和状态，具体行程由子智能体按需读取。"""
    return {key: snapshot[key] for key in ("plan_version", "stage", "destination", "start_date", "end_date")
            if key in snapshot}


# ============安排模型与子任务 ============
async def _plan(request_context, context, snapshot, source_registry, execution: ReviewExecution):
    reader = ToolExecutor(source_registry)
    read_context = ToolContext(actor_id=context.tool_context.actor_id)
    # ============调用只读工具 ============
    async def read_tool(name, args):
        if name == "preview_review_changes" and args.get("plan_version") != context.plan_version:
            return legacy_result(name, False, message="试算必须使用本轮绑定的行程版本", error_type="PlanConflict")
        if name == "get_review_context":
            day = args.get("day")
            if args and (set(args) != {"day"} or type(day) is not int or day < 1):
                return legacy_result(name, False, message="请输入有效的行程日期序号。", error_type="validation_error")
            return legacy_result(name, True, deepcopy(review_context_for_day(snapshot, day)))
        # This handler runs inside the child's private MCP server. The client Dispatcher owns retries.
        return await reader.execute(name, args, read_context, allowed=source_registry.names)

    write_registry = review_registry(context.plan_id, writable=True)
    operations_schema = write_registry.get("apply_review_changes").input_schema["properties"]["operations"]
    handlers = context.handlers
    if handlers is None:
        handlers = create_review_handlers(context.client, context.model, source_registry.ollama_schemas(), operations_schema, execution,
                                          max_tools=context.max_tools, max_steps=context.max_steps)
        selection = request_context.get("selection")
        if selection and selection.get("kind") == "select_place" and "places" in selection:
            # 恢复点已保存用户选择，不再执行同一批候选搜索。
            handlers.pop("search_places", None)
    summary = _review_summary(snapshot)
    delegations = ReviewDelegations(handlers, TaskContext({**summary, "selection": request_context.get("selection")}, read_tool), context.max_tasks)
    return await run_parent(client=context.client, model=context.model, request_context=request_context,
                            snapshot=summary, delegations=delegations, max_steps=context.max_steps,
                            max_tools=context.max_tools, execution=execution, concurrency=context.concurrency)



# ============处理最终提案 ============
async def _finish_proposal(decision, context, snapshot, read, execution: ReviewExecution):
    if snapshot["stage"] != "review":
        return ReviewTurnResult("failed", "行程已确认，只能查询。请返回编辑行程后再修改。", context.plan_version)
    registry = review_registry(context.plan_id, writable=True)
    name = "apply_review_changes" if decision.kind == "changes" else "optimize_review_routes"
    args = {"plan_version": context.plan_version}
    if decision.kind == "changes":
        args["operations"] = decision.operations
    try:
        validate_arguments(registry.get(name), args)
    except (ValidationError, ModelValidationError):
        return ReviewTurnResult("failed", "审核提案不符合工具参数要求。", context.plan_version)
    if not context.save_requested:
        if context.draft is not None and decision.kind == "optimize" and not context.draft.get("persistent"):
            return ReviewTurnResult("failed", "草稿暂只支持可逐项保存的编辑，请使用页面的路线优化功能。",
                                    context.plan_version)
        try:
            trial = await read("preview_review_changes", args)
        except ReviewStepTimeout as exc:
            return ReviewTurnResult("failed", str(exc), context.plan_version)
        if not trial.success:
            status = "conflict" if trial.error_type == "PlanConflict" else "failed"
            return ReviewTurnResult(status, trial.message, context.plan_version)
        return ReviewTurnResult("proposal" if trial.data["accepted"] else "failed",
                                decision.message if trial.data["accepted"] else "试算未通过验收。",
                                context.plan_version, trial.data.get("operations", decision.operations),
                                trial.data, decision.kind)
    if snapshot["stage"] != "review" or "review:write" not in context.tool_context.permissions:
        return ReviewTurnResult("failed", "当前请求无权保存审核变更", context.plan_version)

    try:
        async with private_tool_session(registry, context.tool_context) as bound:
            result = await execution.run(
                ToolExecutor(bound).execute(name, args, context.tool_context, allowed={name}, timeout_seconds=None),
                "保存审核变更",
            )
    except ReviewStepTimeout:
        # 线程中已经开始的写入可能继续，不能自动重试。
        return ReviewTurnResult("failed", "保存超时，结果待核实，请重新读取行程。", context.plan_version)
    if result.success:
        saved = result.data
        applied = saved.get("applied", True)
        message = "已保存审核变更。" if applied else saved.get("optimization_message") or "路线未调整。"
        return ReviewTurnResult("saved" if applied else "answer", message,
                                saved["plan_version"], decision.operations, saved)
    if result.error_type == "PlanConflict":
        return ReviewTurnResult("conflict", result.message, context.plan_version)
    if result.error_type == "review_rejected":
        return ReviewTurnResult("failed", result.message, context.plan_version, snapshot=result.error.details["preview"])
    if result.error.code in {ErrorCode.INVALID_ARGUMENT, ErrorCode.PERMISSION_DENIED}:
        return ReviewTurnResult("failed", result.message, context.plan_version)
    try:
        latest = await read("get_review_context", {})
    except ReviewStepTimeout:
        latest = None
    current = latest.data if latest is not None and latest.success else None
    return ReviewTurnResult("failed", "保存结果未知，请核对最新行程后再试。",
                            current["plan_version"] if current else context.plan_version, snapshot=current)
