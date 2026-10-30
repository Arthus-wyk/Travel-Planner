"""FastAPI HTTP adapter for the travel planner API."""
from __future__ import annotations

import asyncio
import logging
import argparse
import traceback
import uuid
import base64
import re
from datetime import datetime
from pathlib import Path

import uvicorn
from fastapi import WebSocket, WebSocketDisconnect, BackgroundTasks, Body, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from agent.planning_graph import run_planning_action
from service.chat import handle_chat, public_message_items
from service.runtime_logs import list_runs, read_run
from service.storage.travel_db import (
    delete_plan, get_conversation, get_plan, list_conversations, list_messages,
    list_plan_summaries, save_conversation, save_plan,
)
from service.travel.itinerary_audit import audit_plan
from service.travel.planning import create_draft, itinerary_sheet
from service.travel.map_planning import day_map
from service.storage import planning_jobs
from service.travel.generation_jobs import execute_job
from service.mcp.server import amap_search_places
from service.storage.travel_db import read_map_cache
from service.utils.model_client import get_model


def new_conversation(*, conversation_id: str, plan_id: str | None, title: str) -> dict:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return {"conversation_id": conversation_id, "plan_id": plan_id, "title": title,
            "created_at": now, "updated_at": now}


def _result(data, error=None, error_status=500):
    return JSONResponse({"success": False, **error}, status_code=error_status) if error else {"success": True, **data}


def _require_local_log_request(request: Request) -> None:
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    origin = request.headers.get("origin", "")
    origin_host = origin.split("//")[-1].split(":")[0]
    if (not request.client or request.client.host not in local_hosts
            or request.url.hostname not in local_hosts
            or (origin and origin_host not in local_hosts)):
        raise HTTPException(status_code=403, detail="Runtime logs are available only on localhost.")


def create_app(server_file: Path, model: str) -> FastAPI:
    app = FastAPI(title="Travel Planner API", version="0.1")

    @app.middleware("http")
    async def api_headers(request: Request, call_next):
        if int(request.headers.get("content-length", "0") or 0) > 15 * 1024 * 1024:
            return JSONResponse({"success": False, "message": "Request body exceeds 15 MB"}, status_code=400)
        response = await call_next(request)
        response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        if request.url.path.startswith("/api/logs/"):
            origin = request.headers.get("origin", "")
            if origin.split("//")[-1].split(":")[0] in {"127.0.0.1", "localhost", "::1"}:
                response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"
            response.headers["Cache-Control"] = "no-store"
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError):
        return JSONResponse({"success": False, "message": str(exc)}, status_code=400)

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        return JSONResponse({"success": False, "message": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(Exception)
    async def server_error_handler(request: Request, exc: Exception):
        traceback.print_exc()
        return JSONResponse({"success": False, "message": str(exc)}, status_code=500)

    @app.options("/{path:path}")
    async def options():
        return {"ok": True}

    @app.get("/api/health")
    async def health():
        return {"ok": True, "model": model, "routes": ["plans", "planning", "conversations", "messages", "chat"]}

    @app.get("/api/plans")
    async def plans():
        data, error = list_plan_summaries()
        return _result({"plans": data}, error)

    @app.get("/api/plans/{plan_id}")
    async def plan_detail(plan_id: str):
        data, error = get_plan(plan_id)
        return _result({"plan": data}, error, 404 if error and error.get("message") == "Travel plan not found." else 500)

    @app.delete("/api/plans/{plan_id}")
    async def remove_plan(plan_id: str):
        deleted, error = delete_plan(plan_id)
        if error:
            return _result({}, error)
        if not deleted:
            raise HTTPException(status_code=404, detail="Travel plan not found.")
        return {"success": True}

    @app.post("/api/plans/drafts")
    async def plan_draft(payload: dict = Body(...)):
        plan = create_draft(payload, uuid.uuid4().hex)
        save_plan(plan)
        return {"success": True, "plan": plan}

    @app.get("/api/plans/{plan_id}/conversations")
    async def plan_conversations(plan_id: str):
        data, error = list_conversations(plan_id)
        return _result({"conversations": data}, error)

    @app.get("/api/plans/{plan_id}/planning/audit")
    async def plan_audit(plan_id: str):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail=(error or {}).get("message", "Plan not found"))
        return {"success": True, "audit": audit_plan(plan)}

    @app.post("/api/plans/{plan_id}/planning/places/search")
    def place_search(plan_id: str, payload: dict = Body(...)):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        query = payload.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
            raise ValueError("请输入 1–100 字的地点名称")
        result = amap_search_places(query.strip(), region_code=plan["destination"])
        if not result.get("success"):
            raise HTTPException(status_code=502, detail=result.get("message", "地点搜索失败"))
        return {"success": True, "places": result["places"]}

    @app.get("/api/plans/{plan_id}/planning/map")
    def plan_day_map(plan_id: str, day: int = 1):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        result = day_map(plan, day)
        return {**result, "image_url": f"/api/maps/images/{result['image_cache_key']}"}

    @app.get("/api/maps/images/{image_key}")
    def map_image(image_key: str):
        if not re.fullmatch(r"[0-9a-f]{64}", image_key):
            raise HTTPException(status_code=404, detail="Map image not found")
        data = read_map_cache(image_key, allow_expired=True)
        if not isinstance(data, str) or not data.startswith("data:image/"):
            raise HTTPException(status_code=404, detail="Map image expired; reload its preview")
        header, body = data.split(",", 1)
        return Response(base64.b64decode(body), media_type=header[5:].split(";")[0], headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/plans/{plan_id}/sheet")
    async def plan_sheet(plan_id: str):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail=(error or {}).get("message", "Plan not found"))
        if (plan.get("planning") or {}).get("stage") != "confirmed":
            raise HTTPException(status_code=400, detail="Confirm the itinerary before opening its sheet")
        return {"success": True, "sheet": itinerary_sheet(plan)}

    @app.websocket("/api/plans/{plan_id}/generation/ws")
    async def generation_socket(websocket: WebSocket, plan_id: str):
        await websocket.accept()
        previous = None
        try:
            while True:
                job = await asyncio.to_thread(planning_jobs.read_job, plan_id)
                packet = {"type": "generation_status", "job": job}
                if packet != previous:
                    await websocket.send_json(packet)
                    previous = packet
                else:
                    await websocket.send_json({"type": "heartbeat"})
                await asyncio.sleep(1)
        except WebSocketDisconnect:
            pass
        except Exception:
            logging.getLogger(__name__).exception("generation websocket failed plan_id=%s", plan_id)
            try:
                await websocket.close(code=1011, reason="无法读取生成状态")
            except Exception:
                pass

    @app.post("/api/plans/{plan_id}/generation")
    def generate_from_guide(plan_id: str, background: BackgroundTasks, payload: dict = Body(...)):
        if not isinstance(payload.get("text", ""), str) or len(payload.get("text", "")) > 20000:
            raise ValueError("攻略文字最长 20000 字")
        if not payload.get("image_base64") and not payload.get("text", "").strip():
            raise ValueError("请上传攻略图片或填写攻略文字")
        job_id, plan = planning_jobs.create_job(plan_id, payload)
        if plan is not None:
            background.add_task(execute_job, plan_id, job_id, plan, payload)
        return {"success": True, "job_id": job_id}

    @app.get("/api/plans/{plan_id}/generation")
    def generation_status(plan_id: str):
        return {"success": True, "job": planning_jobs.read_job(plan_id)}

    @app.post("/api/plans/{plan_id}/planning/{action}")
    def planning_action(plan_id: str, action: str, payload: dict = Body(...)):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail=(error or {}).get("message", "Plan not found"))
        outcome = run_planning_action(plan, action, payload)
        save_plan(outcome["plan"])
        response = {"success": True, "plan": outcome["plan"], "trace": outcome["trace"]}
        if outcome.get("audit"):
            response["audit"] = outcome["audit"]
        return response

    @app.get("/api/conversations")
    async def conversations():
        data, error = list_conversations()
        return _result({"conversations": data}, error)

    @app.post("/api/conversations")
    async def create_conversation(payload: dict = Body(...)):
        conversation = new_conversation(
            conversation_id=str(payload.get("conversation_id") or uuid.uuid4().hex),
            plan_id=payload.get("plan_id") or None,
            title=str(payload.get("title") or "新对话"),
        )
        save_conversation(conversation)
        return {"success": True, "conversation": conversation}

    @app.get("/api/conversations/{conversation_id}")
    async def conversation_detail(conversation_id: str):
        data, error = get_conversation(conversation_id)
        missing = error and "not found" in error.get("message", "").lower()
        return _result({"conversation": data}, error, 404 if missing else 500)

    @app.get("/api/conversations/{conversation_id}/messages")
    async def conversation_messages(conversation_id: str):
        data, error = list_messages(conversation_id)
        missing = error and "not found" in error.get("message", "").lower()
        return _result({"messages": public_message_items(data or [])}, error, 404 if missing else 500)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_conversation_message(conversation_id: str, payload: dict = Body(...)):
        message = str(payload.get("content") or payload.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="content must not be empty.")
        return await handle_chat(server_file=server_file, model=str(payload.get("model") or model),
                                 conversation_id=conversation_id, user_message=message)

    @app.post("/api/chat")
    async def chat(payload: dict = Body(...)):
        message = str(payload.get("message", "")).strip()
        conversation_id = str(payload.get("conversation_id") or uuid.uuid4().hex)
        if not message:
            raise HTTPException(status_code=400, detail="message must not be empty.")
        if not get_conversation(conversation_id)[0]:
            save_conversation(new_conversation(conversation_id=conversation_id, plan_id=payload.get("plan_id"), title="新对话"))
        return await handle_chat(server_file=server_file, model=str(payload.get("model") or model),
                                 conversation_id=conversation_id, user_message=message)

    @app.get("/api/logs/runs")
    async def runtime_runs(
        request: Request,
        conversation_id: str = "",
        status: str = "",
        since: str = "",
        until: str = "",
    ):
        _require_local_log_request(request)
        return {"success": True, **list_runs(Path.cwd(), conversation_id=conversation_id, status=status, since=since, until=until)}

    @app.get("/api/logs/runs/{run_id}")
    async def runtime_run(request: Request, run_id: str, conversation_id: str = ""):
        _require_local_log_request(request)
        data = read_run(Path.cwd(), run_id, conversation_id=conversation_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Run log not found.")
        return {"success": True, **data}

    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FastAPI server for the travel planner.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--server", type=Path, default=Path(__file__).resolve().parents[2] / "mcp_server.py")
    args = parser.parse_args()
    try: args.model = get_model()
    except ValueError as exc: parser.error(str(exc))
    return args


def main() -> None:
    args = parse_args()
    planning_jobs.recover_jobs()
    uvicorn.run(create_app(args.server.resolve(), args.model), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
