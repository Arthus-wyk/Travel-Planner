import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger("travel.generation")
CHECKPOINT_SCHEMA_VERSION = 5
if not logger.handlers:
    directory = Path(__file__).resolve().parents[2] / "logs"
    directory.mkdir(exist_ok=True)
    handler = RotatingFileHandler(directory / "generation.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

from agent.planning.planning_graph import after_audit, after_itinerary, generation_job_id, run_planning_action
from service.travel.guide import GuideNeedsInput
from service.storage import planning_jobs as store
from service.storage.travel_db import compact_routes
import json


def next_stage(stage, state):
    stages = {"coordinator": "generate_guide", "activity_agent": "search_places",
              "place_match_agent": "default_choices", "default_choices": "prepare_draft",
              "prepare_draft": "itinerary_agent", "route_agent": "schedule_agent", "schedule_agent": "audit_agent"}
    if stage == "itinerary_agent":
        return after_itinerary(state)
    if stage == "audit_agent":
        return after_audit(state)
    return stages.get(stage, "done")


def execute_job(plan_id, job_id, plan, payload, *, retry=False):
    logger.info("started plan_id=%s job_id=%s", plan_id, job_id)
    digest = store.fingerprint({"plan": plan, "payload": payload})
    def progress(stage, state):
        following = next_stage(stage, state)
        logger.info("checkpoint plan_id=%s job_id=%s stage=%s next=%s attempts=%s",
                    plan_id, job_id, stage, following, state.get("attempts", 0))
        current = state["plan"]
        store.save_checkpoint(plan_id, digest, {**state, "plan": {**current, "routes": compact_routes(current.get("routes") or [])},
                                                "payload": {}, "resume_stage": following,
                                                "schema_version": CHECKPOINT_SCHEMA_VERSION})
        store.update_job(plan_id, job_id, "running", stage)
    try:
        resume = store.read_checkpoint(plan_id, digest) if retry else None
        if resume and resume.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            resume = None
        if resume:
            resume["payload"] = payload
            if resume.get("resume_stage") == "itinerary_agent":
                resume["format_attempts"] = 0
            logger.info("resuming plan_id=%s job_id=%s next=%s", plan_id, job_id, resume["resume_stage"])
        store.update_job(plan_id, job_id, "running", "starting")
        token = generation_job_id.set(job_id)
        try:
            outcome = run_planning_action(plan, "generate_from_guide", payload,
                progress=progress, resume=resume)
        finally:
            generation_job_id.reset(token)
        store.commit_result(plan_id, job_id, plan, outcome["plan"])
        for event in outcome["plan"]["planning"].get("place_events", []):
            logger.info("place_event plan_id=%s job_id=%s event=%s", plan_id, job_id, event)
        logger.info("completed plan_id=%s job_id=%s", plan_id, job_id)
    except GuideNeedsInput as exc:
        logger.warning("needs_input plan_id=%s job_id=%s reason=%s", plan_id, job_id, exc)
        if getattr(exc, 'plan', None) is not None:
            try:
                store.commit_result(plan_id, job_id, plan, {**plan, 'planning': exc.plan['planning']}, needs_input=str(exc)[:500])
            except Exception as save_error:
                logger.exception('failed to save partial generation plan_id=%s', plan_id)
                store.update_job(plan_id, job_id, 'failed', 'failed', str(save_error)[:500])
            return
        store.update_job(plan_id, job_id, "needs_input", "guide_agent", str(exc)[:500])
    except Exception as exc:
        from service.utils.request_log import log_error
        log_error(exc, plan_id=plan_id, job_id=job_id, stage="generation_job")
        logger.exception("failed plan_id=%s job_id=%s", plan_id, job_id)
        store.update_job(plan_id, job_id, "failed", "failed", str(exc)[:500])
