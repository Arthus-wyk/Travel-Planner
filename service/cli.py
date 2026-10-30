"""命令行只管理交互和连接，实际任务使用与 HTTP 相同的 run_turn。"""

import argparse
import asyncio
import sys
from pathlib import Path

from agent.context import AgentContext
from agent.runner import run_turn
from agent.state import AgentCancelledError, AgentRequest, AgentRunError
from infrastructure.mcp import connect_tools
from service.utils.api_telemetry import ApiTelemetryLogger
from service.utils.model_client import get_model, create_model_client
from service.utils.transcript_log import TranscriptLogger
from service.utils.usage_stats import format_usage_stats, load_usage_stats


async def run_agent(
    server_file: Path, model: str, *, session_id: str | None = None
) -> None:
    async with connect_tools(server_file) as registry:
        transcript = TranscriptLogger(Path.cwd(), session_id=session_id)
        telemetry = ApiTelemetryLogger(
            session_id=transcript.session_id, project_path=str(transcript.project_path)
        )
        context = AgentContext(
            create_model_client(),
            model,
            registry,
            streaming=True,
            transcript=transcript,
            telemetry=telemetry,
        )
        history = []
        current_plan = None
        print(
            f"Model: {model}\nAvailable tools: {', '.join(sorted(registry.names))}"
        )
        print(f"Session id: {transcript.session_id}\nTranscript: {transcript.path}")
        print("Type exit or quit to stop; /usage shows usage statistics.")
        while True:
            try:
                text = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                return
            if not text:
                continue
            if text.lower() in {"exit", "quit"}:
                print("Goodbye.")
                return
            if text.lower() == "/usage":
                print(format_usage_stats(load_usage_stats()))
                continue
            history.append({"role": "user", "content": text})
            # 请求历史不包含本轮输入；同一条原始消息只由图追加一次。
            request = AgentRequest(
                text, history=history[:-1], current_plan=current_plan
            )
            try:
                result = await run_turn(request, context)
            except (AgentRunError, AgentCancelledError) as exc:
                result = exc.partial_result
                if result.plan is not None:
                    current_plan = result.plan
                history.extend(result.new_messages)
                if isinstance(exc, AgentCancelledError):
                    raise
                print(f"Agent error: {exc}", file=sys.stderr)
                continue
            history.extend(result.new_messages)
            current_plan = result.plan or current_plan
            print(f"Agent: {result.answer}\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with the travel planning agent")
    parser.add_argument(
        "--server",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "mcp_server.py",
    )
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args()
    try:
        args.model = get_model()
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(
            run_agent(args.server.resolve(), args.model, session_id=args.session_id)
        )
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
