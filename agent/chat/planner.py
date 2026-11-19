"""Shared agent entry point; owns private session initialization and explicit results."""
import asyncio
import json
from copy import deepcopy

from service.utils.runtime_log import trace_run, record_event, current_run
from agent.chat.runtime import memory
from agent.chat.runtime.context import AgentContext
from agent.chat.runtime.agent_loop import build_graph
from agent.chat.prompts import build_system_prompt
from agent.chat.runtime.rewrite import rewrite_request
from agent.chat.schemas.state import AgentCancelledError, AgentRequest, AgentResult, AgentSession, AgentState

GRAPH = build_graph()


def result_from_state(state: AgentState) -> AgentResult:
    plan = state.get("plan")
    if plan is None:
        plan = next((event["result"]["plan"] for event in reversed(state.get("evidence", []))
                     if isinstance(event.get("result"), dict) and isinstance(event["result"].get("plan"), dict)), None)
    return AgentResult(answer=state.get("answer", ""), status=state.get("status", "failed"),
        rewrite=state.get("rewrite", {}), output=state.get("output"), evidence=state.get("evidence", []),
        new_messages=state.get("new_messages", []), tool_events=state.get("tool_events", []), plan=plan,
        output_error=state.get("output_error"))


async def run_turn(request: AgentRequest, context: AgentContext) -> AgentResult:
    if not request.text.strip():
        raise ValueError("User input must not be empty")
    if context.max_rounds < 1:
        raise ValueError("max_rounds must be positive")
    owns_trace = current_run.get() is None
    with trace_run(context.transcript, model=context.model, text=request.text) as run:
        result = await _run_turn(request, context)
        if run is not None and owns_trace:
            run.finish(result.status, result.answer)
        return result


async def _run_turn(request: AgentRequest, context: AgentContext) -> AgentResult:
    session = context.session
    if session is None:
        session = AgentSession()
        context.session = session
    rewrite = request.rewrite or {"original_text": request.text, "rewritten_text": request.text,
                                 "references": [], "unresolved_references": []}
    if context.role == "chat" and context.system_prompt is None and context.enable_rewrite and request.rewrite is None:
        rewrite = await rewrite_request(client=context.client, model=context.model, text=request.text,
            history=request.history, current_plan=request.current_plan, selection=request.selection,
            execution=context.execution)
    policy, recall_context = memory.recall(request.text, context.memory_enabled)
    if not session.messages:
        session.messages.append({"role": "system", "content": context.system_prompt or build_system_prompt(request.current_plan)})
        session.messages.extend(deepcopy(request.history))
        if context.memory_enabled:
            session.messages.append({"role": "user", "content": recall_context})
    signature = json.dumps({"task": request.text, "extra_data": request.extra_data,
                            "task_version": context.task_version}, ensure_ascii=False, sort_keys=True)
    if context.task_id is None or session.task_signature != signature:
        payload = {"extra_data": request.extra_data}
        if context.role != "review_parent":
            payload.update(task=request.text, original_text=request.text, rewrite=rewrite)
        session.messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
        session.task_signature = signature
    names = set(context.tools.names)
    if context.allowed_tools is not None:
        names &= set(context.allowed_tools)
    state: AgentState = {"text": request.text, "current_plan": deepcopy(request.current_plan),
        "messages": deepcopy(session.messages), "rewrite": rewrite, "round_index": 0,
        "guard_retries": 0, "calls": [], "tool_events": [],
        "failed_calls": list(session.failed_calls), "evidence": list(session.evidence),
        "answer": "", "status": "failed", "done": False, "new_messages": [], "plan": None,
        "output": None, "active_tools": sorted(names)}
    record_event("agent_start", {"agent_id": context.agent_id or session.agent_id,
                 "parent_agent_id": context.parent_agent_id, "role": context.role,
                 "task_id": context.task_id, "task_version": context.task_version})
    try:
        async for snapshot in GRAPH.astream(state, {"recursion_limit": 10000}, context=context, stream_mode="values"):
            state.update(snapshot)
            session.messages = deepcopy(state["messages"])
        result = result_from_state(state)
        if context.on_text is not None:
            await context.on_text(result.answer)
        if context.memory_enabled and policy["read_long_term"] and result.status == "completed":
            try:
                result.memory = await memory.extract_and_store(context, request.text, result.answer,
                    (result.plan or request.current_plan or {}).get("plan_id"))
            except OSError as exc:
                result.memory = {"success": False, "message": str(exc)}
        record_event("agent_end", {"agent_id": context.agent_id or session.agent_id,
                     "parent_agent_id": context.parent_agent_id, "status": result.status,
                     "task_id": context.task_id, "task_version": context.task_version})
        return result
    except asyncio.CancelledError as exc:
        state["evidence"] = list(session.evidence)
        raise AgentCancelledError(result_from_state(state)) from exc
    except Exception as exc:
        state["evidence"] = list(session.evidence)
        state["answer"] = "执行中断，已保留验证成果。"
        state["status"] = "partial" if session.evidence else "failed"
        state["new_messages"] = [{"role": "assistant", "content": state["answer"]}]
        record_event("agent_end", {"agent_id": context.agent_id or session.agent_id,
                     "parent_agent_id": context.parent_agent_id, "task_id": context.task_id,
                     "task_version": context.task_version, "status": state["status"], "error_type": type(exc).__name__})
        return result_from_state(state)
