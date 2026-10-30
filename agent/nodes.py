"""图节点的实际工作；每个函数只处理一个阶段，不隐藏第二套执行循环。"""

import json
from langgraph.runtime import Runtime

from agent import memory
from agent.compress import compress_messages
from agent.context import AgentContext
from agent.executor import ToolExecutor
from agent.intent import understand
from agent.prompts import is_empty_execution_promise, public_answer
from agent.router import select_tools
from agent.state import AgentState
from agent.tool_calls import embedded_calls, message_calls, model_tools, normalize_calls
from infrastructure import llm
from service.travel.tool_verification import verify_tool_result
from tooling.results import ToolResult


async def prepare(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    policy, recall_context = memory.recall(state["text"], context.memory_enabled)
    if context.transcript is not None:
        context.transcript.record_message("user", state["text"])
    return {"memory_policy": policy, "memory_context": recall_context}


async def understand_request(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    understanding = understand(state["text"], state["current_plan"], state["messages"])
    understanding.update(
        memory_policy=state["memory_policy"], memory_context=state["memory_context"]
    )
    if runtime.context.transcript is not None:
        runtime.context.transcript.record("intent_understanding", understanding)
    content = "[User intent understanding]\n" + json.dumps(
        understanding, ensure_ascii=False
    )
    return {
        "understanding": understanding,
        "messages": [*state["messages"], {"role": "user", "content": content}],
    }


async def route(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    names = select_tools(
        context.tools,
        state["understanding"]["semantic"]["domains"],
        context.allowed_tools,
    )
    return {"active_tools": sorted(names)}


async def call_model(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    if state["round_index"] >= context.max_rounds + (3 if state.get("repair_activated") else 0):
        return {
            "done": True,
            "status": "limit_reached",
            "answer": "Stopped because the agent exceeded the maximum number of reasoning rounds.",
        }
    round_index = state["round_index"] + 1
    print(f"[agent round] {round_index}")
    schemas = model_tools(context.tools, state["active_tools"])
    response, messages, error = await llm.request_model_with_retry(
        ollama=context.client,
        model=context.model,
        messages=state["messages"],
        tools=schemas,
        config=llm.RetryConfig(
            max_attempts=6,
            base_delay_seconds=1,
            max_delay_seconds=30,
            fallback_after_529=2,
            max_context_compressions=1,
            max_auth_recoveries=1,
        ),
        fallback_model=context.fallback_model,
        use_streaming=context.streaming,
        on_text=None,
        auth_recovery=context.auth_recovery,
        context_compressor=compress_messages,
        api_telemetry=context.telemetry,
    )
    update = {
        "round_index": round_index,
        "messages": list(messages),
        "calls": [],
        "call_index": 0,
    }
    if response is None:
        return {
            **update,
            "done": True,
            "status": "failed",
            "answer": error or "Unknown API error.",
        }
    message = response.message
    calls = normalize_calls(message.tool_calls or []) or embedded_calls(
        message.content or "", set(state["active_tools"])
    )
    record = {
        "role": "assistant",
        "content": message.content or "",
        "tool_calls": message_calls(calls),
    }
    update["messages"].append(record)
    if context.transcript is not None:
        context.transcript.record_api_summary(response.usage)
        context.transcript.record_message(
            "assistant",
            message.content,
            tool_calls=record["tool_calls"],
            usage=response.usage,
        )
    if calls:
        return {**update, "calls": calls, "guard_retries": 0}
    repairable = next(
        (failure for failure in state["failed_calls"]
         if failure.get("error_type") == "validation_error"
         and failure.get("attempts", 1) < 3
         and failure.get("nudges", 0) < 2),
        None,
    )
    if repairable is not None:
        failed = [
            {**failure, "nudges": failure.get("nudges", 0) + 1}
            if failure is repairable else failure
            for failure in state["failed_calls"]
        ]
        update["messages"].append({
            "role": "system",
            "content": f"The {repairable['name']} call was rejected before execution. "
            f"Correct its arguments and call it again with _retry_of="
            f"{repairable['operation_id']}. Use the tool schema. "
            "Do not tell the user the task failed while this correction is available.",
        })
        return {**update, "failed_calls": failed}
    if is_empty_execution_promise(message.content or ""):
        if state["guard_retries"] < 2:
            update["messages"].append(
                {
                    "role": "system",
                    "content": "The previous assistant message described future tool work but did not call any tool. "
                    "Call an available tool now, or ask one concise question if required information is missing.",
                }
            )
            if context.transcript is not None:
                context.transcript.record(
                    "no_tool_guard",
                    {"round": round_index, "assistant_content": message.content},
                )
            return {**update, "guard_retries": state["guard_retries"] + 1}
        return {
            **update,
            "done": True,
            "status": "failed",
            "answer": "执行失败：模型未发起有效的工具调用，因此没有修改行程。请重试这项操作。",
        }
    if not (message.content or "").strip():
        return {
            **update,
            "done": True,
            "status": "failed",
            "answer": "The model returned an empty response.",
        }
    answer = message.content
    if state["failed_calls"]:
        return {
            **update,
            "done": True,
            "status": "failed",
            "answer": "部分操作未完成。" + answer,
        }
    return {**update, "done": True, "status": "completed", "answer": answer}


async def execute_tool(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    context = runtime.context
    call = state["calls"][state["call_index"]]
    if context.transcript is not None:
        context.transcript.record_tool_use(
            call["name"], call["arguments"], round_index=state["round_index"]
        )
    # 唯一执行入口，不直接调用 MCP；写入失败不自动重试，交给模型判断与修正。
    retry_of = call.get("retry_of")
    if retry_of is None:
        candidates = [failure for failure in state["failed_calls"]
                      if failure["name"] == call["name"]
                      and failure.get("error_type") == "validation_error"]
        if len(candidates) == 1:
            retry_of = candidates[0]["operation_id"]
    valid_retry = any(
        f["operation_id"] == retry_of and f["name"] == call["name"]
        for f in state["failed_calls"]
    )
    previous = next((f for f in state["failed_calls"] if f["operation_id"] == retry_of), None)
    if valid_retry and previous.get("attempts", 1) >= 3:
        result = ToolResult(call["name"], False,
                            message="Parameter correction limit reached.",
                            error_type="repair_limit")
    elif call.get("retry_of") is not None and not valid_retry:
        result = ToolResult(
            call["name"],
            False,
            message="_retry_of must reference a failed call of the same tool",
            error_type="validation_error",
        )
    else:
        result = await ToolExecutor(context.tools).execute(
            call["name"],
            call["arguments"],
            context.tool_context,
            allowed=set(state["active_tools"]),
        )
    payload = result.to_dict()
    operation_id = (
        retry_of if valid_retry else f"r{state['round_index']}c{state['call_index']}"
    )
    model_payload = {**payload, "operation_id": operation_id}
    if context.transcript is not None:
        context.transcript.record_tool_result(
            call["name"],
            json.dumps(model_payload, ensure_ascii=False),
            round_index=state["round_index"],
        )
    event = {
        "tool": result.tool,
        "operation_id": operation_id,
        "status": "success" if result.success else "error",
        "result": result.data,
        "message": result.message or None,
        "verification": None,
    }
    return {
        "tool_result": payload,
        "tool_retry_of": retry_of if valid_retry else None,
        "tool_events": [*state["tool_events"], event],
        "messages": [
            *state["messages"],
            {
                "role": "tool",
                "tool_name": result.tool,
                "content": json.dumps(model_payload, ensure_ascii=False),
            },
        ],
    }


async def verify_tool(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    result = ToolResult(**state["tool_result"])
    call = state["calls"][state["call_index"]]
    # 通用传输层已经解析完毕；领域校验直接接收字典，不再反复解包 JSON。
    data = (
        result.data if result.success else {"success": False, "message": result.message}
    )
    verification = verify_tool_result(result.tool, call["arguments"], data)
    failed = list(state["failed_calls"])
    retry_of = state.get("tool_retry_of")
    valid_retry = any(
        f["operation_id"] == retry_of and f["name"] == call["name"] for f in failed
    )
    operation_id = (
        retry_of if valid_retry else f"r{state['round_index']}c{state['call_index']}"
    )
    previous = next((f for f in failed if f["operation_id"] == operation_id), None)
    if result.success and verification["status"] != "failed":
        # 只解除明确关联的重试；相同参数也可能表示用户要执行两次独立操作。
        match = next(
            (
                f
                for f in failed
                if f["name"] == result.tool and (f["operation_id"] == retry_of)
            ),
            None,
        )
        if match is not None:
            failed.remove(match)
    else:
        failed = [f for f in failed if f["operation_id"] != operation_id]
        failed.append(
            {
                "operation_id": operation_id,
                "name": result.tool,
                "arguments": call["arguments"],
                "error_type": result.error_type,
                "attempts": (previous.get("attempts", 1) + 1 if previous else 1),
                "nudges": previous.get("nudges", 0) if previous else 0,
            }
        )
    event = {
        "tool": "verify_last_tool_result",
        "status": "success",
        "result": None,
        "message": None,
        "verification": verification,
    }
    message = {
        "role": "tool",
        "tool_name": "verify_last_tool_result",
        "content": json.dumps(
            {"verification": verification, "verified_tool": result.tool},
            ensure_ascii=False,
        ),
    }
    if runtime.context.transcript is not None:
        runtime.context.transcript.record_tool_result(
            "verify_last_tool_result",
            json.dumps(verification, ensure_ascii=False),
            round_index=state["round_index"],
        )
    update = {
        "call_index": state["call_index"] + 1,
        "failed_calls": failed,
        "repair_activated": state.get("repair_activated", False) or result.error_type == "validation_error",
        "tool_events": [*state["tool_events"], event],
        "messages": [*state["messages"], message],
    }
    if (
        result.success
        and isinstance(result.data, dict)
        and isinstance(result.data.get("plan"), dict)
    ):
        update["plan"] = result.data["plan"]
    return update


async def finish(state: AgentState, runtime: Runtime[AgentContext]) -> dict:
    # 公开消息显式产生；内部思考、工具调用和护栏不会混入可持久化的列表。
    answer = (
        public_answer(state["answer"], state["plan"])
        if state["status"] == "completed"
        else state["answer"]
    )
    return {
        "answer": answer,
        "new_messages": [{"role": "assistant", "content": answer}],
    }
