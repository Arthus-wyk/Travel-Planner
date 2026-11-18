from copy import deepcopy

from agent.planning.planning_graph import run_planning_action
from service.storage import planning_jobs
from service.storage.travel_db import get_plan
from service.travel.itinerary_audit import audit_plan


# ============试算审核变更 ============
def preview_review_action(plan: dict, action: str, payload: dict) -> dict:
    if action not in {"review_changes", "optimize_routes"}:
        raise ValueError("不支持的路线审核操作")
    matches = (
        payload["plan_version"] == planning_jobs.fingerprint(plan)
        if "plan_version" in payload else payload.get("base_plan") == plan
    )
    if not matches:
        raise planning_jobs.PlanConflict("行程已在其他页面修改，请重新读取后保存")
    return run_planning_action(deepcopy(plan), action, payload)


# ============验收试算结果 ============
def review_acceptance(original: dict, outcome: dict) -> dict:
    audit = outcome.get("audit") or audit_plan(outcome["plan"])
    existing = {
        (issue["type"], repr(issue.get("target")), issue.get("message"))
        for issue in audit_plan(original)["issues"] if issue["severity"] == "high"
    }
    blockers = [
        issue for issue in audit["issues"] if issue["severity"] == "high"
        and (issue["type"], repr(issue.get("target")), issue.get("message")) not in existing
    ]
    return {
        "accepted": not blockers and outcome.get("optimization_applied", True),
        "blocking_issues": blockers,
        "audit": audit,
    }


# ============保存审核变更 ============
def save_review_action(plan: dict, action: str, payload: dict, *, require_acceptance: bool = False) -> dict:
    outcome = preview_review_action(plan, action, payload)
    if require_acceptance:
        acceptance = review_acceptance(plan, outcome)
        if not acceptance["accepted"]:
            return {**outcome, **acceptance}
    if not outcome.get("optimization_applied", True):
        return {**outcome, "plan": plan}
    plan_id = plan["plan_id"]
    planning_jobs.save_review_changes(plan_id, plan, outcome["plan"])
    saved, error = get_plan(plan_id)
    if error or saved is None:
        raise RuntimeError("修改已保存，但读取结果失败，请重新读取核实")
    return {**outcome, "plan": saved}
