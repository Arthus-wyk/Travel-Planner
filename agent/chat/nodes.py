"""Role-neutral native tool loop with permission and domain verification boundaries."""
import asyncio
import json
from copy import deepcopy

from jsonschema import ValidationError, validate
from langgraph.runtime import Runtime
from agent.chat.compress import compress_messages
from agent.chat.context import AgentContext
from agent.chat.executor import ToolExecutor
from agent.chat.prompts import is_empty_execution_promise, public_answer
from agent.chat.review_execution import ReviewExecution, ReviewStepTimeout
from agent.chat.state import AgentState
from agent.chat.tool_calls import model_tools, normalize_calls
from infrastructure import llm
from service.travel.tool_verification import verify_tool_result
from service.utils.runtime_log import record_event
from tooling.results import ToolResult


def execution(context):
    return context.execution or ReviewExecution(timeout_seconds=context.timeout_seconds)


async def call_model(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    session = context.session
    if session.model_steps >= context.max_rounds + 2:
        return {"done": True, "status": "limit_reached", "answer": "模型步骤上限已到，已保留验证成果。"}
    session.model_steps += 1
    schemas = model_tools(context.tools, state["active_tools"])
    can_repair = any(0 < count <= 3 for count in session.budget.failures.values())
    if (session.model_steps > context.max_rounds
            or (session.budget.remaining <= 0 and not can_repair)):
        schemas = []
    elif session.budget.remaining <= 0:
        retry_names = {failure["name"] for failure in session.failed_calls
                       if 0 < session.budget.failures.get(failure["operation_id"], 0) <= 3}
        schemas = [schema for schema in schemas if schema["function"]["name"] in retry_names]
    request_messages = deepcopy([message for message in state["messages"]
                                 if message.get("name") != "agent_runtime"])
    request_messages.append({"role": "system", "name": "agent_runtime", "content": json.dumps({"runtime": {
        **session.budget.feedback(), "model_steps": session.model_steps,
        "finalize_only": not schemas, "failed_operations": [
            {key: failure[key] for key in ("operation_id", "name", "error_type")}
            for failure in session.failed_calls],
        "instruction": "纠错使用_retry_of引用失败operation_id，调整参数，首次失败后最多3次免费纠错。没有工具额度时交付已验证成果和剩余事项。",
    }}, ensure_ascii=False)})
    response, messages, error = await execution(context).run(llm.request_model_with_retry(
        ollama=context.client, model=context.model, messages=request_messages,
        tools=schemas, config=llm.RetryConfig(max_attempts=6),
        fallback_model=context.fallback_model, use_streaming=context.streaming,
        on_text=None, auth_recovery=context.auth_recovery, context_compressor=compress_messages,
        api_telemetry=context.telemetry, response_format=context.response_format, stage=context.role,
    ), "调用模型")
    update = {"round_index": session.model_steps, "messages": list(messages), "calls": []}
    if response is None:
        return {**update, "done": True, "status": "partial" if session.evidence else "failed",
                "answer": error or "模型调用失败。"}
    message = response.message
    raw_calls = message.tool_calls or []
    calls = normalize_calls(raw_calls)
    if len(calls) != len(raw_calls):
        return {**update, "done": True, "status": "failed", "answer": "工具调用未完整解析，整批未执行。"}
    record = {"role": "assistant", "content": message.content or ""}
    if calls:
        if not schemas and (session.model_steps > context.max_rounds or session.budget.remaining <= 0):
            return {**update, "done": True, "status": "limit_reached", "answer": "模型步骤上限已到，已保留验证成果。"}
        records = []
        for index, call in enumerate(calls):
            raw = raw_calls[index]
            call_id = raw.get("id") if isinstance(raw, dict) else getattr(raw, "id", None)
            call["id"] = call_id or f"call-{session.model_steps}-{index}"
            records.append({"id": call["id"], "type": "function", "function": {
                "name": call["name"], "arguments": {**call["arguments"],
                **({"_retry_of": call["retry_of"]} if "retry_of" in call else {})}}})
        record["tool_calls"] = records
    update["messages"].append(record)
    if context.transcript is not None:
        context.transcript.record_api_summary(getattr(response, "usage", {}))
        context.transcript.record_message("assistant", message.content, tool_calls=record.get("tool_calls", []))
    if calls:
        return {**update, "calls": calls, "guard_retries": 0}
    if not (message.content or "").strip():
        return {**update, "done": True, "status": "failed", "answer": "模型返回了空响应。"}
    output = None
    if context.response_format:
        try:
            output = json.loads(message.content)
            validate(output, context.response_format)
        except (ValueError, TypeError, ValidationError) as exc:
            return {**update, "done": True, "status": "failed", "answer": f"结构化结果校验失败：{type(exc).__name__}"}
    elif is_empty_execution_promise(message.content):
        if state.get("guard_retries", 0) < 2:
            update["messages"].append({"role": "system", "content": "请调用工具执行，或明确说明缺少什么信息；不要只承诺以后执行。"})
            return {**update, "guard_retries": state.get("guard_retries", 0) + 1}
        return {**update, "done": True, "status": "failed", "answer": "模型没有发起有效工具调用。"}
    return {**update, "done": True, "status": "failed" if session.failed_calls and output is None else "completed",
            "answer": message.content, "output": output}


async def execute_tool(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    session = context.session
    semaphore = asyncio.Semaphore(max(1, context.concurrency))
    pending = []
    reserved = set()
    # Reserve all budgets before dispatch so concurrent handlers cannot overspend.
    for call in state["calls"]:
        retry_of = call.get("retry_of")
        previous = next((failure for failure in session.failed_calls
                         if failure["operation_id"] == retry_of and failure["name"] == call["name"]), None)
        if retry_of is not None and previous is None:
            pending.append((call, None, ToolResult(call["name"], False,
                message="_retry_of must reference a failed operation of the same tool", error_type="invalid_retry")))
            continue
        if previous is not None:
            operation_id = retry_of
        else:
            session.operation_sequence += 1
            operation_id = f"op-{session.operation_sequence}"
        if operation_id in reserved or (previous is not None and session.budget.failures.get(operation_id, 0) > 3):
            pending.append((call, operation_id, ToolResult(call["name"], False,
                message="Failure correction is exhausted or already scheduled", error_type="repair_limit")))
            continue
        allowed, reason = session.budget.begin(operation_id, call["arguments"])
        reserved.add(operation_id)
        rejected = None if allowed else ToolResult(call["name"], False, message=reason, error_type="budget_rejected")
        pending.append((call, operation_id, rejected))

    async def execute_and_record(entry):
        call, operation_id, rejected = entry
        result = rejected
        if result is None:
            async with semaphore:
                try:
                    operation = ToolExecutor(context.tools).execute(
                        call["name"], call["arguments"], context.tool_context,
                        allowed=set(state["active_tools"]))
                    # Delegation spans multiple independently timed child steps.
                    result = (await operation if call["name"] == "delegate_task"
                              else await execution(context).run(operation, f"执行{call['name']}"))
                except asyncio.CancelledError:
                    session.messages.append({"role": "tool", "tool_name": call["name"],
                        "tool_call_id": call["id"], "content": json.dumps({"success": False,
                        "operation_id": operation_id, "error_type": "cancelled"})})
                    raise
                except ReviewStepTimeout as exc:
                    result = ToolResult(call["name"], False, message=str(exc), error_type="timeout")
        data = result.data if result.success else {"success": False, "message": result.message}
        if not isinstance(data, (str, dict, list)):
            data = {"success": result.success, "value": data}
        verification = verify_tool_result(result.tool, call["arguments"], data)
        success = result.success and verification["status"] != "failed"
        if operation_id is not None and rejected is None:
            session.budget.finish(operation_id, success)
            session.failed_calls[:] = [f for f in session.failed_calls if f["operation_id"] != operation_id]
            if not success:
                session.failed_calls.append({"operation_id": operation_id, "name": call["name"],
                    "arguments": deepcopy(call["arguments"]), "error_type": result.error_type})
        event = {"tool": result.tool, "arguments": deepcopy(call["arguments"]),
                 "operation_id": operation_id, "status": "success" if success else "error",
                 "result": result.data, "message": result.message or None, "verification": verification}
        if success:
            session.evidence.append(event)
        # Persist immediately: sibling cancellation must not erase completed side effects.
        state["tool_events"].append(event)
        state["evidence"] = list(session.evidence)
        if success and isinstance(result.data, dict) and isinstance(result.data.get("plan"), dict):
            state["plan"] = result.data["plan"]
        payload = {**result.to_dict(), "operation_id": operation_id, "verification": verification,
                   "remaining": session.budget.feedback()}
        record_event("tool_result", {**event, "agent_id": context.agent_id or session.agent_id,
                     "parent_agent_id": context.parent_agent_id,
                     "task_id": context.task_id, "task_version": context.task_version})
        message = {"role": "tool", "tool_name": result.tool, "tool_call_id": call["id"],
                   "content": json.dumps(payload, ensure_ascii=False)}
        session.messages.append(message)
        return message

    if all(entry[0]["name"] == "delegate_task" for entry in pending):
        results = await asyncio.gather(*(execute_and_record(entry) for entry in pending))
    else:
        results = []
        blocked = False
        for call, operation_id, rejected in pending:
            if blocked:
                rejected = ToolResult(call["name"], False, message="Previous operation failed; ask the model for the next decision",
                                      error_type="decision_required")
                # Normal allowance counts model-generated calls, including skipped batch siblings.
            message = await execute_and_record((call, operation_id, rejected))
            results.append(message)
            payload = json.loads(message["content"])
            blocked = blocked or not payload["success"] or payload["verification"]["status"] == "failed"
    return {"messages": [*state["messages"], *results], "tool_events": list(state["tool_events"]),
            "failed_calls": list(session.failed_calls), "evidence": list(session.evidence), "plan": state.get("plan")}


async def finish(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    answer = state["answer"]
    if runtime.context.role == "chat" and state["status"] == "completed" and state.get("output") is None:
        answer = public_answer(answer, state.get("plan"))
    return {"answer": answer, "new_messages": [{"role": "assistant", "content": answer}]}
