"""Persistent generation jobs and atomic compare-before-commit on the plan row."""
import json
from contextlib import contextmanager
from hashlib import sha256
from uuid import uuid4
from service.storage.travel_db import connect, ensure_schema, row_to_plan


@contextmanager
def transaction():
    with connect() as conn:
        conn.begin()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def fingerprint(plan):
    return sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def ensure_jobs():
    ensure_schema()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS planning_jobs (
            plan_id VARCHAR(191) PRIMARY KEY, job_id CHAR(32) NOT NULL,
            input_hash CHAR(64) NOT NULL, status VARCHAR(20) NOT NULL,
            stage VARCHAR(80) NOT NULL, error TEXT NOT NULL,
            updated_at DATETIME NOT NULL) CHARACTER SET utf8mb4""")
        cur.execute("""CREATE TABLE IF NOT EXISTS planning_job_checkpoints (
            plan_id VARCHAR(191) PRIMARY KEY, input_hash CHAR(64) NOT NULL,
            state_json LONGTEXT NOT NULL, updated_at DATETIME NOT NULL
        ) CHARACTER SET utf8mb4""")


def read_checkpoint(plan_id, input_hash):
    ensure_jobs()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT state_json FROM planning_job_checkpoints WHERE plan_id=%s AND input_hash=%s", (plan_id, input_hash))
        row = cur.fetchone()
        return json.loads(row["state_json"]) if row else None


def save_checkpoint(plan_id, input_hash, state):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""INSERT INTO planning_job_checkpoints VALUES (%s,%s,%s,UTC_TIMESTAMP())
            ON DUPLICATE KEY UPDATE input_hash=VALUES(input_hash),state_json=VALUES(state_json),updated_at=UTC_TIMESTAMP()""",
            (plan_id, input_hash, json.dumps(state, ensure_ascii=False, default=str)))


def recover_jobs():
    ensure_jobs()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE planning_jobs SET status='failed', error=%s WHERE status IN ('queued','running')",
                    ("服务重启中断了生成，请重新生成；原行程已保留。",))


def read_job(plan_id):
    ensure_jobs()
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM planning_jobs WHERE plan_id=%s", (plan_id,))
        row = cur.fetchone()
        if row:
            row["updated_at"] = str(row["updated_at"])
        return row


def create_job(plan_id, payload):
    ensure_jobs()
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM travel_plans WHERE plan_id=%s FOR UPDATE", (plan_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Plan not found")
        plan = row_to_plan(row)
        cur.execute("SELECT * FROM planning_jobs WHERE plan_id=%s FOR UPDATE", (plan_id,))
        previous = cur.fetchone()
        digest = fingerprint({"plan": plan, "payload": payload})
        if previous and previous["status"] in {"queued", "running"}:
            if previous["input_hash"] != digest:
                raise ValueError("该行程正在生成，请等待当前任务结束后重新提交")
            return previous["job_id"], None
        cur.execute("DELETE FROM planning_job_checkpoints WHERE plan_id=%s AND input_hash<>%s", (plan_id, digest))
        job_id = uuid4().hex
        cur.execute("""INSERT INTO planning_jobs VALUES (%s,%s,%s,'queued','queued','',UTC_TIMESTAMP())
            ON DUPLICATE KEY UPDATE job_id=VALUES(job_id), input_hash=VALUES(input_hash),
            status='queued',stage='queued',error='',updated_at=UTC_TIMESTAMP()""", (plan_id, job_id, digest))
        return job_id, plan


def update_job(plan_id, job_id, status, stage, error=""):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE planning_jobs SET status=%s,stage=%s,error=%s,updated_at=UTC_TIMESTAMP() WHERE plan_id=%s AND job_id=%s",
                    (status, stage, error, plan_id, job_id))


def commit_result(plan_id, job_id, original, result):
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM travel_plans WHERE plan_id=%s FOR UPDATE", (plan_id,))
        row = cur.fetchone()
        if not row or fingerprint(row_to_plan(row)) != fingerprint(original):
            raise ValueError("生成期间行程已修改，本次结果未覆盖新版本，请重新生成")
        cur.execute("SELECT job_id,status FROM planning_jobs WHERE plan_id=%s FOR UPDATE", (plan_id,))
        job = cur.fetchone()
        if not job or job["job_id"] != job_id or job["status"] != "running":
            raise ValueError("生成任务已失效")
        cur.execute("""UPDATE travel_plans SET itinerary=%s,routes=%s,planning=%s,budget_items=%s,
            updated_at=UTC_TIMESTAMP() WHERE plan_id=%s""",
            tuple(json.dumps(result.get(k, [] if k != "planning" else {}), ensure_ascii=False)
                  for k in ("itinerary", "routes", "planning", "budget_items")) + (plan_id,))
        cur.execute("UPDATE planning_jobs SET status='completed',stage='completed',updated_at=UTC_TIMESTAMP() WHERE plan_id=%s AND job_id=%s", (plan_id, job_id))
        cur.execute("DELETE FROM planning_job_checkpoints WHERE plan_id=%s", (plan_id,))
