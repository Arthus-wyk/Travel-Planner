"""Role-neutral native tool loop with permission and domain verification boundaries."""
import asyncio
import json
from copy import deepcopy

from langgraph.runtime import Runtime
from agent.chat.runtime.compress import compress_messages
from agent.chat.runtime.context import AgentContext
from agent.tools.dispatcher import ToolExecutor, arguments_with_defaults
from agent.chat.prompts import TOOL_ERROR_HANDLING_RULES, is_empty_execution_promise, public_answer
from agent.chat.runtime.execution import ReviewExecution, ReviewStepTimeout
from agent.chat.schemas.state import AgentState
from agent.tools.calls import model_tools, normalize_calls
from infrastructure import llm
from service.travel.tool_verification import verify_tool_result
from service.utils.model_client import StructuredOutputError, decode_structured_output
from service.utils.runtime_log import record_event
from agent.tools.schemas.errors import ErrorCode
from agent.tools.schemas.outputs import failure, legacy_result


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
        "instruction": TOOL_ERROR_HANDLING_RULES + "每次初始失败最多3次模型纠错。没有工具额度时交付已验证成果和剩余事项。",
    }}, ensure_ascii=False)})
    token_options = ({"max_output_tokens": context.max_output_tokens}
                     if context.max_output_tokens is not None else {})
    try:
        response, messages, error = await execution(context).run(llm.request_model_with_retry(
            ollama=context.client, model=context.model, messages=request_messages,
            tools=schemas, config=llm.RetryConfig(max_attempts=6),
            fallback_model=context.fallback_model, use_streaming=context.streaming,
            on_text=None, auth_recovery=context.auth_recovery, context_compressor=compress_messages,
            api_telemetry=context.telemetry, response_format=context.response_format, stage=context.role,
            **token_options,
        ), "调用模型")
    except StructuredOutputError as exc:
        return {"done": True, "status": "failed", "answer": str(exc), "output_error": exc.code}
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
    output = None
    if context.response_format:
        try:
            output, repaired = decode_structured_output(message.content, context.response_format)
            if repaired:
                record["content"] = json.dumps(output, ensure_ascii=False)
                record_event("structured_output_repaired", {"stage": context.role, "model": context.model})
        except StructuredOutputError as exc:
            return {**update, "done": True, "status": "failed", "answer": str(exc), "output_error": exc.code}
    elif not (message.content or "").strip():
        return {**update, "done": True, "status": "failed", "answer": "模型返回了空响应。"}
    elif is_empty_execution_promise(message.content):
        if state.get("guard_retries", 0) < 2:
            update["messages"].append({"role": "system", "content": "请调用工具执行，或明确说明缺少什么信息；不要只承诺以后执行。"})
            return {**update, "guard_retries": state.get("guard_retries", 0) + 1}
        return {**update, "done": True, "status": "failed", "answer": "模型没有发起有效工具调用。"}
    return {**update, "done": True, "status": "failed" if session.failed_calls and output is None else "completed",
            "answer": record["content"], "output": output}


async def execute_tool(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    session = context.session
    semaphore = asyncio.Semaphore(max(1, context.concurrency))
    pending = []
    reserved = set()
    # Reserve all budgets before dispatch so concurrent handlers cannot overspend.
    for call in state["calls"]:
        retry_of = call.get("retry_of")
        spec = context.tools.get(call["name"]) if call["name"] in context.tools.names else None
        arguments = call["arguments"]
        effective_args = arguments_with_defaults(spec, arguments) if spec else arguments
        previous = next((failure for failure in session.failed_calls
                         if failure["operation_id"] == retry_of and failure["name"] == call["name"]), None)
        terminal = next((item for item in session.failed_calls if item["name"] == call["name"] and (
            item.get("code") == ErrorCode.PERMISSION_DENIED.value
            or item.get("recovery") == "REPLAY_UNSAFE"
            or ((arguments_with_defaults(spec, item["arguments"]) if spec else item["arguments"]) == effective_args
                and item.get("recovery") in {
                    "RETRY_EXHAUSTED", "DEADLINE_EXCEEDED",
                }
            )
        )), None)
        if terminal is not None:
            code = ErrorCode(terminal["code"])
            pending.append((call, None, failure(call["name"], code, "This call cannot be repeated in the current turn.",
                                                details={"recovery": terminal.get("recovery")})))
            continue
        if retry_of is not None and previous is None:
            pending.append((call, None, legacy_result(call["name"], False,
                message="_retry_of must reference a failed operation of the same tool", error_type="invalid_retry")))
            continue
        if previous is not None:
            operation_id = retry_of
        else:
            session.operation_sequence += 1
            operation_id = f"op-{session.operation_sequence}"
        if operation_id in reserved or (previous is not None and session.budget.failures.get(operation_id, 0) > 3):
            pending.append((call, operation_id, legacy_result(call["name"], False,
                message="Failure correction is exhausted or already scheduled", error_type="repair_limit")))
            continue
        allowed, reason = session.budget.begin(operation_id, call["arguments"])
        reserved.add(operation_id)
        rejected = None if allowed else legacy_result(call["name"], False, message=reason, error_type="budget_rejected")
        pending.append((call, operation_id, rejected))

    async def execute_and_record(entry):
        call, operation_id, rejected = entry
        result = rejected
        if result is None:
            async with semaphore:
                try:
                    run = execution(context)
                    spec = context.tools.get(call["name"]) if call["name"] in context.tools.names else None
                    is_delegation = call["name"] == "delegate_task"
                    operation = ToolExecutor(context.tools).execute(
                        call["name"], call["arguments"], context.tool_context,
                        allowed=set(state["active_tools"]), timeout_seconds=None if is_delegation else run.timeout_seconds)
                    # Delegation has independently timed child steps.
                    if is_delegation or (spec and spec.via_mcp):
                        if not is_delegation and run.on_progress:
                            run.on_progress(f"正在执行{call['name']}…")
                        result = await operation
                    else:
                        result = await run.run(operation, f"执行{call['name']}")
                except asyncio.CancelledError:
                    cancelled = failure(call["name"], ErrorCode.INTERNAL_ERROR, "Tool call was cancelled.",
                                        details={"reason": "cancelled"})
                    session.messages.append({"role": "tool", "tool_name": call["name"],
                        "tool_call_id": call["id"], "content": json.dumps({**cancelled.to_dict(),
                        "operation_id": operation_id})})
                    raise
                except ReviewStepTimeout:
                    result = failure(call["name"], ErrorCode.UPSTREAM_TIMEOUT, "Tool call timed out.")
        if result.status == "error":
            verification = {"status": "failed", "message": result.message, "retryable": result.error.retryable}
        else:
            verification = verify_tool_result(call["name"], call["arguments"], result.data)
            if verification["status"] == "failed":
                result = failure(call["name"], ErrorCode.INTERNAL_ERROR, "Tool output failed domain verification.")
            elif result.status == "partial":
                verification = {**verification, "status": "warning", "message": result.message}
        usable = result.status != "error"
        if operation_id is not None and rejected is None:
            session.budget.finish(operation_id, usable)
            session.failed_calls[:] = [f for f in session.failed_calls if f["operation_id"] != operation_id]
            if not usable:
                session.failed_calls.append({"operation_id": operation_id, "name": call["name"],
                    "arguments": deepcopy(call["arguments"]), "error_type": result.error_type,
                    "code": result.error.code.value, "recovery": result.error.details.get("recovery")})
        event = {"tool": call["name"], "arguments": deepcopy(call["arguments"]),
                 "operation_id": operation_id, "status": result.status,
                 "result": result.data, "message": result.message or None, "verification": verification,
                 **({"error": result.error.model_dump(mode="json")} if result.error else {})}
        if usable:
            session.evidence.append(event)
        # Persist immediately: sibling cancellation must not erase completed side effects.
        state["tool_events"].append(event)
        state["evidence"] = list(session.evidence)
        if usable and isinstance(result.data, dict) and isinstance(result.data.get("plan"), dict):
            state["plan"] = result.data["plan"]
        payload = {**result.to_dict(), "operation_id": operation_id, "remaining": session.budget.feedback()}
        if call["name"] != "delegate_task":
            payload["verification"] = {key: value for key, value in verification.items()
                                       if key != "message" or result.error is None}
        record_event("tool_result", {**event, "agent_id": context.agent_id or session.agent_id,
                     "parent_agent_id": context.parent_agent_id,
                     "task_id": context.task_id, "task_version": context.task_version})
        message = {"role": "tool", "tool_name": call["name"], "tool_call_id": call["id"],
                   "content": json.dumps(payload, ensure_ascii=False)}
        session.messages.append(message)
        return message, usable

    if all(entry[0]["name"] == "delegate_task" for entry in pending):
        completed = await asyncio.gather(*(execute_and_record(entry) for entry in pending))
        results = [message for message, _ in completed]
    else:
        results = []
        blocked = False
        for call, operation_id, rejected in pending:
            if blocked:
                rejected = legacy_result(call["name"], False, message="Previous operation failed; ask the model for the next decision",
                                      error_type="decision_required")
                # Normal allowance counts model-generated calls, including skipped batch siblings.
            message, success = await execute_and_record((call, operation_id, rejected))
            results.append(message)
            blocked = blocked or not success
    return {"messages": [*state["messages"], *results], "tool_events": list(state["tool_events"]),
            "failed_calls": list(session.failed_calls), "evidence": list(session.evidence), "plan": state.get("plan")}


async def finish(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    answer = state["answer"]
    if runtime.context.role == "chat" and state["status"] == "completed" and state.get("output") is None:
        answer = public_answer(answer, state.get("plan"))
    return {"answer": answer, "new_messages": [{"role": "assistant", "content": answer}]}
