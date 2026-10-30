from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any

from service.config import (
    DB_CHARSET,
    DB_CONNECT_TIMEOUT_SECONDS,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    VALID_DB_CHARSETS,
)
from service.utils.jsonl_log import to_jsonable


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def database_error(exc: Exception) -> dict[str, Any]:
    return {
        "success": False,
        "message": (
            "Database error. Check MySQL connection settings and schema "
            f"permissions. Details: {type(exc).__name__}: {exc}"
        ),
    }


def check_database_on_startup() -> None:
    """Create the MySQL database and table before exposing MCP tools."""
    try:
        ensure_schema()
    except Exception as exc:
        raise RuntimeError(
            "Failed to initialize the MySQL schema during MCP server startup. "
            "Check database connection settings and schema permissions."
        ) from exc


@lru_cache(maxsize=1)
def _ensure_map_cache() -> None:
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """CREATE TABLE IF NOT EXISTS map_api_cache (
                    cache_key CHAR(64) PRIMARY KEY,
                    payload LONGTEXT NOT NULL,
                    expires_at DATETIME NOT NULL,
                    INDEX idx_map_cache_expiry (expires_at)
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"""
            )


def ensure_schema() -> None:
    charset = _validated_charset()
    database_name = _quote_identifier(DB_NAME)

    with connect(database=None) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS {database_name} "
                f"CHARACTER SET {charset} COLLATE {charset}_unicode_ci"
            )

    _ensure_map_cache()
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS travel_plans (
                    plan_id VARCHAR(191) PRIMARY KEY,
                    title VARCHAR(255) NULL,
                    destination VARCHAR(255) NOT NULL,
                    start_date DATE NOT NULL,
                    end_date DATE NOT NULL,
                    departure_city VARCHAR(255) NULL,
                    travelers INT NOT NULL,
                    budget DECIMAL(12, 2) NOT NULL,
                    currency VARCHAR(16) NOT NULL,
                    travel_style VARCHAR(64) NOT NULL,
                    interests LONGTEXT NOT NULL,
                    constraints_json LONGTEXT NOT NULL,
                    pace VARCHAR(64) NOT NULL,
                    notes TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    itinerary LONGTEXT NOT NULL,
                    routes LONGTEXT NULL,
                    recommendations LONGTEXT NOT NULL,
                    budget_items LONGTEXT NOT NULL,
                    planning LONGTEXT NULL
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id VARCHAR(191) PRIMARY KEY,
                    plan_id VARCHAR(191) NULL,
                    title VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL,
                    INDEX idx_conversations_plan_updated (plan_id, updated_at),
                    CONSTRAINT fk_conversations_plan
                        FOREIGN KEY (plan_id) REFERENCES travel_plans(plan_id)
                        ON DELETE SET NULL
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    message_id VARCHAR(191) PRIMARY KEY,
                    conversation_id VARCHAR(191) NOT NULL,
                    role VARCHAR(32) NOT NULL,
                    content LONGTEXT NOT NULL,
                    tool_name VARCHAR(255) NULL,
                    metadata_json LONGTEXT NULL,
                    sequence_no BIGINT NOT NULL,
                    created_at DATETIME NOT NULL,
                    INDEX idx_messages_conversation_sequence (conversation_id, sequence_no),
                    CONSTRAINT fk_messages_conversation
                        FOREIGN KEY (conversation_id) REFERENCES conversations(conversation_id)
                        ON DELETE CASCADE
                ) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci
                """
            )
            try:
                cursor.execute("ALTER TABLE travel_plans ADD COLUMN title VARCHAR(255) NULL")
            except Exception as exc:
                if "duplicate" not in str(exc).lower() and "1060" not in str(exc):
                    raise
            try:
                cursor.execute(
                    "ALTER TABLE travel_plans ADD COLUMN routes LONGTEXT NULL"
                )
            except Exception as exc:
                if "duplicate" not in str(exc).lower() and "1060" not in str(exc):
                    raise
            try:
                cursor.execute("ALTER TABLE travel_plans ADD COLUMN planning LONGTEXT NULL")
            except Exception as exc:
                if "duplicate" not in str(exc).lower() and "1060" not in str(exc):
                    raise
            cursor.execute(
                "UPDATE travel_plans SET title = CONCAT(destination, ' 行程') WHERE title IS NULL OR title = ''"
            )


def read_map_cache(cache_key: str, allow_expired: bool = False) -> Any | None:
    _ensure_map_cache()
    with connect() as conn:
        with conn.cursor() as cursor:
            expiry = "" if allow_expired else " AND expires_at>UTC_TIMESTAMP()"
            cursor.execute("SELECT payload FROM map_api_cache WHERE cache_key=%s" + expiry, (cache_key,))
            row = cursor.fetchone()
    return json.loads(row["payload"]) if row else None


def write_map_cache(cache_key: str, payload: Any, ttl_seconds: int) -> None:
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """INSERT INTO map_api_cache (cache_key,payload,expires_at)
                   VALUES (%s,%s,TIMESTAMPADD(SECOND,%s,UTC_TIMESTAMP()))
                   ON DUPLICATE KEY UPDATE payload=VALUES(payload),expires_at=VALUES(expires_at)""",
                (cache_key, json.dumps(payload, ensure_ascii=False), ttl_seconds),
            )


def save_plan(plan: dict[str, Any]) -> None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO travel_plans (
                    plan_id,
                    title,
                    destination,
                    start_date,
                    end_date,
                    departure_city,
                    travelers,
                    budget,
                    currency,
                    travel_style,
                    interests,
                    constraints_json,
                    pace,
                    notes,
                    created_at,
                    updated_at,
                    itinerary,
                    routes,
                    recommendations,
                    budget_items,
                    planning
                )
                VALUES (
                    %(plan_id)s,
                    %(title)s,
                    %(destination)s,
                    %(start_date)s,
                    %(end_date)s,
                    %(departure_city)s,
                    %(travelers)s,
                    %(budget)s,
                    %(currency)s,
                    %(travel_style)s,
                    %(interests)s,
                    %(constraints_json)s,
                    %(pace)s,
                    %(notes)s,
                    %(created_at)s,
                    %(updated_at)s,
                    %(itinerary)s,
                    %(routes)s,
                    %(recommendations)s,
                    %(budget_items)s,
                    %(planning)s
                )
                ON DUPLICATE KEY UPDATE
                    title = VALUES(title),
                    destination = VALUES(destination),
                    start_date = VALUES(start_date),
                    end_date = VALUES(end_date),
                    departure_city = VALUES(departure_city),
                    travelers = VALUES(travelers),
                    budget = VALUES(budget),
                    currency = VALUES(currency),
                    travel_style = VALUES(travel_style),
                    interests = VALUES(interests),
                    constraints_json = VALUES(constraints_json),
                    pace = VALUES(pace),
                    notes = VALUES(notes),
                    updated_at = VALUES(updated_at),
                    itinerary = VALUES(itinerary),
                    routes = VALUES(routes),
                    recommendations = VALUES(recommendations),
                    budget_items = VALUES(budget_items),
                    planning = VALUES(planning)
                """,
                {
                    "plan_id": plan["plan_id"],
                    "title": plan.get("title"),
                    "destination": plan["destination"],
                    "start_date": plan["start_date"],
                    "end_date": plan["end_date"],
                    "departure_city": plan.get("departure_city"),
                    "travelers": plan["travelers"],
                    "budget": plan["budget"],
                    "currency": plan["currency"],
                    "travel_style": plan["travel_style"],
                    "interests": _json_dumps(plan["interests"]),
                    "constraints_json": _json_dumps(plan["constraints"]),
                    "pace": plan["pace"],
                    "notes": plan["notes"],
                    "created_at": plan["created_at"],
                    "updated_at": plan["updated_at"],
                    "itinerary": _json_dumps(plan["itinerary"]),
                    "routes": _json_dumps(plan.get("routes", [])),
                    "recommendations": _json_dumps(plan["recommendations"]),
                    "budget_items": _json_dumps(plan["budget_items"]),
                    "planning": _json_dumps(plan.get("planning", {})),
                },
            )


def get_plan(plan_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        ensure_schema()
        with connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM travel_plans WHERE plan_id = %s",
                    (plan_id,),
                )
                row = cursor.fetchone()
    except Exception as exc:
        return None, database_error(exc)

    if row is None:
        return None, {
            "success": False,
            "message": "Travel plan not found.",
        }

    return row_to_plan(row), None


def plan_exists(plan_id: str) -> bool:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM travel_plans WHERE plan_id = %s",
                (plan_id,),
            )
            return cursor.fetchone() is not None


def list_plan_summaries() -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        ensure_schema()
        with connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        plan_id,
                        title,
                        destination,
                        start_date,
                        end_date,
                        travelers,
                        budget,
                        currency,
                        updated_at
                    FROM travel_plans
                    ORDER BY updated_at DESC
                    """
                )
                rows = cursor.fetchall()
    except Exception as exc:
        return None, database_error(exc)

    plans = [
        {
            "plan_id": row["plan_id"],
            "title": row.get("title") or f"{row['destination']} 行程",
            "destination": row["destination"],
            "dates": (
                f"{normalize_scalar(row['start_date'])} "
                f"to {normalize_scalar(row['end_date'])}"
            ),
            "travelers": row["travelers"],
            "budget": normalize_scalar(row["budget"]),
            "currency": row["currency"],
            "updated_at": normalize_scalar(row["updated_at"]),
        }
        for row in rows
    ]

    return plans, None


def delete_plan(plan_id: str) -> tuple[int | None, dict[str, Any] | None]:
    try:
        ensure_schema()
        with connect() as conn:
            with conn.cursor() as cursor:
                deleted = cursor.execute(
                    "DELETE FROM travel_plans WHERE plan_id = %s",
                    (plan_id,),
                )
    except Exception as exc:
        return None, database_error(exc)

    return deleted, None


def connect(database: str | None = DB_NAME):
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: pymysql. Install project dependencies with "
            "`uv sync` or `pip install pymysql`."
        ) from exc

    return pymysql.connect(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=database,
        charset=_validated_charset(),
        cursorclass=DictCursor,
        autocommit=True,
        connect_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        read_timeout=DB_CONNECT_TIMEOUT_SECONDS,
        write_timeout=DB_CONNECT_TIMEOUT_SECONDS,
    )


def row_to_plan(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "plan_id": row["plan_id"],
        "title": row.get("title") or f"{row['destination']} 行程",
        "destination": row["destination"],
        "start_date": normalize_scalar(row["start_date"]),
        "end_date": normalize_scalar(row["end_date"]),
        "departure_city": row["departure_city"],
        "travelers": row["travelers"],
        "budget": normalize_scalar(row["budget"]),
        "currency": row["currency"],
        "travel_style": row["travel_style"],
        "interests": _json_loads(row["interests"], []),
        "constraints": _json_loads(row["constraints_json"], []),
        "pace": row["pace"],
        "notes": row["notes"],
        "created_at": normalize_scalar(row["created_at"]),
        "updated_at": normalize_scalar(row["updated_at"]),
        "itinerary": _json_loads(row["itinerary"], []),
        "routes": _json_loads(row.get("routes"), []),
        "recommendations": _json_loads(row["recommendations"], []),
        "budget_items": _json_loads(row["budget_items"], []),
        "planning": _json_loads(row.get("planning"), {}),
    }


def save_conversation(conversation: dict[str, Any]) -> None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO conversations (conversation_id, plan_id, title, created_at, updated_at)
                VALUES (%(conversation_id)s, %(plan_id)s, %(title)s, %(created_at)s, %(updated_at)s)
                ON DUPLICATE KEY UPDATE
                    plan_id = VALUES(plan_id), title = VALUES(title), updated_at = VALUES(updated_at)
                """,
                conversation,
            )


def get_conversation(conversation_id: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        ensure_schema()
        with connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT * FROM conversations WHERE conversation_id = %s", (conversation_id,))
                row = cursor.fetchone()
    except Exception as exc:
        return None, database_error(exc)
    if row is None:
        return None, {"success": False, "message": "Conversation not found."}
    return {key: normalize_scalar(value) for key, value in row.items()}, None


def list_conversations(plan_id: str | None = None) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        ensure_schema()
        with connect() as conn:
            with conn.cursor() as cursor:
                if plan_id:
                    cursor.execute("SELECT * FROM conversations WHERE plan_id = %s ORDER BY updated_at DESC", (plan_id,))
                else:
                    cursor.execute("SELECT * FROM conversations ORDER BY updated_at DESC")
                rows = cursor.fetchall()
    except Exception as exc:
        return None, database_error(exc)
    return [{key: normalize_scalar(value) for key, value in row.items()} for row in rows], None


def save_message(message: dict[str, Any]) -> None:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO messages
                    (message_id, conversation_id, role, content, tool_name, metadata_json, sequence_no, created_at)
                VALUES
                    (%(message_id)s, %(conversation_id)s, %(role)s, %(content)s, %(tool_name)s,
                     %(metadata_json)s, %(sequence_no)s, %(created_at)s)
                """,
                {
                    "message_id": message["message_id"],
                    "conversation_id": message["conversation_id"],
                    "role": message["role"],
                    "content": message["content"],
                    "tool_name": message.get("tool_name"),
                    "metadata_json": _json_dumps(message.get("metadata", {})),
                    "sequence_no": message["sequence_no"],
                    "created_at": message["created_at"],
                },
            )
            cursor.execute(
                "UPDATE conversations SET updated_at = %s WHERE conversation_id = %s",
                (message["created_at"], message["conversation_id"]),
            )


def list_messages(conversation_id: str) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    try:
        ensure_schema()
        with connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT * FROM messages WHERE conversation_id = %s ORDER BY sequence_no ASC",
                    (conversation_id,),
                )
                rows = cursor.fetchall()
    except Exception as exc:
        return None, database_error(exc)
    result = []
    for row in rows:
        result.append({
            "message_id": row["message_id"],
            "conversation_id": row["conversation_id"],
            "role": row["role"],
            "content": row["content"],
            "tool_name": row["tool_name"],
            "metadata": _json_loads(row.get("metadata_json"), {}),
            "sequence_no": row["sequence_no"],
            "created_at": normalize_scalar(row["created_at"]),
        })
    return result, None


def next_message_sequence(conversation_id: str) -> int:
    ensure_schema()
    with connect() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(MAX(sequence_no), 0) + 1 AS next_no FROM messages WHERE conversation_id = %s", (conversation_id,))
            return int(cursor.fetchone()["next_no"])


def normalize_scalar(value: Any) -> Any:
    if isinstance(value, Decimal):
        as_float = float(value)
        return int(as_float) if as_float.is_integer() else as_float
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _json_dumps(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False)


def _json_loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    if isinstance(value, (list, dict)):
        return value
    return json.loads(value)


def _quote_identifier(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(
            "TRAVEL_DB_NAME may only contain letters, numbers, and underscores."
        )
    return f"`{value}`"


def _validated_charset() -> str:
    if DB_CHARSET not in VALID_DB_CHARSETS:
        raise ValueError("TRAVEL_DB_CHARSET must be either utf8 or utf8mb4.")
    return DB_CHARSET

