"""Append-only SQL pipeline log repository."""

from __future__ import annotations

from typing import Any

from server.core.logger import logger
from server.services.sql.db_runtime import get_connection, qualify_table_name


SQL_LOG_TABLE = "NEXT_SQL_LOG"


def insert_sql_log(
    *,
    space_nm: str | None,
    sql_id: str | None,
    sql_info_rowid: str | None,
    sql_kind: str,
    sql_content: str | None,
    status: str,
    prompt_name: str | None = None,
    model_name: str | None = None,
    batch_no: int | None = None,
    cycle_no: int | None = None,
    elapsed_seconds: float | None = None,
    attempt_no: int | None = None,
    stage_name: str | None = None,
    error_message: str | None = None,
) -> None:
    """Insert SQL generation/execution history.

    Logging must not break the migration pipeline, so insert failures are
    downgraded to warnings. This lets deployments add NEXT_SQL_LOG after code
    rollout without blocking existing jobs.
    """
    metric_context = _current_metric_context()
    if batch_no is None:
        batch_no = metric_context.get("batch_no")
    if cycle_no is None:
        cycle_no = metric_context.get("cycle_no")

    table = qualify_table_name(SQL_LOG_TABLE)
    query = f"""
        INSERT INTO {table} (
            CREATED_AT, SPACE_NM, SQL_ID, SQL_INFO_ROWID, SQL_KIND, SQL_CONTENT,
            STATUS, PROMPT_NAME, MODEL_NAME, BATCH_NO, CYCLE_NO, ELAPSED_SECONDS,
            ATTEMPT_NO, STAGE_NAME, ERROR_MESSAGE
        ) VALUES (
            CURRENT_TIMESTAMP, :space_nm, :sql_id, :sql_info_rowid, :sql_kind, :sql_content,
            :status, :prompt_name, :model_name, :batch_no, :cycle_no, :elapsed_seconds,
            :attempt_no, :stage_name, :error_message
        )
    """
    payload: dict[str, Any] = {
        "space_nm": _fit_text(space_nm, 200),
        "sql_id": _fit_text(sql_id, 200),
        "sql_info_rowid": _fit_text(sql_info_rowid, 30),
        "sql_kind": _fit_text(sql_kind, 30),
        "sql_content": _to_text(sql_content),
        "status": _fit_text(status, 20),
        "prompt_name": _fit_text(prompt_name, 120),
        "model_name": _fit_text(model_name, 120),
        "batch_no": batch_no,
        "cycle_no": cycle_no,
        "elapsed_seconds": round(float(elapsed_seconds), 3) if elapsed_seconds is not None else None,
        "attempt_no": attempt_no,
        "stage_name": _fit_text(stage_name, 100),
        "error_message": _to_text(error_message),
    }
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, payload)
            conn.commit()
    except Exception as exc:
        logger.warning(f"[SqlLog] Failed to insert NEXT_SQL_LOG row: {exc}")


def _to_text(value: str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _fit_text(value: str | None, max_len: int) -> str | None:
    text = _to_text(value)
    if text is None:
        return None
    if max_len <= 0:
        return ""
    encoded = text.encode("utf-8", errors="ignore")
    if len(encoded) <= max_len:
        return text
    return encoded[:max_len].decode("utf-8", errors="ignore")


def _current_metric_context() -> dict[str, int | None]:
    try:
        from server.tools.context import get_current_metric_context

        return get_current_metric_context()
    except Exception:
        return {"batch_no": None, "cycle_no": None}
