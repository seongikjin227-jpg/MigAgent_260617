"""Supervisor/agent cycle metrics repository."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from server.core.logger import logger
from server.services.sql.db_runtime import get_connection, qualify_table_name


METRICS_TABLE = "AG_AGENT_RUN_METRICS"


def insert_agent_run_metrics(rows: list[dict[str, Any]]) -> None:
    """Insert aggregated agent runtime metrics.

    Metrics are observability data only, so a missing table or insert failure
    must not break the existing agent pipeline.
    """
    if not rows:
        return

    table = qualify_table_name(METRICS_TABLE)
    query = f"""
        INSERT INTO {table} (
            BATCH_NO, CYCLE_NO, AGENT_NAME, JOB_COUNT, SUCCESS_COUNT, FAIL_COUNT, SKIP_COUNT,
            STARTED_AT, FINISHED_AT, ELAPSED_SECONDS
        )
        VALUES (
            :batch_no, :cycle_no, :agent_name, :job_count, :success_count, :fail_count, :skip_count,
            :started_at, :finished_at, :elapsed_seconds
        )
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(query, rows)
            conn.commit()
    except Exception as exc:
        logger.warning(f"[Metrics] Failed to insert agent run metrics: {exc}")


def build_metric_row(
    *,
    batch_no: int,
    cycle_no: int,
    agent_name: str,
    job_count: int,
    success_count: int,
    fail_count: int,
    skip_count: int,
    started_at: datetime,
    finished_at: datetime,
    elapsed_seconds: float,
) -> dict[str, Any]:
    return {
        "batch_no": batch_no,
        "cycle_no": cycle_no,
        "agent_name": agent_name,
        "job_count": job_count,
        "success_count": success_count,
        "fail_count": fail_count,
        "skip_count": skip_count,
        "started_at": started_at,
        "finished_at": finished_at,
        "elapsed_seconds": round(elapsed_seconds, 3),
    }
