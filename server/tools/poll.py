"""poll_jobs 도구: DB에서 대기 중인 작업 목록을 조회하고 레지스트리를 갱신합니다."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

_TARGET_JOB_FILE = Path(__file__).resolve().parent.parent.parent / "runtime" / "target_job.json"


def _pop_target_job() -> dict | None:
    """챗봇 재실행 요청 대상 job을 읽고 파일을 즉시 삭제합니다 (1회성)."""
    if not _TARGET_JOB_FILE.exists():
        return None
    try:
        data = json.loads(_TARGET_JOB_FILE.read_text(encoding="utf-8"))
        _TARGET_JOB_FILE.unlink(missing_ok=True)
        return data
    except Exception:
        return None

from langchain_core.tools import tool

from server.tools.context import (
    callbacks,
    formatting_registry,
    mig_registry,
    sql_registry,
    tuning_registry,
)


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


JOB_BATCH_SIZE = _env_int("SUPERVISOR_JOB_BATCH_SIZE", 20)
MIGRATION_JOB_BATCH_SIZE = _env_int("MIGRATION_JOB_BATCH_SIZE", 1)
SQL_CONVERSION_JOB_BATCH_SIZE = _env_int("SQL_CONVERSION_JOB_BATCH_SIZE", 1)
SQL_TUNING_JOB_BATCH_SIZE = _env_int("SQL_TUNING_JOB_BATCH_SIZE", 1)
SQL_FORMATTING_JOB_BATCH_SIZE = _env_int("SQL_FORMATTING_JOB_BATCH_SIZE", 1)


def _agent_flags() -> tuple[bool, bool, bool, bool]:
    """현재 .env 설정에서 에이전트별 실행 여부를 반환합니다.
    반환: (run_mig, run_sql, run_tuning, run_formatting)
    모두 false이면 전체 실행, 하나라도 true이면 선택된 것만 실행합니다."""
    mig_only  = os.getenv("DB_MIGRATION_ONLY",   "false").lower() == "true"
    sql_only  = os.getenv("SQL_CONVERSION_ONLY",  "false").lower() == "true"
    tune_only = os.getenv("SQL_TUNING_ONLY",      "false").lower() == "true"
    fmt_only  = os.getenv("SQL_FORMATTING_ONLY",  "false").lower() == "true"

    has_selection = mig_only or sql_only or tune_only or fmt_only
    if not has_selection:
        return True, True, True, True
    return mig_only, sql_only, tune_only, fmt_only


def priority_gate_jobs(
    mig_jobs: list,
    sql_jobs: list,
    tuning_jobs: list,
    formatting_jobs: list,
) -> tuple[list, list, list, list]:
    """Keep only the highest-priority non-empty job type for a normal cycle."""
    if mig_jobs:
        return mig_jobs, [], [], []
    if sql_jobs:
        return [], sql_jobs, [], []
    if tuning_jobs:
        return [], [], tuning_jobs, []
    if formatting_jobs:
        return [], [], [], formatting_jobs
    return [], [], [], []


def build_poll_jobs_tool(
    get_migration_jobs: Callable,
    get_sql_jobs: Callable,
    get_tuning_jobs: Callable,
    get_formatting_jobs: Callable,
) -> Callable:
    """poll_jobs 도구를 클로저로 생성합니다. Supervisor 초기화 시 한 번만 호출합니다."""

    @tool
    def poll_jobs() -> str:
        """DB에서 대기 중인 작업 목록을 조회하고 현재 사이클의 처리 대상을 등록합니다.
        사이클 시작 시 반드시 먼저 호출해야 합니다.
        반환값: migration_jobs, sql_jobs, tuning_jobs, formatting_jobs 목록과 summary를 담은 JSON 문자열."""
        logger = callbacks.get("logger")
        run_mig, run_sql, run_tuning, run_fmt = _agent_flags()

        # 챗봇 재실행 요청이 있으면 해당 job만 처리 (1회성)
        target = _pop_target_job()

        mig_jobs, sql_jobs, tuning_jobs, formatting_jobs = [], [], [], []
        try:
            if run_mig:
                mig_jobs = get_migration_jobs()
        except Exception as exc:
            if logger:
                logger.error(f"[poll_jobs] DataMigration 조회 오류: {exc}")
        try:
            if run_sql:
                sql_jobs = get_sql_jobs()
            if run_tuning:
                tuning_jobs = get_tuning_jobs()
            if run_fmt:
                formatting_jobs = get_formatting_jobs()
        except Exception as exc:
            if logger:
                logger.error(f"[poll_jobs] SQL/Tuning/Formatting 조회 오류: {exc}")

        # target_job 필터: 요청된 단일 job만 이번 사이클에 등록
        if target:
            t = target.get("type", "")
            if t == "mig":
                mid = int(target.get("map_id", -1))
                mig_jobs      = [j for j in mig_jobs if j.map_id == mid]
                sql_jobs      = []
                tuning_jobs   = []
                formatting_jobs = []
            elif t == "sql_conv":
                sid = str(target.get("sql_id", ""))
                space_nm = target.get("space_nm")
                sql_jobs      = [
                    j for j in sql_jobs
                    if str(j.sql_id) == sid
                    and (not space_nm or str(getattr(j, "space_nm", "")) == str(space_nm))
                ]
                mig_jobs      = []
                tuning_jobs   = []
                formatting_jobs = []
            elif t == "sql_tune":
                sid = str(target.get("sql_id", ""))
                space_nm = target.get("space_nm")
                tuning_jobs   = [
                    j for j in tuning_jobs
                    if str(j.sql_id) == sid
                    and (not space_nm or str(getattr(j, "space_nm", "")) == str(space_nm))
                ]
                mig_jobs      = []
                sql_jobs      = []
                formatting_jobs = []
            if logger:
                logger.info(f"[poll_jobs] 챗봇 재실행 요청 → type={t}, target={target}")

        if not target:
            mig_jobs, sql_jobs, tuning_jobs, formatting_jobs = priority_gate_jobs(
                mig_jobs, sql_jobs, tuning_jobs, formatting_jobs
            )

        mig_registry.clear()
        sql_registry.clear()
        tuning_registry.clear()
        formatting_registry.clear()

        for job in mig_jobs[:MIGRATION_JOB_BATCH_SIZE]:
            mig_registry[job.map_id] = job
        for job in sql_jobs[:SQL_CONVERSION_JOB_BATCH_SIZE]:
            sql_registry[str(job.row_id)] = job
        for job in tuning_jobs[:SQL_TUNING_JOB_BATCH_SIZE]:
            tuning_registry[str(job.row_id)] = job
        for job in formatting_jobs[:SQL_FORMATTING_JOB_BATCH_SIZE]:
            formatting_registry[str(job.row_id)] = job

        result = {
            "migration_jobs": [
                {
                    "map_id": job.map_id,
                    "map_type": job.map_type,
                    "fr_table": job.fr_table,
                    "to_table": job.to_table,
                    "priority": job.priority,
                    "prior_map_id": getattr(job, "prior_map_id", None),
                    "retry_count": getattr(job, "retry_count", 0) or 0,
                    "status": job.status,
                    "batch_cnt": getattr(job, "batch_cnt", 0) or 0,
                }
                for job in mig_registry.values()
            ],
            "sql_jobs": [
                {
                    "row_id": job.row_id,
                    "status": job.status,
                    "tag_kind": job.tag_kind,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                }
                for job in sql_registry.values()
            ],
            "tuning_jobs": [
                {
                    "row_id": job.row_id,
                    "tuned_test": job.tuned_test,
                }
                for job in tuning_registry.values()
            ],
            "formatting_jobs": [
                {
                    "row_id": job.row_id,
                    "space_nm": job.space_nm,
                    "sql_id": job.sql_id,
                }
                for job in formatting_registry.values()
            ],
            "summary": {
                "migration_total": len(mig_jobs),
                "migration_in_batch": len(mig_registry),
                "sql_total": len(sql_jobs),
                "sql_in_batch": len(sql_registry),
                "tuning_total": len(tuning_jobs),
                "tuning_in_batch": len(tuning_registry),
                "formatting_total": len(formatting_jobs),
                "formatting_in_batch": len(formatting_registry),
                "sql_by_status": _count_by_status(
                    [j.status for j in sql_registry.values()]
                ),
            },
        }

        if logger:
            s = result["summary"]
            active = [
                n for n, flag in [
                    ("Mig", run_mig), ("Sql", run_sql),
                    ("Tuning", run_tuning), ("Fmt", run_fmt),
                ] if flag
            ]
            mode_str = "+".join(active) if len(active) < 4 else "전체"
            if s["migration_total"] or s["sql_total"] or s["tuning_total"] or s["formatting_total"]:
                logger.info(
                    f"[poll_jobs][{mode_str}] "
                    f"Mig={s['migration_in_batch']}/{s['migration_total']}, "
                    f"Sql={s['sql_in_batch']}/{s['sql_total']}, "
                    f"Tuning={s['tuning_in_batch']}/{s['tuning_total']}, "
                    f"Formatting={s['formatting_in_batch']}/{s['formatting_total']}"
                )
            else:
                logger.info(f"[poll_jobs][{mode_str}] 대기 중인 작업 없음")

        return json.dumps(result, ensure_ascii=False, default=str)

    return poll_jobs


def _count_by_status(statuses: list) -> dict:
    counts: dict = {}
    for s in statuses:
        key = (s or "NULL").strip().upper()
        counts[key] = counts.get(key, 0) + 1
    return counts
