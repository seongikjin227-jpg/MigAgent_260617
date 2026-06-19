import os
import re
import json
import html
from urllib.parse import urlencode
import streamlit as st
from collections import Counter
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
from openai import OpenAI
from utils.db import (
    get_mig_status_summary,
    get_sql_status_summary,
    get_sql_length_success_summary,
    get_tuning_status_summary,
    get_formatting_summary,
    get_recent_fails,
    get_mig_jobs,
    get_mig_logs,
    reset_mig_job_for_rerun,
    reset_sql_conversion_job,
    reset_sql_tuning_job,
    find_sql_job_spaces,
    get_sql_failure_log,
    get_mig_failure_analysis_rows,
    get_sql_conversion_failure_analysis_rows,
    get_sql_tuning_failure_analysis_rows,
    poll_mig_job_result,
    poll_sql_job_result,
)
from utils.env_manager import read_env
from utils.agent_control import get_status as _agent_status, start as _start_supervisor, stop as _stop_supervisor

_ROOT      = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")
_CHATS_DIR = _ROOT / "runtime" / "chats"
_WAKE_FILE = _ROOT / "runtime" / "agent.wake"
_FAIL_ANALYSIS_HINTS_FILE = _ROOT / "app" / "config" / "fail_analysis_hints.json"


def _is_supervisor_mode() -> bool:
    env = read_env()
    return env.get("SUPERVISOR_MODE", "false").lower() == "true"


def _wake_supervisor() -> None:
    """실행 중인 Supervisor의 대기를 즉시 중단시켜 새 사이클을 시작합니다."""
    try:
        _WAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _WAKE_FILE.touch()
    except Exception:
        pass


_TARGET_JOB_FILE = _ROOT / "runtime" / "target_job.json"
_COMMAND_FILE = _ROOT / "runtime" / "chat_command.json"


def _write_target_job(data: dict) -> None:
    """Supervisor의 다음 사이클에서 이 job만 처리하도록 지시합니다."""
    try:
        _TARGET_JOB_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TARGET_JOB_FILE.write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
    except Exception:
        pass

def _write_one_shot_command(command: str) -> None:
    """Ask the background Supervisor to run exactly one requested cycle."""
    try:
        _COMMAND_FILE.parent.mkdir(parents=True, exist_ok=True)
        _COMMAND_FILE.write_text(
            json.dumps({"command": command, "one_shot": True}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass


def _target_qualifier(sql_id: str, space_nm: str | None = None) -> str:
    if space_nm:
        return f"SQL_ID={sql_id}, SPACE_NM={space_nm}"
    return f"SQL_ID={sql_id}"


def _ensure_supervisor_running() -> None:
    """Supervisor가 꺼져 있으면 자동으로 시작하고, 실행 중이면 즉시 깨웁니다."""
    if not _agent_status()["running"]:
        _start_supervisor()
    else:
        _wake_supervisor()


# ── Supervisor Function-Calling 도구 정의 ─────────────────────────────────────
def _stop_supervisor_for_one_shot() -> None:
    """Interrupt the current Supervisor cycle before a chat-requested one-shot run."""
    if _agent_status()["running"]:
        _stop_supervisor()


def _start_supervisor_for_one_shot() -> None:
    _start_supervisor()


def _num(value, default: int = 0) -> int:
    try:
        return int(float(str(value or "").strip()))
    except Exception:
        return default


def _length_bucket(row: dict) -> str:
    length = _num(row.get("EFFECTIVE_SQL_LEN") or row.get("SQL_LENGTH") or row.get("FR_SQL_LEN"))
    if length <= 0:
        return "UNKNOWN"
    if length <= 5000:
        return "SHORT_<=5000"
    if length <= 20000:
        return "MEDIUM_5001_20000"
    return "LONG_>20000"


def _classify_sql_fail_log(log_text: str) -> str:
    text = (log_text or "").upper()
    patterns = [
        ("MISSING_ORDER_BY_EXPRESSION", ["MISSING ORDER BY EXPRESSION", "ORDER BY EXPRESSION", "ROW_NUMBER"]),
        ("BIND_VARIABLE", ["BIND", "ORA-01008", "ORA-01036"]),
        ("UNEXPECTED_END_OF_SQL_COMMAND", ["UNEXPECTED END OF SQL COMMAND"]),
        ("INVALID_IDENTIFIER", ["INVALID IDENTIFIER", "ORA-00904"]),
        ("INVALID_NUMBER", ["INVALID NUMBER", "ORA-01722"]),
        ("SYNTAX_OR_PARSE", ["ORA-00900", "ORA-00905", "ORA-00907", "ORA-00923", "ORA-00933", "PARSE"]),
        ("OBJECT_OR_COLUMN", ["ORA-00942", "ORA-01403", "INVALID IDENTIFIER", "TABLE OR VIEW"]),
        ("DATA_TYPE", ["ORA-01722", "ORA-018", "ORA-00932", "INCONSISTENT DATATYPES", "INVALID NUMBER"]),
        ("VALIDATION_COUNT_MISMATCH", ["VALIDATION_FAIL", "COUNT", "BASELINE_COUNT", "TUNED_COUNT", "FROM_COUNT", "TO_COUNT"]),
        ("TIMEOUT_OR_PERFORMANCE", ["TIMEOUT", "ORA-01013", "ELAPSED", "PERFORMANCE"]),
        ("LLM_OR_JSON", ["JSON", "LLM", "MODEL", "PARSE_LLM", "RESPONSE"]),
        ("EMPTY_OR_MISSING_SQL", ["EMPTY", "NULL SQL", "NO SQL", "MISSING"]),
    ]
    for label, needles in patterns:
        if any(needle in text for needle in needles):
            return label
    if text.strip():
        return "OTHER_ERROR"
    return "NO_LOG"

def _classify_fail_stage(row: dict, agent: str) -> str:
    status_key = "TUNED_TEST" if "TUNING" in (agent or "").upper() else "STATUS"
    explicit_status = str(row.get(status_key) or "").strip().upper()
    if explicit_status in {"FAIL-TOBE", "FAIL-TUNED", "FAIL-BIND", "FAIL-TEST"}:
        return explicit_status

    text = " ".join(
        str(row.get(key) or "")
        for key in ("LOG", "STATUS", "TUNED_TEST")
    ).upper()
    agent_key = (agent or "").upper()

    if "TUNING" in agent_key:
        patterns = [
            ("TUNING_TEST", ["TUNED_TEST", "TEST_VALIDATION", "VALIDATION_FAIL", "BASELINE_COUNT", "TUNED_COUNT"]),
            ("TUNING_SQL_GENERATION", ["TUNED_SQL", "GENERATE_TUNED_SQL", "TUNING_ERROR"]),
            ("TUNING_LLM_RESPONSE", ["LLM", "MODEL", "JSON", "RESPONSE", "PARSE"]),
            ("TUNING_BIND_OR_PARAM", ["BIND", "ORA-01008", "ORA-01036"]),
            ("TUNING_DB_EXECUTION", ["ORA-", "SQL EXEC", "DATABASE", "QUERY"]),
        ]
    else:
        patterns = [
            ("TEST_SQL_VALIDATION", ["TEST_SQL", "TEST_VALIDATION", "VALIDATION_FAIL", "BASELINE_COUNT", "FROM_COUNT", "TO_COUNT"]),
            ("BIND_SQL_GENERATION", ["BIND_SQL", "BIND SET", "BIND_SET", "NO_BIND", "ORA-01008", "ORA-01036"]),
            ("TOBE_SQL_GENERATION", ["TOBE_SQL", "TO_SQL", "GENERATE_TOBE", "CONVERSION"]),
            ("LLM_RESPONSE_PARSE", ["LLM", "MODEL", "JSON", "RESPONSE", "PARSE_LLM"]),
            ("DB_EXECUTION", ["ORA-", "SQL EXEC", "DATABASE", "QUERY"]),
        ]

    for label, needles in patterns:
        if any(needle in text for needle in needles):
            return label
    if text.strip():
        return "OTHER_STAGE"
    return "NO_LOG"


def _load_fail_analysis_hints(stage: str) -> list[dict]:
    try:
        if stage == "DB_MIGRATION":
            return []
        data = json.loads(_FAIL_ANALYSIS_HINTS_FILE.read_text(encoding="utf-8"))
        key = "sql_tuning_hints" if stage == "SQL_TUNING" else "sql_conversion_hints"
        hints = data.get(key, [])
        return hints if isinstance(hints, list) else []
    except Exception:
        return []


def _top_counter(counter: Counter, limit: int = 12) -> list[dict]:
    return [{"name": str(name), "count": int(count)} for name, count in counter.most_common(limit)]


def _summarize_sql_fail_rows(rows: list[dict], stage: str) -> dict:
    map_kind_counts: Counter = Counter()
    length_counts: Counter = Counter()
    log_type_counts: Counter = Counter()
    fail_stage_counts: Counter = Counter()
    status_counts: Counter = Counter()
    samples = []

    for row in rows:
        map_kind = (row.get("MAP_KIND") or row.get("MAP_TYPE") or row.get("TAG_KIND") or "UNKNOWN").strip() or "UNKNOWN"
        map_kind_counts[map_kind] += 1
        length_counts[_length_bucket(row)] += 1
        log_type = _classify_sql_fail_log(str(row.get("LOG") or ""))
        log_type_counts[log_type] += 1
        fail_stage = _classify_fail_stage(row, stage)
        fail_stage_counts[fail_stage] += 1
        status_counts[str(row.get("STATUS") or "NULL").strip() or "NULL"] += 1
        if len(samples) < 30:
            samples.append({
                "sql_id": row.get("SQL_ID"),
                "space_nm": row.get("SPACE_NM"),
                "map_kind": map_kind,
                "length_bucket": _length_bucket(row),
                "status": row.get("STATUS"),
                "tuned_test": row.get("TUNED_TEST"),
                "fail_stage": fail_stage,
                "log_type": log_type,
                "log": str(row.get("LOG") or "")[:800],
                "upd_ts": row.get("UPD_TS"),
            })

    return {
        "stage": stage,
        "total_fail_rows": len(rows),
        "map_kind_counts": _top_counter(map_kind_counts),
        "length_counts": _top_counter(length_counts),
        "status_counts": _top_counter(status_counts),
        "fail_stage_counts": _top_counter(fail_stage_counts),
        "log_type_counts": _top_counter(log_type_counts),
        "recent_samples": samples,
        "admin_fail_cause_hints": _load_fail_analysis_hints(stage),
        "analysis_instruction": (
            "First summarize statistics by map_kind and SQL length. Then count log categories. "
            "Use admin_fail_cause_hints as high-priority domain knowledge when log keywords match. "
            "Finally infer the most likely major causes in Korean, with caveats if logs are sparse."
        ),
    }


def _classify_mig_fail_log(log_text: str) -> str:
    text = (log_text or "").upper()
    patterns = [
        ("DEPENDENCY", ["DEPENDENCY", "PRIOR_MAP_ID", "SAME-TARGET", "DEP_CHECK"]),
        ("LLM_OR_JSON", ["LLM", "MODEL", "JSON", "RESPONSE", "PARSE"]),
        ("SQL_EXECUTION", ["ORA-", "SQL_EXEC", "EXEC", "DATABASE"]),
        ("VERIFY", ["VERIFY", "VALIDATION", "COUNT", "MISMATCH"]),
        ("EMPTY_OR_MISSING_SQL", ["EMPTY", "NULL SQL", "NO SQL", "MISSING"]),
        ("TIMEOUT_OR_ABORT", ["TIMEOUT", "ABORT", "INTERRUPT"]),
    ]
    for label, needles in patterns:
        if any(needle in text for needle in needles):
            return label
    if text.strip():
        return "OTHER_ERROR"
    return "NO_LOG"


def _summarize_mig_fail_rows(rows: list[dict], stage: str = "DB_MIGRATION") -> dict:
    map_type_counts: Counter = Counter()
    step_counts: Counter = Counter()
    log_type_counts: Counter = Counter()
    log_category_counts: Counter = Counter()
    target_counts: Counter = Counter()
    status_counts: Counter = Counter()
    samples = []

    for row in rows:
        map_type = str(row.get("MAP_TYPE") or "UNKNOWN").strip() or "UNKNOWN"
        step = str(row.get("STEP_NAME") or "NO_STEP").strip() or "NO_STEP"
        log_type = str(row.get("LOG_TYPE") or "NO_LOG_TYPE").strip() or "NO_LOG_TYPE"
        target = str(row.get("TO_TABLE") or "UNKNOWN").strip() or "UNKNOWN"
        log_category = _classify_mig_fail_log(str(row.get("LOG") or ""))
        map_type_counts[map_type] += 1
        step_counts[step] += 1
        log_type_counts[log_type] += 1
        log_category_counts[log_category] += 1
        target_counts[target] += 1
        status_counts[str(row.get("STATUS") or "NULL").strip() or "NULL"] += 1
        if len(samples) < 30:
            samples.append({
                "sql_id": row.get("MAP_ID"),
                "space_nm": row.get("TO_TABLE"),
                "map_kind": map_type,
                "length_bucket": f"retry={row.get('RETRY_COUNT') or 0}, elapsed={row.get('ELAPSED_SECONDS') or 0}s",
                "fail_stage": step,
                "map_id": row.get("MAP_ID"),
                "map_type": map_type,
                "fr_table": row.get("FR_TABLE"),
                "to_table": row.get("TO_TABLE"),
                "status": row.get("STATUS"),
                "step_name": step,
                "log_type": log_type,
                "log_category": log_category,
                "retry_count": row.get("RETRY_COUNT"),
                "elapsed_seconds": row.get("ELAPSED_SECONDS"),
                "upd_ts": row.get("UPD_TS") or row.get("LOG_TIME"),
                "log": str(row.get("LOG") or "")[:800],
            })

    return {
        "stage": stage,
        "total_fail_rows": len(rows),
        "map_kind_counts": _top_counter(map_type_counts),
        "length_counts": _top_counter(target_counts),
        "map_type_counts": _top_counter(map_type_counts),
        "target_counts": _top_counter(target_counts),
        "status_counts": _top_counter(status_counts),
        "fail_stage_counts": _top_counter(step_counts),
        "log_type_counts": _top_counter(log_type_counts),
        "log_category_counts": _top_counter(log_category_counts),
        "recent_samples": samples,
        "admin_fail_cause_hints": _load_fail_analysis_hints(stage),
        "analysis_instruction": (
            "Summarize DB Migration failures by MAP_TYPE, target table, STEP_NAME, and log category. "
            "Use the latest NEXT_MIG_LOG message for evidence. "
            "Infer likely causes in Korean and list concrete next checks."
        ),
    }


_SUPERVISOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_mig_failure_log",
            "description": (
                "특정 map_id의 Migration 작업 정보와 NEXT_MIG_LOG 실패 로그를 조회합니다. "
                "실패 원인 분석 요청 시 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "map_id": {"type": "integer", "description": "조회할 MAP_ID (숫자)"}
                },
                "required": ["map_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_sql_failure_log",
            "description": (
                "특정 sql_id의 SQL 작업 상태와 NEXT_SQL_INFO.LOG 실패 로그를 조회합니다. "
                "SQL 변환/튜닝 실패 원인 분석 요청 시 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id":   {"type": "string", "description": "조회할 SQL_ID"},
                    "space_nm": {"type": "string", "description": "네임스페이스 (선택, 같은 sql_id가 여러 개일 때 구분)"},
                },
                "required": ["sql_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_mig_failures",
            "description": (
                "최근 DB Migration FAIL 전체를 종합 분석합니다. "
                "NEXT_MIG_INFO.STATUS='FAIL'인 row와 최신 NEXT_MIG_LOG를 모아 MAP_TYPE, target table, step, log 유형별 건수와 주요 원인을 추정할 때 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "최근 FAIL 분석 대상 최대 건수. 기본값 200.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_sql_conversion_failures",
            "description": (
                "최근 SQL Conversion FAIL 전체를 종합 분석합니다. "
                "NEXT_SQL_INFO에서 STATUS가 FAIL 계열(FAIL/FAIL-TOBE/FAIL-BIND/FAIL-TEST)인 row를 모아 MAP_TYPE/TAG_KIND, SQL 길이 구간, "
                "LOG 에러 유형별 건수와 주요 원인 추정을 생성할 때 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "최근 FAIL 분석 대상 최대 건수. 기본값 200.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_sql_tuning_failures",
            "description": (
                "최근 SQL Tuning FAIL 전체를 종합 분석합니다. "
                "NEXT_SQL_INFO에서 TUNED_TEST가 FAIL 계열(FAIL/FAIL-TUNED/FAIL-BIND/FAIL-TEST)인 row를 모아 MAP_TYPE/TAG_KIND, SQL 길이 구간, "
                "LOG 에러 유형별 건수와 주요 원인 추정을 생성할 때 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "최근 FAIL 분석 대상 최대 건수. 기본값 200.",
                    }
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerun_migration",
            "description": (
                "Migration 작업을 즉시 재실행합니다. "
                "NEXT_MIG_INFO에서 USE_YN='Y', STATUS=NULL, MIG_SQL=NULL, RETRY_COUNT=0으로 초기화하고 "
                "Supervisor를 즉시 깨워 처리하게 합니다. "
                "사용자가 재실행을 요청할 때 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "map_id": {"type": "integer", "description": "재실행할 MAP_ID"}
                },
                "required": ["map_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerun_sql_conversion",
            "description": (
                "SQL 변환 작업을 즉시 재실행합니다. "
                "NEXT_SQL_INFO.STATUS를 'URGENT'로 설정하고 Supervisor를 즉시 깨웁니다. "
                "사용자가 SQL 변환 재실행을 요청할 때 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id":   {"type": "string", "description": "재실행할 SQL_ID"},
                    "space_nm": {"type": "string", "description": "네임스페이스 (선택)"},
                },
                "required": ["sql_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rerun_sql_tuning",
            "description": (
                "SQL 튜닝 작업을 즉시 재실행합니다. "
                "NEXT_SQL_INFO.TUNED_TEST를 'URGENT'로 설정하고 Supervisor를 즉시 깨웁니다. "
                "사용자가 SQL 튜닝 재실행을 요청할 때 사용하세요."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql_id":   {"type": "string", "description": "재실행할 SQL_ID"},
                    "space_nm": {"type": "string", "description": "네임스페이스 (선택)"},
                },
                "required": ["sql_id"],
            },
        },
    },
]


def _handle_supervisor_tool(name: str, args: dict) -> str:
    """Supervisor 도구 호출을 실행하고 결과를 JSON 문자열로 반환합니다."""
    try:
        if name == "query_mig_failure_log":
            map_id = int(args["map_id"])
            logs = get_mig_logs(map_id)
            all_jobs = {int(j["MAP_ID"]): j for j in get_mig_jobs()}
            job = all_jobs.get(map_id, {})
            return json.dumps({
                "map_id": map_id,
                "job": {
                    "fr_table":       job.get("FR_TABLE"),
                    "to_table":       job.get("TO_TABLE"),
                    "status":         job.get("STATUS"),
                    "use_yn":         job.get("USE_YN"),
                    "retry_count":    job.get("RETRY_COUNT"),
                    "elapsed_sec":    job.get("ELAPSED_SECONDS"),
                    "mig_sql_head":   str(job.get("MIG_SQL") or "")[:300],
                },
                "logs": [
                    {
                        "step":    lg.get("STEP_NAME"),
                        "level":   lg.get("LOG_LEVEL"),
                        "status":  lg.get("STATUS"),
                        "message": str(lg.get("MESSAGE") or "")[:400],
                    }
                    for lg in logs[-20:]
                ],
                "total_log_count": len(logs),
            }, ensure_ascii=False, default=str)

        if name == "query_sql_failure_log":
            sql_id   = args["sql_id"]
            space_nm = args.get("space_nm")
            rows = get_sql_failure_log(sql_id, space_nm)
            return json.dumps({
                "sql_id": sql_id, "space_nm": space_nm,
                "rows": [
                    {
                        "space_nm":   r.get("SPACE_NM"),
                        "status":     r.get("STATUS"),
                        "tuned_test": r.get("TUNED_TEST"),
                        "log":        str(r.get("LOG") or "")[:500],
                    }
                    for r in rows
                ],
            }, ensure_ascii=False, default=str)

        if name == "analyze_mig_failures":
            limit = int(args.get("limit") or 200)
            rows = get_mig_failure_analysis_rows(limit=limit)
            return json.dumps(
                _summarize_mig_fail_rows(rows, stage="DB_MIGRATION"),
                ensure_ascii=False,
                default=str,
            )

        if name == "analyze_sql_conversion_failures":
            limit = int(args.get("limit") or 200)
            rows = get_sql_conversion_failure_analysis_rows(limit=limit)
            return json.dumps(
                _summarize_sql_fail_rows(rows, stage="SQL_CONVERSION"),
                ensure_ascii=False,
                default=str,
            )

        if name == "analyze_sql_tuning_failures":
            limit = int(args.get("limit") or 200)
            rows = get_sql_tuning_failure_analysis_rows(limit=limit)
            return json.dumps(
                _summarize_sql_fail_rows(rows, stage="SQL_TUNING"),
                ensure_ascii=False,
                default=str,
            )

        if name == "rerun_migration":
            map_id = int(args["map_id"])
            _stop_supervisor_for_one_shot()
            ok = reset_mig_job_for_rerun(map_id)
            if not ok:
                return json.dumps({"success": False, "map_id": map_id,
                                   "reason": "DB에서 해당 MAP_ID를 찾을 수 없습니다."}, ensure_ascii=False)
            _write_target_job({"type": "mig", "map_id": map_id})
            _write_one_shot_command(
                f"One-shot request: run only Data Migration MAP_ID={map_id}. "
                "Call poll_jobs() first, then run_data_migration for that map_id only. "
                "Do not run SQL conversion, SQL tuning, or SQL formatting. "
                "After the requested job, call flush_cycle_metrics() and finish."
            )
            _start_supervisor_for_one_shot()
            result = poll_mig_job_result(map_id, timeout_sec=300)
            return json.dumps(result, ensure_ascii=False, default=str)

        if name == "rerun_sql_conversion":
            sql_id   = args["sql_id"]
            space_nm = args.get("space_nm")
            spaces = find_sql_job_spaces(sql_id)
            if not spaces:
                return json.dumps({"success": False, "sql_id": sql_id,
                                   "reason": "DB에서 해당 SQL_ID를 찾을 수 없습니다."}, ensure_ascii=False)
            if space_nm and str(space_nm) not in {str(s) for s in spaces}:
                return json.dumps({"success": False, "sql_id": sql_id, "space_nm": space_nm,
                                   "reason": "DB에서 해당 SQL_ID/SPACE_NM 조합을 찾을 수 없습니다.",
                                   "space_nm_candidates": spaces}, ensure_ascii=False)
            if not space_nm and len(spaces) > 1:
                return json.dumps({
                    "success": False,
                    "sql_id": sql_id,
                    "reason": "같은 SQL_ID가 여러 SPACE_NM에 존재합니다. SPACE_NM을 지정해야 합니다.",
                    "space_nm_candidates": spaces,
                }, ensure_ascii=False)
            _stop_supervisor_for_one_shot()
            cnt = reset_sql_conversion_job(sql_id, space_nm)
            if cnt == 0:
                return json.dumps({"success": False, "sql_id": sql_id,
                                   "reason": "DB에서 해당 SQL_ID를 찾을 수 없습니다."}, ensure_ascii=False)
            _write_target_job({"type": "sql_conv", "sql_id": sql_id, "space_nm": space_nm})
            _write_one_shot_command(
                f"One-shot request: run only SQL Conversion for {_target_qualifier(sql_id, space_nm)}. "
                "Call poll_jobs() first, then run_sql_conversion for the row_id returned in sql_jobs only. "
                "Do not run Data Migration, SQL tuning, or SQL formatting. "
                "After the requested job, call flush_cycle_metrics() and finish."
            )
            _start_supervisor_for_one_shot()
            result = poll_sql_job_result(sql_id, field="STATUS", space_nm=space_nm, timeout_sec=300)
            return json.dumps(result, ensure_ascii=False, default=str)

        if name == "rerun_sql_tuning":
            sql_id   = args["sql_id"]
            space_nm = args.get("space_nm")
            spaces = find_sql_job_spaces(sql_id)
            if not spaces:
                return json.dumps({"success": False, "sql_id": sql_id,
                                   "reason": "DB에서 해당 SQL_ID를 찾을 수 없습니다."}, ensure_ascii=False)
            if space_nm and str(space_nm) not in {str(s) for s in spaces}:
                return json.dumps({"success": False, "sql_id": sql_id, "space_nm": space_nm,
                                   "reason": "DB에서 해당 SQL_ID/SPACE_NM 조합을 찾을 수 없습니다.",
                                   "space_nm_candidates": spaces}, ensure_ascii=False)
            if not space_nm and len(spaces) > 1:
                return json.dumps({
                    "success": False,
                    "sql_id": sql_id,
                    "reason": "같은 SQL_ID가 여러 SPACE_NM에 존재합니다. SPACE_NM을 지정해야 합니다.",
                    "space_nm_candidates": spaces,
                }, ensure_ascii=False)
            _stop_supervisor_for_one_shot()
            cnt = reset_sql_tuning_job(sql_id, space_nm)
            if cnt == 0:
                return json.dumps({"success": False, "sql_id": sql_id,
                                   "reason": "DB에서 해당 SQL_ID를 찾을 수 없습니다."}, ensure_ascii=False)
            _write_target_job({"type": "sql_tune", "sql_id": sql_id, "space_nm": space_nm})
            _write_one_shot_command(
                f"One-shot request: run only SQL Tuning for {_target_qualifier(sql_id, space_nm)}. "
                "Call poll_jobs() first, then run_sql_tuning for the row_id returned in tuning_jobs only. "
                "Do not run Data Migration, SQL conversion, or SQL formatting. "
                "After the requested job, call flush_cycle_metrics() and finish."
            )
            _start_supervisor_for_one_shot()
            result = poll_sql_job_result(sql_id, field="TUNED_TEST", space_nm=space_nm, timeout_sec=300)
            return json.dumps(result, ensure_ascii=False, default=str)

        return json.dumps({"error": f"알 수 없는 도구: {name}"})

    except Exception as exc:
        return json.dumps({"error": str(exc)})

# ── CSS ───────────────────────────────────────────────────────────────────────
CSS = """
<style>
/* 대화 목록 버튼 */
div[data-testid="stVerticalBlock"] button.chat-item {
    text-align: left; width: 100%;
}
/* 상태 카드 */
.stat-card {
    background: #f8f9fa; border: 1px solid #e9ecef;
    border-radius: 12px; padding: 14px 16px; margin-bottom: 10px;
}
.stat-card-title {
    font-size: 12px; font-weight: 700; letter-spacing: 1px;
    text-transform: uppercase; color: #6c757d; margin-bottom: 10px;
}
.stat-row {
    display: flex; justify-content: space-between;
    align-items: center; margin-bottom: 4px;
}
.stat-label { font-size: 13px; color: #495057; }
.stat-val   { font-size: 14px; font-weight: 700; color: #212529; }
.status-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    margin-top: 10px;
}
.status-box {
    background: #ffffff;
    border: 1px solid #e9ecef;
    border-radius: 8px;
    padding: 8px 10px;
    min-height: 58px;
    overflow-wrap: anywhere;
}
.status-box-label {
    font-size: 11px;
    line-height: 1.25;
    color: #6c757d;
    font-weight: 700;
    margin-bottom: 5px;
}
.status-box-value {
    font-size: 20px;
    line-height: 1.1;
    font-weight: 800;
}
.status-box-link,
.status-box-link:hover,
.status-box-link:visited {
    color: inherit;
    text-decoration: none;
    display: block;
}
.status-box-clickable {
    cursor: pointer;
    transition: border-color .15s ease, box-shadow .15s ease;
}
.status-box-clickable:hover {
    border-color: #fca5a5;
    box-shadow: 0 2px 8px rgba(239, 68, 68, .14);
}
.badge-pass { color: #10b981; }
.badge-fail { color: #ef4444; }
.badge-etc  { color: #6c757d; }
.rate-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
    margin: 10px 0 8px 0;
}
.rate-box {
    background: #ffffff; border: 1px solid #e9ecef;
    border-radius: 8px; padding: 8px 10px;
}
.rate-label {
    font-size: 11px; color: #6c757d; font-weight: 700;
    letter-spacing: .4px; margin-bottom: 4px;
}
.rate-value { font-size: 18px; font-weight: 800; color: #212529; }
.rate-sub { font-size: 11px; color: #adb5bd; margin-top: 2px; }
.rate-note { font-size: 10px; color: #868e96; line-height: 1.4; margin: 2px 0 8px 0; }
/* 구분선 */
.divider { border-top: 1px solid #e9ecef; margin: 8px 0; }
</style>
"""

# ── 채팅 파일 관리 ─────────────────────────────────────────────────────────────
def _list_chats() -> list[dict]:
    if not _CHATS_DIR.exists():
        return []
    chats = []
    for f in sorted(_CHATS_DIR.glob("*.json"), reverse=True):
        try:
            chats.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return chats

def _load_chat(chat_id: str) -> dict | None:
    path = _CHATS_DIR / f"{chat_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

def _save_chat(chat: dict):
    _CHATS_DIR.mkdir(parents=True, exist_ok=True)
    (_CHATS_DIR / f"{chat['id']}.json").write_text(
        json.dumps(chat, ensure_ascii=False, indent=2), encoding="utf-8"
    )

def _delete_chat(chat_id: str):
    (_CHATS_DIR / f"{chat_id}.json").unlink(missing_ok=True)

def _new_chat() -> dict:
    return {
        "id":       datetime.now().strftime("%Y%m%d_%H%M%S_%f"),
        "title":    "새 대화",
        "messages": [],
    }

# ── MAP_ID 감지 + DB 로그 조회 ────────────────────────────────────────────────
def _extract_map_ids(text: str) -> list[int]:
    """메시지에서 MAP_ID 숫자를 추출."""
    patterns = [
        r"map[\s_-]?id[\s:=]?\s*(\d+)",
        r"(\d+)\s*번",
        r"#(\d+)",
    ]
    found = set()
    for p in patterns:
        for m in re.findall(p, text, re.IGNORECASE):
            found.add(int(m))
    return list(found)

def _fetch_map_context(map_ids: list[int]) -> str:
    """MAP_ID별 상세 정보 + 로그를 텍스트로 반환."""
    if not map_ids:
        return ""
    lines = ["", "[조회된 MAP_ID 상세 정보]"]
    try:
        all_jobs = {int(j["MAP_ID"]): j for j in get_mig_jobs()}
        for mid in map_ids:
            lines.append(f"\n▶ MAP_ID {mid}")
            job = all_jobs.get(mid)
            if not job:
                lines.append("  - 해당 MAP_ID 없음")
                continue
            lines.append(f"  - 소스→타겟: {job.get('FR_TABLE')} → {job.get('TO_TABLE')}")
            lines.append(f"  - 상태: {job.get('STATUS') or 'NULL'}")
            lines.append(f"  - 재시도: {job.get('RETRY_COUNT')}회, 소요: {job.get('ELAPSED_SECONDS')}초")
            if job.get("MIG_SQL"):
                lines.append(f"  - MIG_SQL: {str(job['MIG_SQL'])[:200]}")
            if job.get("VERIFY_SQL"):
                lines.append(f"  - VERIFY_SQL: {str(job['VERIFY_SQL'])[:200]}")

            logs = get_mig_logs(mid)
            if logs:
                lines.append(f"  - 실행 로그 ({len(logs)}건):")
                for lg in logs[-10:]:  # 최근 10개만
                    lines.append(
                        f"    [{lg.get('LOG_LEVEL','?')}][{lg.get('STEP_NAME','?')}] "
                        f"{str(lg.get('MESSAGE',''))[:150]}"
                    )
            else:
                lines.append("  - 로그 없음")
    except Exception as e:
        lines.append(f"  (조회 오류: {e})")
    return "\n".join(lines)

# ── LLM ───────────────────────────────────────────────────────────────────────
def _system_prompt(supervisor_mode: bool = False) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    if supervisor_mode:
        lines = [
            "당신은 Oracle 데이터 마이그레이션 파이프라인의 **Supervisor AI 어시스턴트**입니다.",
            "사용자의 질문에 한국어로 전문적이고 구체적으로 답변하세요.",
            "",
            "당신의 역할:",
            "1. 사용자가 특정 map_id의 실패 원인을 물어보면 query_mig_failure_log 도구를 호출하여",
            "   로그를 조회한 뒤 원인을 한국어로 분석·요약합니다.",
            "2. 사용자가 sql_id의 실패 원인을 물어보면 query_sql_failure_log 도구를 호출합니다.",
            "3. 사용자가 재실행을 요청하면 즉시 해당 rerun 도구를 호출하여 DB 상태를 변경하고",
            "   Supervisor를 깨웁니다. 사용자에게 확인 메시지를 묻지 않고 바로 실행합니다.",
            "4. 정보 조회와 재실행을 조합할 수 있습니다 (예: 실패 원인 확인 후 재실행).",
            "",
            "도구 선택 기준:",
            "- '왜 실패했어?', '실패 원인', '로그 보여줘' → query_mig_failure_log 또는 query_sql_failure_log",
            "- 'SQL Conversion Fail 종합 분석', '최근 SQL Conversion Fail 원인 종합 분석' → analyze_sql_conversion_failures",
            "- 'SQL Tuning Fail 종합 분석', '최근 SQL Tuning Fail 원인 종합 분석' → analyze_sql_tuning_failures",
            "- '재실행해줘', '다시 돌려줘', '다시 실행' + migration/이관 → rerun_migration",
            "- '재실행해줘', '다시 돌려줘' + sql 변환/conversion → rerun_sql_conversion",
            "- '재실행해줘', '다시 돌려줘' + sql 튜닝/tuning → rerun_sql_tuning",
            "",
            "종합 분석 답변 형식:",
            "1) 전체 FAIL 건수와 MAP_KIND/MAP_TYPE 분포",
            "2) SQL LENGTH 구간별 분포",
            "3) LOG 유형별 건수",
            "4) 관리자 원인 힌트와 실제 LOG 패턴을 대조한 주요 원인 추정과 근거",
            "5) 다음 확인/개선 질문 2~3개",
            "관리자 원인 힌트(admin_fail_cause_hints)는 높은 우선순위의 도메인 지식입니다.",
            "다만 LOG 근거가 부족하면 가능성으로 표현하고, 어떤 추가 확인이 필요한지 질문하세요.",
            "",
            f"[현재 시각: {now}]",
            "",
            "[에이전트별 현황]",
        ]
    else:
        lines = [
            "당신은 Oracle 데이터 마이그레이션 파이프라인의 운영 어시스턴트입니다.",
            "사용자의 질문에 한국어로 친절하고 간결하게 답변하세요.",
            "숫자나 상태를 물어보면 아래 실시간 DB 데이터를 기반으로 정확히 답변하세요.",
            "",
            f"[현재 시각: {now}]",
            "",
            "[에이전트별 현황]",
        ]

    for label, fn in [
        ("Mig Agent",    get_mig_status_summary),
        ("SQL Agent",    get_sql_status_summary),
        ("Tuning Agent", get_tuning_status_summary),
    ]:
        try:
            s = fn()
            detail = ", ".join(f"{k} {v}건" for k, v in s.items())
            lines.append(f"- {label}: {detail} (합계 {sum(s.values())}건)")
        except Exception:
            lines.append(f"- {label}: 조회 실패")

    # Supervisor 모드에서는 실패 작업을 더 많이, 더 상세히 포함
    max_fails = 20 if supervisor_mode else 5
    try:
        fails = get_recent_fails(max_fails)
        lines.append("")
        if fails:
            lines.append("[최근 실패 작업]")
            for r in fails:
                lines.append(f"- MAP_ID {r['MAP_ID']}: {r['FR_TABLE']} → {r['TO_TABLE']}")
        else:
            lines.append("[최근 실패 작업]: 없음")
    except Exception:
        pass

    if supervisor_mode:
        lines.append("")
        lines.append("[Supervisor 모드 안내]")
        lines.append("사용자가 재실행을 요청하면 명확하게 확인 메시지를 출력하세요.")
        lines.append("재실행 명령이 Supervisor에 전달되었음을 사용자에게 알려주세요.")

    return "\n".join(lines)


def _extract_sql_ids(text: str) -> list[tuple[str, str | None]]:
    """메시지에서 (sql_id, space_nm) 쌍을 추출합니다."""
    sm = re.search(r"space[\s_-]?nm\s*[=:]\s*['\"]?(\w+)['\"]?", text, re.IGNORECASE)
    space_nm = sm.group(1) if sm else None
    sql_ids = re.findall(r"sql[\s_-]?id\s*[=:]\s*['\"]?(\w+)['\"]?", text, re.IGNORECASE)
    return [(sid, space_nm) for sid in sql_ids]


def _fetch_sql_context(sql_pairs: list[tuple[str, str | None]]) -> str:
    """(sql_id, space_nm) 목록의 실패 로그를 텍스트로 반환합니다."""
    if not sql_pairs:
        return ""
    lines = ["", "[조회된 SQL_ID 상세 정보]"]
    for sql_id, space_nm in sql_pairs:
        qualifier = f" (space_nm={space_nm})" if space_nm else ""
        lines.append(f"\n▶ sql_id={sql_id}{qualifier}")
        try:
            rows = get_sql_failure_log(sql_id, space_nm)
            if not rows:
                lines.append("  - 해당 SQL_ID 없음")
                continue
            for r in rows:
                lines.append(f"  - STATUS: {r.get('STATUS') or 'NULL'}, TUNED_TEST: {r.get('TUNED_TEST') or 'NULL'}")
                log_text = r.get("LOG") or ""
                if log_text:
                    lines.append(f"  - LOG: {str(log_text)[:300]}")
        except Exception as e:
            lines.append(f"  (조회 오류: {e})")
    return "\n".join(lines)


def _call_llm_supervisor(chat_messages: list[dict]) -> str:
    """Supervisor 모드 전용 LLM 호출: function calling 루프로 도구를 자율 실행합니다."""
    api_key  = os.getenv("OPEN_API_KEY") or os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL",  "")
    model    = os.getenv("LLM_MODEL", "GLM-5.1")
    client   = OpenAI(api_key=api_key, base_url=base_url)

    system = _system_prompt(supervisor_mode=True)
    messages: list[dict] = [{"role": "system", "content": system}] + list(chat_messages)

    for _ in range(5):  # 최대 5 라운드 (도구 연쇄 호출 대비)
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=_SUPERVISOR_TOOLS,
            tool_choice="auto",
            temperature=0.5,
            max_tokens=5000,
        )
        msg = resp.choices[0].message

        # 도구 호출이 없으면 최종 답변
        if not msg.tool_calls:
            return (msg.content or "").strip()

        # assistant 메시지를 히스토리에 추가
        messages.append(msg.model_dump(exclude_unset=True))

        # 각 도구 호출 실행 후 결과를 히스토리에 추가
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_result = _handle_supervisor_tool(tc.function.name, args)
            messages.append({
                "role":         "tool",
                "tool_call_id": tc.id,
                "content":      tool_result,
            })

    # 5 라운드 소진 시 마지막 응답 반환
    return (msg.content or "도구 처리 완료. 결과를 확인하세요.").strip()


def _call_llm(chat_messages: list[dict], supervisor_mode: bool = False) -> str:
    if supervisor_mode:
        return _call_llm_supervisor(chat_messages)

    api_key  = os.getenv("OPEN_API_KEY") or os.getenv("LLM_API_KEY", "")
    base_url = os.getenv("LLM_BASE_URL",  "")
    model    = os.getenv("LLM_MODEL", "GLM-5.1")
    client   = OpenAI(api_key=api_key, base_url=base_url)

    last_user = next(
        (m["content"] for m in reversed(chat_messages) if m["role"] == "user"), ""
    )

    # MAP_ID 감지 → MIG 로그 컨텍스트
    map_ids = _extract_map_ids(last_user)
    extra   = _fetch_map_context(map_ids) if map_ids else ""

    # SQL_ID 감지 → SQL 실패 로그 컨텍스트
    sql_pairs = _extract_sql_ids(last_user)
    extra += _fetch_sql_context(sql_pairs)

    system  = _system_prompt(supervisor_mode=False) + extra

    full_messages = [{"role": "system", "content": system}] + chat_messages
    resp = client.chat.completions.create(
        model=model, messages=full_messages, temperature=0.7, max_tokens=5000
    )
    return resp.choices[0].message.content.strip()

# ── 오른쪽 상태 패널 ───────────────────────────────────────────────────────────
_ICON = {
    "PASS": "✅",
    "CONVERSION-PASS": "✅",
    "TUNING-PASS": "✅",
    "PASS (non-select)": "✅",
    "FAIL": "❌",
    "FAIL-TOBE": "❌",
    "FAIL-TUNED": "❌",
    "FAIL-BIND": "❌",
    "FAIL-TEST": "❌",
    "RUNNING": "🔄",
    "READY": "🔵",
    "SKIP": "⏭️",
    "NA": "🚫",
    "NULL": "⚫",
    "SQL Conversion 단계": "⏳",
    "PENDING": "🟣",
}
_CLR = {
    "PASS": "badge-pass",
    "CONVERSION-PASS": "badge-pass",
    "TUNING-PASS": "badge-pass",
    "PASS (non-select)": "badge-pass",
    "FAIL": "badge-fail",
    "FAIL-TOBE": "badge-fail",
    "FAIL-TUNED": "badge-fail",
    "FAIL-BIND": "badge-fail",
    "FAIL-TEST": "badge-fail",
}
_STATUS_ORDER = [
    "CONVERSION-PASS",
    "TUNING-PASS",
    "PASS",
    "PASS (non-select)",
    "FAIL-TOBE",
    "FAIL-TUNED",
    "FAIL-BIND",
    "FAIL-TEST",
    "FAIL",
    "RUNNING",
    "SKIP",
    "PENDING",
    "NULL",
    "SQL Conversion 단계",
]
_PROGRESS_EXCLUDED = {"NA"}

def _norm_status(status) -> str:
    return str(status or "NULL").strip().upper() or "NULL"

def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "-"
    return f"{(numerator / denominator) * 100:.1f}%"

def _sum_excluding(normalized: dict[str, int], excluded: set[str]) -> int:
    return sum(v for k, v in normalized.items() if k not in excluded)

def _is_tuning_title(title: str) -> bool:
    return "TUNING" in str(title or "").upper()

def _dashboard_status(status, title: str = "") -> str | None:
    normalized = _norm_status(status)
    if normalized == "NA":
        return None
    if normalized in {"URGENT", "READY"}:
        return "RUNNING"
    if _is_tuning_title(title) and normalized == "NULL":
        return "SQL Conversion 단계"
    if normalized in {"FAIL-TOBE", "FAIL-TUNED", "FAIL-BIND", "FAIL-TEST"}:
        return normalized
    if normalized == "FAIL":
        return "FAIL"
    if (
        normalized in {
            "PASS_NON_SELECT",
            "PASS (NON-SELECT)",
            "PASS(NON-SELECT)",
            "PASS NON SELECT",
            "PASS-NON-SELECT",
        }
        or "NON_SELECT" in normalized
        or "NON-SELECT" in normalized
    ):
        return "PASS (non-select)"
    if normalized in {"TUNING-PASS", "TUNING_PASS"}:
        return "TUNING-PASS"
    if normalized == "CONVERSION-PASS":
        return "CONVERSION-PASS"
    if normalized == "PASS":
        return "TUNING-PASS" if _is_tuning_title(title) else "CONVERSION-PASS"
    return normalized

def _rate_values(title: str, normalized: dict[str, int]) -> tuple[int, int, int, int]:
    pass_count = (
        normalized.get("PASS", 0)
        + normalized.get("CONVERSION-PASS", 0)
        + normalized.get("TUNING-PASS", 0)
    )
    pass_non_select_count = normalized.get("PASS (non-select)", 0)
    fail_count = sum(
        int(v or 0)
        for k, v in normalized.items()
        if str(k).upper() == "FAIL" or str(k).upper().startswith("FAIL-")
    )

    if _is_tuning_title(title):
        progress_count = pass_count + pass_non_select_count
        progress_base = _sum_excluding(normalized, {"NA", "NULL", "SQL Conversion 단계"})
    else:
        progress_count = pass_count
        progress_base = _sum_excluding(normalized, _PROGRESS_EXCLUDED)

    success_count = pass_count
    success_base = pass_count + fail_count
    return progress_count, progress_base, success_count, success_base

def _rate_html(title: str, normalized: dict[str, int]) -> str:
    progress_count, progress_base, success_count, success_base = _rate_values(title, normalized)
    if _is_tuning_title(title):
        rate_note = "진척률=PASS 계열/(이전 단계 대기 제외), 성공률=PASS 계열/(PASS 계열+FAIL)"
    else:
        rate_note = "진척률=PASS/(NA 제외), 성공률=PASS/(PASS+FAIL)"

    return f"""
      <div class="rate-grid">
        <div class="rate-box">
          <div class="rate-label">진척률</div>
          <div class="rate-value">{_pct(progress_count, progress_base)}</div>
          <div class="rate-sub">{progress_count}/{progress_base}건</div>
        </div>
        <div class="rate-box">
          <div class="rate-label">성공률</div>
          <div class="rate-value">{_pct(success_count, success_base)}</div>
          <div class="rate-sub">{success_count}/{success_base}건</div>
        </div>
      </div>
      <div class="rate-note">{rate_note}</div>
    """

def _length_success_html(length_summary: dict[str, dict[str, int]]) -> str:
    short_pass = int(length_summary.get("SHORT", {}).get("PASS", 0) or 0)
    short_fail = int(length_summary.get("SHORT", {}).get("FAIL", 0) or 0)
    long_pass = int(length_summary.get("LONG", {}).get("PASS", 0) or 0)
    long_fail = int(length_summary.get("LONG", {}).get("FAIL", 0) or 0)
    short_base = short_pass + short_fail
    long_base = long_pass + long_fail
    return f"""
      <div class="rate-grid">
        <div class="rate-box">
          <div class="rate-label">SQL ≤ 5000 성공률</div>
          <div class="rate-value">{_pct(short_pass, short_base)}</div>
          <div class="rate-sub">{short_pass}/{short_base}건</div>
        </div>
        <div class="rate-box">
          <div class="rate-label">SQL > 5000 성공률</div>
          <div class="rate-value">{_pct(long_pass, long_base)}</div>
          <div class="rate-sub">{long_pass}/{long_base}건</div>
        </div>
      </div>
      <div class="rate-note">Length 기준: FR_SQL_TEXT ≤ 5000 and (EDIT_FR_SQL ≤ 5000 or EDIT_FR_SQL is NULL)</div>
    """

def _counter_markdown(title: str, items: list[dict], total: int):
    st.markdown(f"**{title}**")
    if not items:
        st.caption("데이터 없음")
        return
    for item in items[:8]:
        name = str(item.get("name") or "UNKNOWN")
        count = int(item.get("count") or 0)
        pct = (count / total * 100) if total else 0
        st.markdown(f"- `{name}`: {count}건 ({pct:.1f}%)")


def _show_sql_fail_analysis_panel(agent: str, rows: list[dict]):
    summary = _summarize_sql_fail_rows(rows, agent)
    total = int(summary.get("total_fail_rows") or 0)
    label = "SQL Conversion" if agent == "SQL_CONVERSION" else "SQL Tuning"

    with st.expander(f"{label} FAIL 원인 통계", expanded=True):
        if total <= 0:
            st.caption("최근 FAIL 데이터가 없습니다.")
            return
        st.caption(f"최근 {total}건 기준")
        _counter_markdown("Stage 분포", summary["fail_stage_counts"], total)
        _counter_markdown("LOG 유형", summary["log_type_counts"], total)
        _counter_markdown("SQL 길이", summary["length_counts"], total)
        _counter_markdown("MAP_TYPE/TAG_KIND", summary["map_kind_counts"], total)

        samples = summary.get("recent_samples") or []
        if samples:
            st.markdown("**최근 샘플**")
            st.dataframe(
                [
                    {
                        "SQL_ID": item.get("sql_id"),
                        "SPACE_NM": item.get("space_nm"),
                        "STAGE": item.get("fail_stage"),
                        "LOG_TYPE": item.get("log_type"),
                        "MAP": item.get("map_kind"),
                        "LEN": item.get("length_bucket"),
                        "UPD_TS": item.get("upd_ts"),
                        "LOG": item.get("log"),
                    }
                    for item in samples[:12]
                ],
                hide_index=True,
                width="stretch",
                height=260,
            )


def _status_card(title: str, summary: dict, extra_html: str = ""):
    normalized_summary: dict[str, int] = {}
    for k, v in summary.items():
        status = _dashboard_status(k, title)
        if status is None:
            continue
        normalized_summary[status] = normalized_summary.get(status, 0) + int(v or 0)

    if not normalized_summary:
        st.markdown(f"""
        <div class="stat-card">
          <div class="stat-card-title">{title}</div>
          <span style="color:#9ca3af;font-size:13px">데이터 없음</span>
        </div>""", unsafe_allow_html=True)
        return
    total = sum(normalized_summary.values())
    status_boxes = []
    for k, v in sorted(
        normalized_summary.items(),
        key=lambda x: _STATUS_ORDER.index(x[0]) if x[0] in _STATUS_ORDER else 99,
    ):
        icon = _ICON.get(k, "◻️")
        cls = _CLR.get(k, "badge-etc")
        safe_status = html.escape(str(k))
        box_html = (
            f'<div class="status-box">'
            f'<div class="status-box-label">{icon} {safe_status}</div>'
            f'<div class="status-box-value {cls}">{v}</div>'
            f'</div>'
        )
        if (k == "FAIL" or str(k).startswith("FAIL-")) and int(v or 0) > 0 and ("SQL" in title or "Tuning" in title or "Mig" in title):
            if "Mig" in title:
                agent = "DB_MIGRATION"
            else:
                agent = "SQL_CONVERSION" if "SQL" in title else "SQL_TUNING"
            query = urlencode({"page": "🔎 Fail Analysis", "agent": agent})
            box_html = (
                f'<a class="status-box-link" href="?{query}">'
                f'<div class="status-box status-box-clickable">'
                f'<div class="status-box-label">{icon} {safe_status}</div>'
                f'<div class="status-box-value {cls}">{v}</div>'
                f'</div>'
                f'</a>'
            )
        status_boxes.append(
            box_html
        )
    st.markdown(f"""
    <div class="stat-card">
      <div class="stat-card-title">{title} &nbsp;<span style="font-weight:400;color:#adb5bd">총 {total}건</span></div>
      {_rate_html(title, normalized_summary)}
      {extra_html}
      <div class="status-grid">
        {''.join(status_boxes)}
      </div>
    </div>""", unsafe_allow_html=True)


def _formatting_card(summary: dict):
    total = int(summary.get("TOTAL", 0) or 0)
    applied = int(summary.get("APPLIED", 0) or 0)
    pending = int(summary.get("PENDING", 0) or 0)
    if total <= 0:
        st.markdown("""
        <div class="stat-card">
          <div class="stat-card-title">🧾 Formatting Guide</div>
          <span style="color:#9ca3af;font-size:13px">데이터 없음</span>
        </div>""", unsafe_allow_html=True)
        return

    st.markdown(f"""
    <div class="stat-card">
      <div class="stat-card-title">🧾 Formatting Guide &nbsp;<span style="font-weight:400;color:#adb5bd">총 {total}건</span></div>
      <div class="rate-grid">
        <div class="rate-box">
          <div class="rate-label">적용률</div>
          <div class="rate-value">{_pct(applied, total)}</div>
          <div class="rate-sub">{applied}/{total}건</div>
        </div>
        <div class="rate-box">
          <div class="rate-label">미적용</div>
          <div class="rate-value">{pending}</div>
          <div class="rate-sub">포맷팅 대기</div>
        </div>
      </div>
      <div class="rate-note">적용률=FORMATTED_SQL 값 있음 / TUNED_TEST PASS 계열</div>
      <div class="status-grid">
        <div class="status-box">
          <div class="status-box-label">✅ 적용</div>
          <div class="status-box-value badge-pass">{applied}</div>
        </div>
        <div class="status-box">
          <div class="status-box-label">⚫ 미적용</div>
          <div class="status-box-value badge-etc">{pending}</div>
        </div>
      </div>
    </div>""", unsafe_allow_html=True)

# ── 메인 렌더 ─────────────────────────────────────────────────────────────────
def render():
    st.markdown(CSS, unsafe_allow_html=True)

    # ── 세션 초기화 ──────────────────────────────────────────────────────────
    supervisor_mode = _is_supervisor_mode()

    if "current_chat" not in st.session_state:
        st.session_state.current_chat = _new_chat()
    if "chat_refresh" not in st.session_state:
        st.session_state.chat_refresh = False
    if "chat_pending_response" not in st.session_state:
        st.session_state.chat_pending_response = False
    if "chat_pending_id" not in st.session_state:
        st.session_state.chat_pending_id = None
    if "chat_pending_supervisor_mode" not in st.session_state:
        st.session_state.chat_pending_supervisor_mode = False
    if "supervisor_command_sent" not in st.session_state:
        st.session_state.supervisor_command_sent = None

    chat = st.session_state.current_chat

    # ── 3패널 레이아웃 ────────────────────────────────────────────────────────
    left, center, right = st.columns([1.6, 4, 1.6], gap="medium")

    # ════════════════════════════════════════════════════════════
    # 왼쪽: 대화 목록
    # ════════════════════════════════════════════════════════════
    with left:
        st.markdown("#### 💬 대화 목록")
        if st.button("✏️ 새 대화", width="stretch", type="primary"):
            st.session_state.current_chat = _new_chat()
            st.rerun()

        st.markdown("---")
        chats = _list_chats()
        if not chats:
            st.caption("대화 기록 없음")
        for c in chats:
            label = c.get("title", "대화")[:18]
            is_current = c["id"] == chat["id"]
            col_title, col_del = st.columns([5, 1])
            with col_title:
                if st.button(
                    f"{'▶ ' if is_current else ''}{label}",
                    key=f"chat_{c['id']}",
                    width="stretch",
                    type="primary" if is_current else "secondary",
                ):
                    loaded = _load_chat(c["id"])
                    if loaded:
                        st.session_state.current_chat = loaded
                        st.rerun()
            with col_del:
                if st.button("🗑", key=f"del_{c['id']}", help="삭제"):
                    _delete_chat(c["id"])
                    if is_current:
                        st.session_state.current_chat = _new_chat()
                    st.rerun()

    # ════════════════════════════════════════════════════════════
    # 가운데: 채팅
    # ════════════════════════════════════════════════════════════
    with center:
        if supervisor_mode:
            st.markdown("#### 🤖 Supervisor 어시스턴트")
            st.markdown(
                '<span style="background:#fef3c7;color:#92400e;padding:3px 10px;'
                'border-radius:12px;font-size:12px;font-weight:700;">🤖 SUPERVISOR MODE</span>',
                unsafe_allow_html=True,
            )
            st.caption("실패 원인 분석, 특정 map_id/row_id 재실행 요청이 가능합니다.")
        else:
            st.markdown("#### 🤖 Migration 어시스턴트")
            st.caption("파이프라인 상태, 실패 원인, 작업 현황 등 무엇이든 물어보세요.")

        # 재실행 명령 전송 완료 알림
        if st.session_state.supervisor_command_sent:
            st.success(st.session_state.supervisor_command_sent)
            st.session_state.supervisor_command_sent = None

        # 메시지 표시
        msg_container = st.container(height=720)
        with msg_container:
            if not chat["messages"]:
                if supervisor_mode:
                    st.markdown("""
                    <div style="text-align:center;color:#9ca3af;padding:60px 0 30px 0">
                        <div style="font-size:40px">🤖</div>
                        <div style="font-size:15px;margin-top:12px">Supervisor 모드가 활성화되었습니다</div>
                        <div style="font-size:12px;margin-top:8px;color:#d1d5db">
                            예: "map_id=5 실패 원인이 뭐야?" · "map_id=5 migration 재실행해줘"<br>
                            예: "row_id=ABC sql 변환 재실행해줘" · "최근 FAIL 원인 분석해줘"
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.markdown("""
                    <div style="text-align:center;color:#9ca3af;padding:80px 0 40px 0">
                        <div style="font-size:40px">💬</div>
                        <div style="font-size:15px;margin-top:12px">아래에서 질문을 입력해보세요</div>
                        <div style="font-size:12px;margin-top:8px;color:#d1d5db">
                            예: "현재 실패한 작업은 몇 개야?" · "PASS된 이관 테이블 목록 보여줘"
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
            for msg in chat["messages"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
            if (
                st.session_state.chat_pending_response
                and st.session_state.chat_pending_id == chat["id"]
            ):
                with st.chat_message("assistant"):
                    placeholder = st.empty()
                    placeholder.markdown("입력중...")
                    try:
                        pending_sv_mode = st.session_state.get("chat_pending_supervisor_mode", False)
                        answer = _call_llm(chat["messages"], supervisor_mode=pending_sv_mode)
                    except Exception as e:
                        answer = f"⚠️ LLM 호출 실패: {e}"
                    placeholder.markdown(answer)
                chat["messages"].append({"role": "assistant", "content": answer})
                _save_chat(chat)
                st.session_state.current_chat = chat
                st.session_state.chat_pending_response = False
                st.session_state.chat_pending_id = None
                st.session_state.chat_pending_supervisor_mode = False
                st.rerun()

        # 입력
        placeholder_text = (
            "실패 원인 분석 또는 재실행 요청을 입력하세요... (예: map_id=5 재실행해줘)"
            if supervisor_mode
            else "메시지를 입력하세요..."
        )
        quick_prompt = None
        user_input = st.chat_input(placeholder_text, key="chat_input")

    # ════════════════════════════════════════════════════════════
    # 오른쪽: 에이전트 상태
    # ════════════════════════════════════════════════════════════
    with right:
        rc, rr = st.columns([2.6, 0.5], gap="small")
        with rc:
            st.markdown("#### 📊 현황")
        with rr:
            if st.button("🔄", help="새로고침", width="stretch"):
                st.rerun()

        try:
            _status_card("📦 Mig",    get_mig_status_summary())
        except Exception as e:
            st.error(str(e))
        try:
            sql_status_summary = get_sql_status_summary()
            _status_card(
                "🔄 SQL",
                sql_status_summary,
                extra_html=_length_success_html(get_sql_length_success_summary()),
            )
        except Exception as e:
            st.error(str(e))
        try:
            tuning_status_summary = get_tuning_status_summary()
            _status_card("⚡ Tuning", tuning_status_summary)
        except Exception as e:
            st.error(str(e))
        try:
            _formatting_card(get_formatting_summary())
        except Exception as e:
            st.error(str(e))

    # ── 메시지 처리 (컬럼 밖에서) ─────────────────────────────────────────────
    selected_input = quick_prompt or user_input
    if selected_input and selected_input.strip():
        user_text = selected_input.strip()

        # 유저 메시지 추가 후 LLM 응답 요청 (Supervisor 모드에서는 LLM이 직접 도구 판단)
        chat["messages"].append({"role": "user", "content": user_text})
        if chat["title"] == "새 대화":
            chat["title"] = user_text[:24]

        _save_chat(chat)
        st.session_state.current_chat = chat
        st.session_state.chat_pending_response = True
        st.session_state.chat_pending_id = chat["id"]
        st.session_state.chat_pending_supervisor_mode = supervisor_mode
        st.rerun()
