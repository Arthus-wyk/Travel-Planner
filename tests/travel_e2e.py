"""Run the browser planning flow against Uvicorn and a disposable MySQL schema."""

import asyncio
import json
import os
import socket
import subprocess
from contextlib import ExitStack
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import uvicorn
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / ".uv-cache" / "travel-e2e-dist"
PLACES = {
    name: {"place_id": f"e2e-{index}", "name": name, "address": f"杭州市{name}",
           "longitude": 120.13 + index * 0.002, "latitude": 30.25, "types": [kind]}
    for index, (name, kind) in enumerate(
        [("西湖", "风景名胜"), ("灵隐寺", "风景名胜"), ("湖畔酒店", "住宿服务")]
    )
}


def guide_result(text: str) -> dict:
    places = [{"id": point["place_id"], "name": name,
               "category": "hotel" if name == "湖畔酒店" else "attraction"}
              for name, point in PLACES.items() if name in text]
    return {"text": text, "places": places, "constraints": []}


def map_search(query: str, **kwargs) -> dict:
    return {"success": True, "places": [point for name, point in PLACES.items() if name in query]}


def map_route(*args, travel_mode: str, **kwargs) -> dict:
    duration = "600s" if travel_mode == "TRANSIT" else "900s"
    return {"success": True, "duration": duration, "distance_meters": 800, "fare": 12}


async def review_model(**request):
    """Script the model only. The delegates, MCP transport, and save are real."""
    messages = request["messages"]
    stage = request["stage"]
    operations = [{"action": "update_budget", "payload": {"budget": 4900}}]
    replies = [json.loads(message["content"]) for message in messages if message["role"] == "tool"]
    content, calls = "", []
    if stage == "review_parent":
        if replies:
            assert replies[-1]["status"] == "success", replies[-1]
            assert replies[-1]["data"]["proposal_kind"] == "changes", replies[-1]
            content = json.dumps({"kind": "changes", "message": "预算调整为 4900 元。",
                                  "question": None, "options": [], "candidate_groups": []})
        else:
            calls = [{"id": "budget-edit", "function": {"name": "delegate_task", "arguments": {
                "task_id": "budget", "role": "build_edits", "task": "将总预算改为 4900 元",
                "extra_data": {"facts": "", "artifact_refs": []},
            }}}]
    elif stage == "build_edits":
        if replies:
            assert replies[-1]["status"] == "success" and replies[-1]["data"]["accepted"], replies[-1]
            content = json.dumps({"status": "completed", "reason": "", "data": {
                "message": "", "remaining": [], "kind": "changes", "operations": operations,
            }})
        else:
            snapshot = json.loads(messages[1]["content"])["extra_data"]["snapshot"]
            calls = [{"id": "budget-preview", "function": {"name": "preview_review_changes", "arguments": {
                "plan_version": snapshot["plan_version"], "operations": operations,
            }}}]
    else:
        assert stage == "review_public_reply", stage
        return None, messages, None
    return SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=calls)), messages, None


async def rewrite_review(**request):
    text = request["text"]
    return {"original_text": text, "rewritten_text": text, "references": [], "unresolved_references": []}


async def check_public_stdio() -> None:
    from agent.tools.context import ToolContext
    from agent.tools.dispatcher import ToolExecutor
    from infrastructure.mcp import connect_tools

    async with connect_tools(ROOT / "mcp_server.py") as registry:
        assert len(registry.names) == 26
        result = await ToolExecutor(registry).execute("list_travel_plans", {}, ToolContext(),
                                                     allowed={"list_travel_plans"})
        assert result.status == "success" and isinstance(result.data["plans"], list), result
    print("PASS public MCP stdio tool call")


def browser_flow(app) -> None:
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="e2e-assets")
    app.add_api_route("/plans/{path:path}", lambda: FileResponse(DIST / "index.html"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, log_level="warning", timeout_graceful_shutdown=5))
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 10
            while not server.started and thread.is_alive() and monotonic() < deadline:
                sleep(0.05)
            if not server.started:
                raise RuntimeError("E2E API did not start")
            subprocess.run(
                ["node", str(ROOT / "frontend/tests/travelFlow.e2e.mjs"), f"http://127.0.0.1:{port}"],
                cwd=ROOT, check=True, timeout=120,
            )
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("E2E API did not shut down")


def main() -> None:
    required = ("TRAVEL_DB_HOST", "TRAVEL_DB_PORT", "TRAVEL_DB_USER", "TRAVEL_DB_PASSWORD")
    if any(key not in os.environ for key in required) or os.environ["TRAVEL_DB_HOST"] != "127.0.0.1":
        raise SystemExit("Set all TRAVEL_DB connection variables for a local disposable MySQL server.")
    if not (DIST / "index.html").is_file():
        raise SystemExit("Build the E2E frontend first. See Verify Skill.")
    # Import configuration only after selecting a schema owned by this run.
    schema = f"travel_e2e_{uuid4().hex}"
    os.environ.update(TRAVEL_DB_NAME=schema, AMAP_API_KEY="", TRAVEL_LOG_HTTP_BODY="0")
    from service.api import server
    from service.storage import travel_db
    from tests.planning_fixtures import mock_itinerary_proposal

    with travel_db.connect(database=None) as conn:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{schema}`")
    try:
        travel_db.ensure_schema()
        asyncio.run(check_public_stdio())
        fixtures = {
            "service.travel.planning.analyze_guide": guide_result,
            "agent.planning.implementations.itinerary_nodes.generate_itinerary_proposal": mock_itinerary_proposal,
            "service.travel.place_matching.amap_search_places": map_search,
            "agent.planning.implementations.routes.amap_compute_route": map_route,
            "service.api.server.place_photo_url": lambda *args: None,
            "service.api.server.generate_roadbook":
                lambda snapshot, model: {str(day): "、".join(stops) for day, stops in snapshot["days"].items()},
            "service.travel.review_interaction.create_model_client": object,
            "agent.chat.review.workflow.rewrite_request": rewrite_review,
            "infrastructure.llm.request_model_with_retry": review_model,
            "service.travel.review_public_reply.request_model_with_retry": review_model,
        }
        with ExitStack() as stack:
            for target, replacement in fixtures.items():
                stack.enter_context(patch(target, replacement))
            stack.enter_context(patch("service.config.AMAP_API_KEY", "fixture"))
            browser_flow(server.create_app(ROOT / "mcp_server.py"))
        print("PASS travel planning browser flow")
    finally:
        with travel_db.connect(database=None) as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"DROP DATABASE `{schema}`")


if __name__ == "__main__":
    main()
