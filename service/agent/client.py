"""Interactive Ollama agent that uses tools from a local MCP server.

主流程是：连接 MCP、取得工具定义、调用模型、执行并校验工具结果，
再把结果回填上下文，直到模型给出最终答复。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from typing import Any, Awaitable, Callable
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from ollama import AsyncClient, ResponseError

from service.agent.compress import compress_messages
from service.agent.intent_understanding import understand_user_input
from service.agent.memory import extract_and_store, memory_context, memory_policy, read_mid_term
from service.agent.preference_profile import read_preference_profile
from service.agent.tool_verifier import verify_tool_result
from service.agent.tool_router import enrich_description, filter_ollama_tools, route_tool_names
from tooling.mcp import MCPTool, MCPToolRegistry, ToolContext
from service.utils.api_telemetry import ApiTelemetryLogger
from service.utils.transcript_log import TranscriptLogger
from service.utils.usage_stats import format_usage_stats, load_usage_stats
from service.utils.withRetry import request_model_with_retry, RetryConfig
from service.utils.ollama_client import DEFAULT_MODEL, create_ollama_client

SYSTEM_PROMPT = """
TRIP-DAY ITINERARY ANCHOR RULES:
- Do not model the flight or other long-distance arrival as an itinerary activity. The itinerary starts when the traveler arrives at the destination.
- By default, each destination day starts from the hotel and ends back at the hotel.
- The final destination day normally does not end back at the hotel; leave it ending at the final planned activity or the departure point when that is known.
- Use these hotel start/end anchors when arranging activities and calculating routes, but do not invent a hotel, airport, or departure time that the user has not provided.
- If the user explicitly gives a different start or end point, follow the user's instruction for that day.

你是一个旅行规划 Agent，可以通过 MCP 工具创建、读取和更新旅行计划。

核心目标：
- 把用户的模糊旅行需求转成可执行、节奏合理、预算可解释的行程。
- 优先维护当前行程；只有用户明确要求“新建/另开一个行程”时才创建新计划。
- 不重复索要上下文里已经存在的信息。

工作流程：
1. 先理解用户真实意图：新建行程、补全日程、修改安排、推荐地点、查路线、查预算或总结。
2. 判断是否真的缺少阻塞信息。只有目的地、行程对象、人数、预算、日期等信息完全无法从当前上下文或工具结果获得，且没有合理默认值时，才问一个简短澄清问题。
3. 只要当前信息足以行动，就立即调用工具。不要先说“我会处理”“请稍等”“接下来我将”。
4. 工具调用后必须阅读结果，再决定继续调用工具、修正参数或给最终回复。
5. 创建或修改行程后，用工具结果、摘要或审计结果支撑最终答复。

当前行程处理规则：
- 如果系统消息提供了 Current travel plan context，请把它当作权威的当前行程状态。
- 当前行程里已有 destination、travelers、budget、currency、interests、constraints、itinerary 等字段时，不要再次要求用户提供这些字段。
- 用户说“帮我规划一个 N 天 M 晚行程”“补一下行程”“安排三天怎么玩”时，默认是在补全当前行程的 day-by-day itinerary。
- 缺少具体日历日期不阻塞相对天数行程；使用 day=1、day=2 等相对天数即可。
- 如果当前行程已经有部分 itinerary，新增或修改时要尽量保留已有安排，避免无提示覆盖。

工具使用规则：
- 需要持久化计划、日程、推荐、预算时必须调用工具，不能只在文字里承诺完成。
- 不要无意义调用工具；简单解释、总结已有消息时可以直接回答。
- 只能使用系统提供的工具，不要把工具名和参数以 JSON 或 Markdown 代码块展示给用户。
- 工具报错时，根据错误修改参数后重试；不要原样重复同一个失败调用。
- 工具结果未确认 success=true 时，不要声称已经完成修改。

意图理解块规则：
- 用户消息可能包含 "User intent understanding" 和 "Task plan"。
- 把 "Rewritten" 当作解析后的用户请求，把 slots/domains/tool sequence 当作参考，不要机械服从明显错误的计划。
- 如果 Task plan 建议 create_travel_plan，但当前上下文已有 plan_id，且用户是在规划/补全当前行程，应改用读取和更新当前行程的工具。

行程质量规则：
- 安排要符合真实旅行节奏，避免一天塞太满。
- 明确说明关键假设，尤其是日期未知、预算估算、跨城市交通和预订风险。
- 推荐候选项时，尽量结合长期偏好画像；用户明确表达稳定偏好时再更新画像。
- 最终回复用用户语言，简洁说明已完成什么、行程亮点、预算/交通注意事项和下一步可调整项。
"""

MAX_TOOL_ROUNDS = 8
MAX_NO_TOOL_GUARD_RETRIES = 2


@dataclass(frozen=True)
class ToolInvocation:
    name: str
    arguments: dict[str, Any]

PROMISE_PHRASES = (
    "我将",
    "我会",
    "接下来",
    "现在，请稍等",
    "请稍等",
    "稍等片刻",
    "我将为您",
    "我会为您",
    "我将获取",
    "我会查询",
    "我会将",
    "我将添加",
    "我会添加",
    "开始考虑",
    "首先，我会",
    "然后，我会",
    "最后，我会",
    "I will",
    "I'll",
    "Let me",
    "please wait",
)

ACTION_VERBS = (
    "添加",
    "查询",
    "获取",
    "检查",
    "创建",
    "更新",
    "修改",
    "安排",
    "加入",
    "搜索",
    "估算",
    "验证",
    "add",
    "search",
    "check",
    "create",
    "update",
    "verify",
)



def mcp_tools_to_ollama(mcp_tools: list[Any]) -> list[dict[str, Any]]:
    """把 MCP 工具 schema 转成 Ollama function-calling 所需格式。"""
    """Convert MCP tool definitions into Ollama function-tool schemas."""
    converted = []
    for tool in mcp_tools:
        parameters = getattr(tool, "inputSchema", None)
        if parameters is None:
            parameters = getattr(tool, "input_schema", None)

        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": enrich_description(tool.name, tool.description),
                    "parameters": parameters
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return converted


def tool_result_to_text(result: Any) -> str:
    """优先序列化结构化结果，退回到文本内容供模型继续判断。"""
    """Serialize an MCP CallToolResult for the model."""
    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    texts = [
        item.text
        for item in getattr(result, "content", [])
        if hasattr(item, "text")
    ]
    if texts:
        return "\n".join(texts)
    return repr(result)


def build_no_tool_guard_message(
    assistant_content: str,
    messages: list[Any],
    tool_names: set[str],
) -> str | None:
    if not is_empty_execution_promise(assistant_content):
        return None

    suggested_tools = recent_task_plan_tools(messages, tool_names)
    tool_text = (
        ", ".join(suggested_tools)
        if suggested_tools
        else ", ".join(sorted(tool_names))
    )
    return (
        "The previous assistant message described future tool work but did not "
        "call any tool. Do not return another planning/status message. If the "
        "user request can be executed with available tools, immediately call "
        f"the appropriate tool(s). Suggested tools from the task plan: {tool_text}. "
        "If a required argument is truly missing, ask exactly one concise "
        "clarifying question instead of saying you will do it later."
    )


def is_empty_execution_promise(content: str) -> bool:
    normalized = content.strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    has_promise = any(phrase.lower() in lowered for phrase in PROMISE_PHRASES)
    has_action = any(verb.lower() in lowered for verb in ACTION_VERBS)
    return has_promise and has_action


def recent_task_plan_tools(
    messages: list[Any],
    tool_names: set[str],
) -> list[str]:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "")
        tools = parse_task_plan_tool_sequence(content)
        available = [tool for tool in tools if tool in tool_names]
        if available:
            return available
    return []


def parse_task_plan_tool_sequence(content: str) -> list[str]:
    prefix = "Tool sequence:"
    for line in content.splitlines():
        if not line.startswith(prefix):
            continue
        raw_value = line.removeprefix(prefix).strip()
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, str)]
    return []


def normalize_tool_calls(calls: list[Any]) -> list[ToolInvocation]:
    """兼容 SDK 对象和字典两种 tool-call 表示，并过滤非法参数。"""
    invocations = []
    for call in calls:
        function = getattr(call, "function", None)
        if function is None and isinstance(call, dict):
            function = call.get("function", call)

        if isinstance(function, dict):
            name = function.get("name")
            arguments = function.get("arguments", {})
        else:
            name = getattr(function, "name", None)
            arguments = getattr(function, "arguments", {})

        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if isinstance(name, str) and isinstance(arguments, dict):
            invocations.append(ToolInvocation(name=name, arguments=arguments))
    return invocations


def extract_embedded_tool_calls(
    content: str,
    tool_names: set[str],
) -> list[ToolInvocation]:
    """Promote fenced tool JSON emitted as prose into real tool invocations."""
    candidates = re.findall(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stripped = content.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)

    invocations = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        name = payload.get("name")
        arguments = payload.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if name not in tool_names or not isinstance(arguments, dict):
            continue

        key = (name, json.dumps(arguments, ensure_ascii=False, sort_keys=True))
        if key in seen:
            continue
        seen.add(key)
        invocations.append(ToolInvocation(name=name, arguments=arguments))
    return invocations


def tool_invocations_to_message(
    invocations: list[ToolInvocation],
) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": invocation.name,
                "arguments": invocation.arguments,
            },
        }
        for invocation in invocations
    ]


class QueryEngine:
    """执行一次完整的模型查询循环。

    这里的 ``query`` 是 agent 的核心运行时，而不是 REPL：它只处理
    ``messages -> 模型 -> tool call -> tool result -> 模型``，不负责读取
    用户输入，也不负责打印最终答案。这样同一个引擎可以被 REPL、HTTP
    API，未来的 AgentTool 以及 forked agent 复用。

    当前版本仍然把已有的循环实现放在 ``_query_impl`` 中，以便进行一次
    低风险的 API 抽取；后续可以继续把模型调用、工具执行和 transcript
    记录拆成独立组件，而不会再影响调用方。
    """

    async def query(
        self,
        ollama: AsyncClient,
        mcp: ClientSession,
        model: str,
        tools: list[dict[str, Any]],
        tool_names: set[str],
        messages: list[Any],
        *,
        fallback_model: str | None = None,
        use_streaming: bool = False,
        on_text: Callable[[str], Awaitable[None]] | None = None,
        auth_recovery: Callable[[], Awaitable[None]] | None = None,
        transcript_logger: TranscriptLogger | None = None,
        api_telemetry: ApiTelemetryLogger | None = None,
        tool_registry: MCPToolRegistry | None = None,
    ) -> str:
        """运行查询主循环，并原地更新 ``messages``。

        ``messages`` 是有意使用可变列表：调用者需要在查询结束后看到
        assistant/tool 消息，以便持久化、恢复上下文或生成 UI 中的事件。
        ``mcp`` 是当前工具执行后端，由 MCPToolRegistry 统一适配。
        """
        if tool_registry is None:
            # 兼容现有 REPL/API：它们目前传入的是 Ollama schema，而不是
            # MCP 原始 definition。这里临时完成适配，调用方以后可以直接
            # 传入自定义 registry。
            tool_registry = MCPToolRegistry.from_ollama_schemas(mcp, tools)
            for name in tool_names - tool_registry.names:
                tool_registry._tools[name] = MCPTool(
                    mcp,
                    type("ToolDefinition", (), {
                        "name": name,
                        "description": "",
                        "inputSchema": {"type": "object", "properties": {}},
                    })(),
                )
        return await _query_impl(
            ollama,
            mcp,
            model,
            tools,
            tool_names,
            messages,
            fallback_model=fallback_model,
            use_streaming=use_streaming,
            on_text=on_text,
            auth_recovery=auth_recovery,
            transcript_logger=transcript_logger,
            api_telemetry=api_telemetry,
            tool_registry=tool_registry,
        )


async def _query_impl(
    ollama: AsyncClient,
    mcp: ClientSession,
    model: str,
    tools: list[dict[str, Any]],
    tool_names: set[str],
    messages: list[Any],
    *,
    fallback_model: str | None = None,
    use_streaming: bool = False,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    auth_recovery: Callable[[], Awaitable[None]] | None = None,
    transcript_logger: TranscriptLogger | None = None,
    api_telemetry: ApiTelemetryLogger | None = None,
    tool_registry: MCPToolRegistry | None = None,
) -> str:
    """
    运行模型/工具循环，直到模型返回最终答复。

    每轮请求受重试、备用模型、流式降级和上下文压缩保护；
    空洞的“我将调用工具”文本会被护栏拦截，避免误报已完成。
    """

    retry_config = RetryConfig(
        max_attempts=6,
        base_delay_seconds=1.0,
        max_delay_seconds=30.0,
        jitter_seconds=0.5,
        fallback_after_529=2,
        max_context_compressions=1,
        max_auth_recoveries=1,
    )
    no_tool_guard_retries = 0

    for round_index in range(1, MAX_TOOL_ROUNDS + 1):
        print(f"\n[agent round] {round_index}")

        response, updated_messages, api_error = (
            await request_model_with_retry(
                ollama=ollama,
                model=model,
                fallback_model=fallback_model,
                messages=messages,
                tools=tools,
                config=retry_config,
                use_streaming=use_streaming,
                # Buffer each round. Only an accepted final answer is exposed.
                on_text=None,
                auth_recovery=auth_recovery,
                context_compressor=compress_messages,
                api_telemetry=api_telemetry,
            )
        )

        # Context might have been compressed during retry.
        messages.clear()
        messages.extend(updated_messages)

        if response is None:
            error_content = api_error or "Unknown API error."

            messages.append(
                {
                    "role": "assistant",
                    "content": error_content,
                }
            )

            if transcript_logger is not None:
                transcript_logger.record_message(
                    "assistant",
                    error_content,
                )

            return error_content

        assistant_message = response.message
        if transcript_logger is not None:
            # API 摘要写回第一本账，/usage 因此不需要读取第二本遥测日志。
            transcript_logger.record_api_summary(response.usage)

        native_calls = normalize_tool_calls(assistant_message.tool_calls or [])
        embedded_calls = (
            []
            if native_calls
            else extract_embedded_tool_calls(
                assistant_message.content or "",
                tool_names,
            )
        )
        calls = native_calls or embedded_calls
        message_tool_calls = assistant_message.tool_calls
        if embedded_calls:
            message_tool_calls = tool_invocations_to_message(embedded_calls)

        assistant_record = {
            "role": assistant_message.role,
            "content": assistant_message.content,
            "tool_calls": message_tool_calls,
            "_internal": True,
        }
        messages.append(assistant_record)

        if transcript_logger is not None:
            transcript_logger.record_message(
                assistant_message.role,
                assistant_message.content,
                tool_calls=message_tool_calls,
                usage=response.usage,
            )

        if not calls:
            guard_message = build_no_tool_guard_message(
                assistant_message.content or "",
                messages,
                tool_names,
            )
            if (
                guard_message is not None
                and no_tool_guard_retries < MAX_NO_TOOL_GUARD_RETRIES
            ):
                no_tool_guard_retries += 1
                print("[guard] blocked empty promise; requesting tool call")
                messages.append(
                    {
                        "role": "system",
                        "content": guard_message,
                        "_internal": True,
                    }
                )
                if transcript_logger is not None:
                    transcript_logger.record(
                        "no_tool_guard",
                        {
                            "round": round_index,
                            "assistant_content": assistant_message.content,
                            "guard_message": guard_message,
                        },
                    )
                continue

            if guard_message is not None:
                final_content = (
                    "执行失败：模型未发起有效的工具调用，因此没有修改行程。"
                    "请重试这项操作。"
                )
                assistant_record["content"] = final_content
            else:
                final_content = (
                    assistant_message.content
                    or "The model returned an empty response."
                )
            assistant_record["_internal"] = False
            if on_text is not None:
                await on_text(final_content)
            return final_content

        # 只要本轮确实产生了工具调用，之前的“空洞承诺”计数就不应继续累积。
        # 否则模型在一次成功调用后再出现两次普通说明，也会被错误终止。
        no_tool_guard_retries = 0

        # 工具调用前的说明也是有效的 assistant message。调用方如果需要实时展示
        # 执行进度，应收到它；空内容不发送，避免 UI 出现空白气泡。
        if not calls and on_text is not None and assistant_message.content:
            await on_text(assistant_message.content)

        for call in calls:
            name = call.name
            arguments = call.arguments

            print(
                f"[tool] {name}"
                f"({json.dumps(arguments, ensure_ascii=False)})"
            )

            if transcript_logger is not None:
                transcript_logger.record_tool_use(
                    name,
                    arguments,
                    round_index=round_index,
                )

            if name not in tool_names:
                result_text = json.dumps(
                    {
                        "status": "error",
                        "tool": name,
                        "error_type": "unknown_tool",
                        "message": f"Unknown MCP tool: {name}",
                    },
                    ensure_ascii=False,
                )

            else:
                # QueryEngine 不再直接调用 MCP。所有工具（当前是 MCP，
                # 未来也可以是 AgentTool/本地工具）都经过 registry。
                result_text = await (tool_registry or MCPToolRegistry()).execute(
                    name,
                    arguments,
                    context=ToolContext(),
                )

            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": result_text,
                }
            )

            verification = verify_tool_result(name, arguments, result_text)
            messages.append(
                {
                    "role": "tool",
                    "tool_name": "verify_last_tool_result",
                    "content": json.dumps(
                        {
                            "status": "success",
                            "tool": "verify_last_tool_result",
                            "verification": verification,
                            "verified_tool": name,
                        },
                        ensure_ascii=False,
                    ),
                }
            )

            if transcript_logger is not None:
                transcript_logger.record_tool_result(
                    name,
                    result_text,
                    round_index=round_index,
                )
                transcript_logger.record_tool_result(
                    "verify_last_tool_result",
                    json.dumps(verification, ensure_ascii=False),
                    round_index=round_index,
                )

    final_content = (
        "Stopped because the agent exceeded the maximum number "
        "of reasoning rounds."
    )
    if on_text is not None:
        await on_text(final_content)
    return final_content


async def answer_user(
    ollama: AsyncClient,
    mcp: ClientSession,
    model: str,
    tools: list[dict[str, Any]],
    tool_names: set[str],
    messages: list[Any],
    *,
    fallback_model: str | None = None,
    use_streaming: bool = False,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    auth_recovery: Callable[[], Awaitable[None]] | None = None,
    transcript_logger: TranscriptLogger | None = None,
    api_telemetry: ApiTelemetryLogger | None = None,
    tool_registry: MCPToolRegistry | None = None,
) -> str:
    """向后兼容的入口；新代码应直接使用 ``QueryEngine().query()``。

    保留这个函数是为了让现有 REPL 和 HTTP API 可以逐步迁移，而不是
    在抽取 QueryEngine 的同时修改所有上层调用方。
    """
    return await QueryEngine().query(
        ollama,
        mcp,
        model,
        tools,
        tool_names,
        messages,
        fallback_model=fallback_model,
        use_streaming=use_streaming,
        on_text=on_text,
        auth_recovery=auth_recovery,
        transcript_logger=transcript_logger,
        api_telemetry=api_telemetry,
        tool_registry=tool_registry,
    )


async def run_agent(
    server_file: Path,
    model: str,
    *,
    session_id: str | None = None,
) -> None:
    if not server_file.is_file():
        raise FileNotFoundError(
            f"MCP server not found: {server_file}\n"
            "Put agent_client.py beside server.py or pass --server PATH."
        )

    server = StdioServerParameters(
        command=sys.executable,
        args=[str(server_file)],
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as mcp:
            await mcp.initialize()

            listed = await mcp.list_tools()
            tools = mcp_tools_to_ollama(listed.tools)
            tool_names = {tool.name for tool in listed.tools}

            print(f"Connected to MCP server: {server_file.name}")
            print(f"Ollama model: {model}")
            print("Available tools: " + ", ".join(sorted(tool_names)))
            print("Type exit or quit to stop.\n")

            ollama = create_ollama_client()
            messages: list[Any] = [
                {"role": "system", "content": SYSTEM_PROMPT}
            ]
            transcript_logger = TranscriptLogger(
                Path.cwd(),
                session_id=session_id,
            )
            api_telemetry = ApiTelemetryLogger(
                session_id=transcript_logger.session_id,
                project_path=str(transcript_logger.project_path),
            )

            # 系统提示也属于会话上下文，启动时先写入流水。
            transcript_logger.record_message("system", SYSTEM_PROMPT)

            print(f"Session id: {transcript_logger.session_id}")
            print(f"Transcript: {transcript_logger.path}")

            while True:
                try:
                    user_input = input("You: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye.")
                    return

                if not user_input:
                    continue
                if user_input.lower() in {"exit", "quit"}:
                    print("Goodbye.")
                    return
                if user_input.lower() == "/usage":
                    print(format_usage_stats(load_usage_stats()))
                    print()
                    continue

                understanding = await understand_user_input(
                    user_input,
                    messages,
                    ollama=ollama,
                    model=model,
                    memory_context=memory_context(user_input, read_preference_profile().get("profile"), read_mid_term()),
                    memory_policy=memory_policy(user_input),
                )
                if understanding.changed:
                    print(f"[rewrite] {understanding.rewritten_text}")
                print(
                    "[intent] "
                    f"{understanding.semantic.primary_intent}"
                    f" / {', '.join(understanding.semantic.domains) or 'unknown'}"
                )

                # Domain-first routing: the model sees only tools relevant to
                # this request; the full registry remains available for safe
                # execution of a routed call and for subsequent turns.
                active_tool_names = route_tool_names(
                    understanding.semantic.domains,
                    tool_names,
                )
                active_tools = filter_ollama_tools(tools, active_tool_names)
                print(
                    "[tools] routed: " + ", ".join(sorted(active_tool_names))
                )

                messages.append(
                    {
                        "role": "user",
                        "content": understanding.format_for_model(),
                    }
                )
                transcript_logger.record_message("user", user_input)
                transcript_logger.record(
                    "intent_understanding",
                    understanding.to_dict(),
                )
                streamed_output_started = False

                async def print_agent_chunk(text: str) -> None:
                    nonlocal streamed_output_started
                    if not streamed_output_started:
                        print("Agent: ", end="", flush=True)
                        streamed_output_started = True
                    print(text, end="", flush=True)

                try:
                    answer = await answer_user(
                        ollama,
                        mcp,
                        model,
                        active_tools,
                        active_tool_names,
                        messages,
                        use_streaming=True,
                        on_text=print_agent_chunk,
                        transcript_logger=transcript_logger,
                        api_telemetry=api_telemetry,
                    )
                except ResponseError as exc:
                    print(f"Ollama error: {exc.error}", file=sys.stderr)
                    continue
                except ConnectionError:
                    print(
                        "Cannot connect to Ollama. Make sure Ollama is running.",
                        file=sys.stderr,
                    )
                    continue

                if streamed_output_started:
                    print("\n")
                else:
                    print(f"Agent: {answer}\n")
                if memory_policy(user_input)["read_long_term"]:
                    memory_result = await extract_and_store(
                        ollama,
                        model,
                        user_input,
                        answer,
                        plan_id=understanding.semantic.slots.get("plan_id"),
                    )
                    transcript_logger.record("memory_extraction", memory_result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Chat with an Ollama agent that can call MCP tools"
    )
    parser.add_argument(
        "--server",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "mcp_server.py",
        help="Path to the MCP server",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Ollama model name (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--session-id",
        default=None,
        help="Reuse a specific transcript session id instead of generating one.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(
            run_agent(
                args.server.resolve(),
                args.model,
                session_id=args.session_id,
            )
        )
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
