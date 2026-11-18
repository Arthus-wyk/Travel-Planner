"""存储审核页暂停状态，并原子认领一次恢复请求。"""

import json
from functools import lru_cache

from service.storage.planning_jobs import PlanConflict, transaction


# ============初始化审核断点表 ============
@lru_cache(maxsize=1)
def ensure_review_checkpoints() -> None:
    """按需创建断点表，并为过期清理保留索引。"""
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("""CREATE TABLE IF NOT EXISTS review_checkpoints (
            checkpoint_id CHAR(32) PRIMARY KEY, plan_id VARCHAR(191) NOT NULL,
            conversation_id VARCHAR(128) NOT NULL, status VARCHAR(12) NOT NULL,
            state_json LONGTEXT NOT NULL, expires_at DATETIME NOT NULL,
            INDEX idx_review_checkpoint_expiry (expires_at)
        ) CHARACTER SET utf8mb4""")


# ============保存暂停状态 ============
def offer(checkpoint_id: str, plan_id: str, conversation_id: str, state: dict) -> None:
    """保存 24 小时内可恢复的交互状态。"""
    ensure_review_checkpoints()
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM review_checkpoints WHERE expires_at <= UTC_TIMESTAMP() LIMIT 100")
        cur.execute("""INSERT INTO review_checkpoints VALUES
                    (%s,%s,%s,'paused',%s,UTC_TIMESTAMP() + INTERVAL 1 DAY)""",
                    (checkpoint_id, plan_id, conversation_id, json.dumps(state, ensure_ascii=False)))


# ============认领恢复请求 ============
def claim(checkpoint_id: str, plan_id: str, conversation_id: str) -> tuple[str, dict]:
    """锁定对应会话的状态，并返回已完成请求的原结果。"""
    ensure_review_checkpoints()
    with transaction() as conn, conn.cursor() as cur:
        cur.execute("""SELECT status,state_json FROM review_checkpoints
                       WHERE checkpoint_id=%s AND plan_id=%s AND conversation_id=%s
                       AND expires_at > UTC_TIMESTAMP() FOR UPDATE""",
                    (checkpoint_id, plan_id, conversation_id))
        row = cur.fetchone()
        if not row:
            raise PlanConflict("这个选项已失效，请重新提问")
        if row["status"] == "running":
            raise PlanConflict("这次选择正在处理，请稍后查看结果")
        if row["status"] == "paused":
            cur.execute("UPDATE review_checkpoints SET status='running' WHERE checkpoint_id=%s", (checkpoint_id,))
        return row["status"], json.loads(row["state_json"])


# ============释放或完成断点 ============
def finish(checkpoint_id: str, response: dict | None = None) -> None:
    """失败时释放选择，成功时保存可重放响应。"""
    with transaction() as conn, conn.cursor() as cur:
        if response is None:
            cur.execute("""UPDATE review_checkpoints SET status='paused'
                           WHERE checkpoint_id=%s AND status='running'""", (checkpoint_id,))
        else:
            cur.execute("""UPDATE review_checkpoints SET status='completed', state_json=%s
                           WHERE checkpoint_id=%s AND status='running'""",
                        (json.dumps(response, ensure_ascii=False, default=str), checkpoint_id))
