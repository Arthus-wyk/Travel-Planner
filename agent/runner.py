"""唯一 Agent 入口。调用者提供任务和资源，核心返回显式结果，不改写外部历史。"""

import asyncio
from copy import deepcopy
from service.utils.runtime_log import trace_run
from agent import memory
from agent.context import AgentContext
from agent.graph import build_graph
from agent.prompts import build_system_prompt
from agent.state import (
    AgentCancelledError,
    AgentRequest,
    AgentResult,
    AgentRunError,
    AgentState,
)

# 编译图不持有模型、MCP 或会话，多个调用共享结构，但不共享状态和 context。
GRAPH = build_graph()


def result_from_state(state: AgentState) -> AgentResult:
    return AgentResult(
        answer=state.get("answer", ""),
        status=state.get("status", "failed"),
        understanding=state.get("understanding", {}),
        new_messages=state.get("new_messages", []),
        tool_events=state.get("tool_events", []),
        plan=state.get("plan"),
    )


async def run_turn(request: AgentRequest, context: AgentContext) -> AgentResult:
    with trace_run(context.transcript, model=context.model, text=request.text) as run:
        result = await _run_turn(request, context)
        if run is not None:
            run.finish(result.status, result.answer)
        return result


async def _run_turn(request: AgentRequest, context: AgentContext) -> AgentResult:
    """未来协调者可注册异步工具，使用独立 scope/context 调用同一个函数。

    不替调用者创建或关闭连接，不在这里写业务数据库；因此 CLI、HTTP 和
    专业 Agent 都能复用，而不用继承某个包含输入输出循环的大类。
    """
    if not request.text.strip():
        raise ValueError("User input must not be empty")
    if context.max_rounds < 1:
        raise ValueError("max_rounds must be positive")
    state: AgentState = {
        "text": request.text,
        "current_plan": deepcopy(request.current_plan),
        "messages": [
            {"role": "system", "content": build_system_prompt(request.current_plan)},
            *deepcopy(request.history),
        ],
        "round_index": 0,
        "guard_retries": 0,
        "calls": [],
        "call_index": 0,
        "tool_events": [],
        "failed_calls": [],
        "answer": "",
        "status": "failed",
        "done": False,
        "new_messages": [],
        "plan": None,
    }
    try:
        # 图步数还包含逐工具执行和校验，不能误用 8 轮模型限制。
        # shortcut: 超过 10000 图步的大批量任务需拆分请求。
        async for snapshot in GRAPH.astream(
            state, {"recursion_limit": 10000}, context=context, stream_mode="values"
        ):
            state.update(snapshot)
        result = result_from_state(state)
        if context.on_text is not None:
            await context.on_text(result.answer)
        if (
            context.memory_enabled
            and state["memory_policy"]["read_long_term"]
            and result.status == "completed"
        ):
            plan_id = (result.plan or request.current_plan or {}).get("plan_id")
            try:
                result.memory = await memory.extract_and_store(
                    context, request.text, result.answer, plan_id
                )
            except OSError as exc:
                # 记忆文件失败不抹掉本轮已完成的旅行操作。
                result.memory = {"success": False, "message": str(exc)}
            if context.transcript is not None:
                context.transcript.record("memory_extraction", result.memory)
        return result
    except asyncio.CancelledError as exc:
        raise AgentCancelledError(result_from_state(state)) from exc
    except Exception as exc:
        raise AgentRunError(str(exc), result_from_state(state)) from exc
