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
from fastapi import WebSocket, WebSocketDisconnect, BackgroundTasks, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from agent.planning.planning_graph import run_planning_action
from agent.planning.roadbook import generate_roadbook, roadbook_signature, roadbook_snapshot
from service.chat import handle_chat, public_message_items
from service.runtime_logs import event_entries, list_runs, read_run
from service.storage.travel_db import (
    delete_plan, get_conversation, get_plan, list_conversations, list_messages,
    list_plan_summaries, save_conversation, save_plan,
)
from service.travel.itinerary_audit import audit_plan
from service.travel.guide import split_guide_days
from service.travel.planning import create_draft, itinerary_sheet
from service.travel.map_planning import day_map, roadbook_map
from service.storage import planning_jobs
from service.travel.generation_jobs import execute_job
from service.integrations.travel_amap import place_photo_url, retry_amap_call
from service.mcp.server import amap_search_places
from service.storage.travel_db import read_map_cache
from service.utils.model_client import AVAILABLE_MODELS, validate_model


def _public_plan(plan):
    planning = plan.get("planning") or {}
    if planning.get("guide_text") and "guide_days" not in planning:
        days, _ = split_guide_days(planning["guide_text"])
        plan = {**plan, "planning": {**planning, "guide_days": days}}
    if "routes" not in plan:
        return plan
    routes = []
    for route in plan.get("routes") or []:
        item = {key: value for key, value in route.items() if key not in {"legs", "route_options", "route_options_key"}}
        if route.get("route_options"):
            item["route_options"] = {
                mode: {key: option[key] for key in ("success", "duration", "duration_minutes", "fare",
                                                   "distance_meters", "message", "source") if key in option}
                for mode, option in route["route_options"].items() if isinstance(option, dict)
            }
        routes.append(item)
    return {**plan, "routes": routes}


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


def create_app(server_file: Path, model: str = AVAILABLE_MODELS[0]) -> FastAPI:
    app = FastAPI(title="Travel Planner API", version="0.1")
    try:
        from service.api.xiaohongshu import router as xiaohongshu_router
    except ModuleNotFoundError as exc:
        if exc.name != "service.api.xiaohongshu":
            raise
    else:
        app.include_router(xiaohongshu_router)
    from service.api.request_logging import RequestLoggingMiddleware

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
    def plans():
        data, error = list_plan_summaries()
        return _result({"plans": data}, error)

    @app.get("/api/plans/{plan_id}")
    def plan_detail(plan_id: str):
        data, error = get_plan(plan_id)
        return _result({"plan": _public_plan(data), "plan_version": planning_jobs.fingerprint(data)} if data is not None else {},
                       error, 404 if error and error.get("message") == "Travel plan not found." else 500)

    @app.delete("/api/plans/{plan_id}")
    def remove_plan(plan_id: str):
        deleted, error = delete_plan(plan_id)
        if error:
            return _result({}, error)
        if not deleted:
            raise HTTPException(status_code=404, detail="Travel plan not found.")
        return {"success": True}

    @app.post("/api/plans/drafts")
    def plan_draft(payload: dict = Body(...)):
        plan = create_draft(payload, uuid.uuid4().hex)
        save_plan(plan)
        return {"success": True, "plan": plan}

    @app.get("/api/plans/{plan_id}/conversations")
    def plan_conversations(plan_id: str):
        data, error = list_conversations(plan_id)
        return _result({"conversations": data}, error)

    @app.get("/api/plans/{plan_id}/planning/audit")
    def plan_audit(plan_id: str):
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
        result = retry_amap_call(amap_search_places, query.strip(), region_code=plan["destination"])
        if not result.get("success"):
            raise HTTPException(status_code=502, detail=result.get("message", "地点搜索失败"))
        return {"success": True, "places": result["places"]}

    @app.get("/api/plans/{plan_id}/planning/map")
    def plan_day_map(plan_id: str, day: int = 1, sheet: bool = False):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        return day_map(plan, day, include_sheet=True) if sheet else day_map(plan, day)

    def confirmed_roadbook_plan(plan_id):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail="Plan not found")
        planning = plan.get("planning") or {}
        if planning.get("stage") != "confirmed" or planning.get("needs_replan"):
            raise HTTPException(status_code=409, detail="请先确认行程")
        return plan

    @app.get("/api/plans/{plan_id}/roadbook/map")
    def plan_roadbook_map(plan_id: str):
        return roadbook_map(confirmed_roadbook_plan(plan_id))

    @app.post("/api/plans/{plan_id}/roadbook")
    def plan_roadbook(plan_id: str):
        plan = confirmed_roadbook_plan(plan_id)
        planning = plan["planning"]
        snapshot = roadbook_snapshot(plan)
        signature = roadbook_signature(snapshot)
        saved = planning.get("roadbook") or {}
        route_model = (planning.get("generation") or {}).get("model")
        if route_model not in AVAILABLE_MODELS:
            route_model = model
        if saved.get("signature") == signature and saved.get("model") == route_model:
            return {"summaries": saved["summaries"]}
        try:
            summaries = generate_roadbook(snapshot, route_model)
        except Exception as exc:
            logging.getLogger(__name__).warning("路书概述生成失败", exc_info=True)
            raise HTTPException(status_code=502, detail="路书概述生成失败，请重试。") from exc
        result = {**plan, "planning": {**planning, "roadbook": {
            "signature": signature, "model": route_model, "summaries": summaries,
        }}}
        try:
            planning_jobs.save_review_changes(plan_id, plan, result)
        except planning_jobs.PlanConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"summaries": summaries}

    @app.get("/api/maps/images/{image_key}")
    def map_image(image_key: str):
        if not re.fullmatch(r"[0-9a-f]{64}", image_key):
            raise HTTPException(status_code=404, detail="Map image not found")
        data = read_map_cache(image_key, allow_expired=True)
        if not isinstance(data, str) or not data.startswith("data:image/"):
            raise HTTPException(status_code=404, detail="Map image expired; reload its preview")
        header, body = data.split(",", 1)
        return Response(base64.b64decode(body), media_type=header[5:].split(";")[0], headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/maps/place-photos/{poi_id}")
    def place_photo(poi_id: str):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", poi_id):
            raise HTTPException(status_code=404, detail="Place photo not found")
        try:
            url = place_photo_url(poi_id)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Place photo temporarily unavailable") from exc
        if not url:
            raise HTTPException(status_code=404, detail="Place photo not found")
        return RedirectResponse(url, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/plans/{plan_id}/sheet")
    def plan_sheet(plan_id: str):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail=(error or {}).get("message", "Plan not found"))
        planning = plan.get("planning") or {}
        if planning.get("stage") != "confirmed" or planning.get("needs_replan"):
            raise HTTPException(status_code=400, detail="Confirm the itinerary before opening its sheet")
        return {"success": True, "sheet": itinerary_sheet(_public_plan(plan))}

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
        except Exception as exc:
            from service.utils.request_log import log_error
            log_error(exc, plan_id=plan_id, stage="generation_websocket")
            logging.getLogger(__name__).exception("generation websocket failed plan_id=%s", plan_id)
            try:
                await websocket.close(code=1011, reason="无法读取生成状态")
            except Exception:
                pass

    @app.get("/api/models")
    def models():
        return {"models": AVAILABLE_MODELS, "default": AVAILABLE_MODELS[0]}

    @app.post("/api/plans/{plan_id}/generation")
    def generate_from_guide(plan_id: str, background: BackgroundTasks, payload: dict = Body(...), retry: bool = False):
        if not isinstance(payload.get("text", ""), str) or len(payload.get("text", "")) > 20000:
            raise ValueError("攻略文字最长 20000 字")
        if not payload.get("image_base64") and not payload.get("text", "").strip():
            raise ValueError("请上传攻略图片或填写攻略文字")
        if "model" in payload:
            validate_model(payload["model"])
        job_id, plan = planning_jobs.create_job(plan_id, payload, retry=retry)
        if plan is not None:
            background.add_task(execute_job, plan_id, job_id, plan, payload, retry=retry)
        return {"success": True, "job_id": job_id}

    @app.get("/api/plans/{plan_id}/generation")
    def generation_status(plan_id: str):
        return {"success": True, "job": planning_jobs.read_job(plan_id)}

    @app.post("/api/plans/{plan_id}/planning/{action}")
    def planning_action(plan_id: str, action: str, payload: dict = Body(...), include_version: bool = False):
        plan, error = get_plan(plan_id)
        if error or plan is None:
            raise HTTPException(status_code=404, detail=(error or {}).get("message", "Plan not found"))
        if action in {'review_changes', 'optimize_routes'} and (
            payload.get('plan_version') != planning_jobs.fingerprint(plan)
            if 'plan_version' in payload else payload.get('base_plan') != plan
        ):
            raise HTTPException(status_code=409, detail='行程已在其他页面修改，请重新读取后保存')
        outcome = run_planning_action(plan, action, payload)
        if action in {'review_changes', 'optimize_routes'}:
            if outcome.get('optimization_applied', True):
                try:
                    planning_jobs.save_review_changes(plan_id, plan, outcome['plan'])
                except planning_jobs.PlanConflict as exc:
                    raise HTTPException(status_code=409, detail=str(exc)) from exc
                saved, saved_error = get_plan(plan_id)
                if saved_error or saved is None:
                    raise HTTPException(status_code=500, detail='修改已保存，但读取结果失败，请重新读取核实')
                outcome['plan'] = saved
            else:
                outcome['plan'] = plan
        else:
            save_plan(outcome["plan"])
            if include_version:
                saved, saved_error = get_plan(plan_id)
                if saved_error or saved is None:
                    raise HTTPException(status_code=500, detail='修改已保存，但读取结果失败，请重新读取核实')
                outcome['plan'] = saved
        response = {"success": True, "plan": _public_plan(outcome["plan"]), "trace": outcome["trace"]}
        if action in {'review_changes', 'optimize_routes'} or include_version:
            response["plan_version"] = planning_jobs.fingerprint(outcome["plan"])
        if outcome.get("audit"):
            response["audit"] = outcome["audit"]
        if action == 'optimize_routes':
            response['optimization_message'] = outcome['optimization_message']
        return response

    @app.get("/api/conversations")
    def conversations():
        data, error = list_conversations()
        return _result({"conversations": data}, error)

    @app.post("/api/conversations")
    def create_conversation(payload: dict = Body(...)):
        conversation = new_conversation(
            conversation_id=str(payload.get("conversation_id") or uuid.uuid4().hex),
            plan_id=payload.get("plan_id") or None,
            title=str(payload.get("title") or "新对话"),
        )
        save_conversation(conversation)
        return {"success": True, "conversation": conversation}

    @app.get("/api/conversations/{conversation_id}")
    def conversation_detail(conversation_id: str):
        data, error = get_conversation(conversation_id)
        missing = error and "not found" in error.get("message", "").lower()
        return _result({"conversation": data}, error, 404 if missing else 500)

    @app.get("/api/conversations/{conversation_id}/messages")
    def conversation_messages(conversation_id: str):
        data, error = list_messages(conversation_id)
        missing = error and "not found" in error.get("message", "").lower()
        return _result({"messages": public_message_items(data or [])}, error, 404 if missing else 500)

    @app.post("/api/conversations/{conversation_id}/messages")
    async def send_conversation_message(conversation_id: str, payload: dict = Body(...)):
        message = str(payload.get("content") or payload.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="content must not be empty.")
        return await handle_chat(server_file=server_file, model=validate_model(payload["model"]) if "model" in payload else model,
                                 conversation_id=conversation_id, user_message=message)

    @app.get("/api/logs/runs")
    def runtime_runs(
        request: Request,
        conversation_id: str = "",
        job_id: str = "",
        status: str = "",
        since: str = "",
        until: str = "",
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
    ):
        _require_local_log_request(request)
        return {"success": True, **list_runs(Path.cwd(), conversation_id=conversation_id, job_id=job_id,
                                             status=status, since=since, until=until, page=page, page_size=page_size)}

    @app.get("/api/logs/runs/{run_id}")
    def runtime_run(request: Request, run_id: str, conversation_id: str = "", view: str = "", search: str = ""):
        _require_local_log_request(request)
        data = read_run(Path.cwd(), run_id, conversation_id=conversation_id)
        if data is None:
            raise HTTPException(status_code=404, detail="Run log not found.")
        if view == "timeline":
            events = [summary for summary, _, _ in event_entries(data["events"], search)]
            return {"success": True, **data, "events": events}
        return {"success": True, **data}

    @app.get("/api/logs/runs/{run_id}/events/{event_id}")
    def runtime_event(request: Request, run_id: str, event_id: str, conversation_id: str = ""):
        _require_local_log_request(request)
        data = read_run(Path.cwd(), run_id, conversation_id=conversation_id)
        if data:
            for summary, event, paired_request in event_entries(data["events"]):
                if summary["eventId"] == event_id:
                    return {"success": True, "event": event, "request": paired_request}
        raise HTTPException(status_code=404, detail="Event log not found.")

    app.add_middleware(RequestLoggingMiddleware)
    return app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="FastAPI server for the travel planner.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    parser.add_argument("--server", type=Path, default=Path(__file__).resolve().parents[2] / "mcp_server.py")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    planning_jobs.recover_jobs()
    uvicorn.run(create_app(args.server.resolve()), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
