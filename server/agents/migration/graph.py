import time
import os
import re
from typing import Literal
from langgraph.graph import StateGraph, END
from server.core.logger import logger
from server.core.exceptions import (
    LLMBaseError, LLMAuthenticationError, LLMTokenLimitError, LLMInvalidRequestError,
    DBSqlError, VerificationFailError, BatchAbortError
)
from server.services.migration.llm_client import generate_sqls
from server.agents.migration.executor import execute_migration, truncate_table
from server.agents.migration.verifier import execute_verification
from server.repositories.migration.repository import (
    update_job_status,
    check_dependencies,
    check_target_priority_dependencies,
    is_first_job_for_target,
    increment_batch_count,
)
from server.repositories.migration.history_repository import log_generated_sql, log_business_history
from server.core.db_migration import fetch_table_ddl, qualify_fr_table, qualify_to_table
from server.agents.migration.state import MigrationState


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


BIZ_MAX_RETRIES = _env_int("DB_MIGRATION_MAX_ATTEMPTS", 10, minimum=0)
BIZ_MAX_ATTEMPTS = BIZ_MAX_RETRIES + 1

def _retry_count(state: MigrationState) -> int:
    return max(0, int(state.get("db_attempts", 1) or 1) - 1)

def _current_generate_sql(state: MigrationState) -> str:
    return state.get("current_migration_sql") or state.get("last_sql") or ""

def _failure_status(state: MigrationState) -> str:
    explicit_status = state.get("failure_status")
    if explicit_status:
        return explicit_status
    if state.get("status") == "EXECUTED":
        return "FAIL-TEST"
    return "FAIL"

def _extract_table_names(fr_table: str) -> list:
    """FR_TABLE 표현식에서 실제 테이블명만 추출합니다."""
    parts = re.split(
        r'\b(?:(?:LEFT|RIGHT|FULL|INNER|CROSS)\s+(?:OUTER\s+)?)?JOIN\b',
        fr_table, flags=re.IGNORECASE
    )
    tables = []
    for part in parts:
        part = re.split(r'\bON\b', part, flags=re.IGNORECASE)[0].strip()
        tokens = part.split()
        if tokens and tokens[0].upper() not in ('SELECT', 'WITH', 'FROM', '('):
            tables.append(tokens[0])
    return tables

# Nodes
def fetch_ddl_node(state: MigrationState) -> dict:
    job = state["next_sql_info"]
    source_ddl = {}
    for tbl_name in _extract_table_names(job.fr_table):
        source_table = qualify_fr_table(tbl_name)
        rows = fetch_table_ddl(source_table)
        if rows:
            source_ddl[source_table] = rows
            logger.info(f"[Graph:DDL] 소스 {source_table} 컬럼 {len(rows)}개 조회 완료")

    target_table = qualify_to_table(job.to_table)
    target_ddl = fetch_table_ddl(target_table)
    if target_ddl:
        logger.info(f"[Graph:DDL] 타겟 {target_table} 컬럼 {len(target_ddl)}개 조회 완료")
    else:
        logger.warning(f"[Graph:DDL] 타겟 {target_table!r} DDL 조회 결과 없음")

    return {"source_ddl": source_ddl if source_ddl else None, "target_ddl": target_ddl if target_ddl else None}

def check_dependency_node(state: MigrationState) -> dict:
    job = state["next_sql_info"]
    dep_status = check_dependencies(job.map_id, job.prior_map_id)

    if dep_status != "READY":
        if str(dep_status or "").strip().upper() == "FAIL":
            logger.warning(f"[Graph:DEP] map_id={job.map_id} | PRIOR_MAP_ID={job.prior_map_id} FAIL. 후속 작업을 SKIP 합니다.")
            return {"status": "SKIP", "error_type": "DEPENDENCY_FAIL", "last_error": f"선행 MAP_ID={job.prior_map_id} 상태: {dep_status}"}
        logger.warning(f"[Graph:DEP] map_id={job.map_id} | PRIOR_MAP_ID={job.prior_map_id} 미통과 ({dep_status}). 다음 cycle까지 대기합니다.")
        return {"status": "WAITING", "error_type": "DEPENDENCY_WAIT", "last_error": f"선행 MAP_ID={job.prior_map_id} 상태: {dep_status}"}

    target_dep_status = check_target_priority_dependencies(job.map_id, job.to_table, job.priority)
    if target_dep_status != "READY":
        if str(target_dep_status or "").strip().upper() in ("FAIL", "SKIP"):
            logger.warning(
                f"[Graph:DEP] map_id={job.map_id} | same-target prior job {target_dep_status}. Skip this job."
            )
            return {
                "status": "SKIP",
                "error_type": "DEPENDENCY_FAIL",
                "last_error": f"same-target prior job status: {target_dep_status}",
            }
        logger.warning(
            f"[Graph:DEP] map_id={job.map_id} | same-target prior job not ready ({target_dep_status}). Wait."
        )
        return {
            "status": "WAITING",
            "error_type": "DEPENDENCY_WAIT",
            "last_error": f"same-target prior job status: {target_dep_status}",
        }

    increment_batch_count(job.map_id)
    return {"error_type": None}

def generate_sql_node(state: MigrationState) -> dict:
    job = state["next_sql_info"]
    job.retry_count = _retry_count(state)

    attempt_msg = f"{state['db_attempts']}"

    logger.info(f"[Graph:LLM] Attempt {attempt_msg} | SQL 생성 요청")
    try:
        is_append = not is_first_job_for_target(job.map_id, job.to_table, job.priority)
        ddl_sql, migration_sql, v_sql = generate_sqls(
            job,
            state["last_error"],
            state["last_sql"],
            state["source_ddl"],
            state["target_ddl"],
            is_append=is_append
        )

        if not migration_sql or not migration_sql.strip():
            err_msg = "LLM returned an empty migration_sql."
            logger.error(f"[Graph:LLM_EMPTY] {err_msg}")
            return {"error_type": "LLM_RETRY", "last_error": err_msg}

        log_generated_sql(job.map_id, migration_sql, v_sql)
        log_business_history(
            job.map_id,
            "GENERATE_SQL",
            "INFO",
            "GENERATE",
            "PASS",
            "Migration SQL generated",
            _retry_count(state),
            os.getenv("MIG_KIND", "DB_MIG"),
            generate_sql=migration_sql,
        )

        return {
            "last_sql": migration_sql,
            "current_ddl_sql": ddl_sql,
            "current_migration_sql": migration_sql,
            "current_v_sql": v_sql,
            "error_type": None
        }
    except (LLMAuthenticationError, LLMTokenLimitError, LLMInvalidRequestError) as e:
        logger.error(f"[Graph:LLM_FATAL] {str(e)}")
        raise BatchAbortError(f"LLM 치명적 에러: {str(e)}") from e
    except LLMBaseError as e:
        return {"error_type": "LLM_RETRY", "last_error": str(e)}

def execute_sql_node(state: MigrationState) -> dict:
    try:
        job = state["next_sql_info"]
        if str(getattr(job, "trunc_yn", "") or "").strip().upper() == "Y":
            logger.info(f"[Graph:TRUNCATE] map_id={job.map_id} | TRUNC_YN=Y, target={job.to_table}")
            try:
                truncate_table(job.to_table)
            except DBSqlError as e:
                logger.error(f"[Graph:TRUNCATE_FAIL] {str(e)}")
                return {"error_type": "BIZ_RETRY", "failure_status": "FAIL-TRUNCATE", "last_error": str(e)}
        execute_migration(state["current_migration_sql"])
        return {"status": "EXECUTED", "error_type": None}
    except DBSqlError as e:
        logger.error(f"[Graph:EXEC_FAIL] {str(e)}")
        return {"error_type": "BIZ_RETRY", "failure_status": "FAIL-INSERT", "last_error": str(e)}

def verify_sql_node(state: MigrationState) -> dict:
    v_sql = state.get("current_v_sql")
    if not v_sql:
        return {"status": "PASS"}

    try:
        logger.info(f"[Graph:VERIFY] 데이터 정합성 검증 시작")
        is_valid, v_msg = execute_verification(v_sql)
        if not is_valid:
            return {"error_type": "BIZ_RETRY", "last_error": f"데이터 불일치: {v_msg}"}
        return {"status": "PASS", "error_type": None}
    except (VerificationFailError, DBSqlError) as e:
        return {"error_type": "BIZ_RETRY", "last_error": str(e)}

def finalize_node(state: MigrationState) -> dict:
    job = state["next_sql_info"]
    elapsed = int(time.time() - state["job_start_time"])
    mig_kind = os.getenv("MIG_KIND", "DB_MIG")

    if state["status"] == "PASS":
        retry_count = _retry_count(state)
        update_job_status(job.map_id, "PASS", elapsed, retry_count)
        log_business_history(job.map_id, "INFO", "INFO", "VERIFY", "PASS", "Migration Success", retry_count, mig_kind, generate_sql=_current_generate_sql(state))
        logger.info(f"[Graph:FINISH] map_id={job.map_id} | >>> 성공 <<<")
        return {"elapsed_time": elapsed, "status": "PASS"}
    elif state["status"] == "SKIP":
        retry_count = _retry_count(state)
        update_job_status(job.map_id, "SKIP", elapsed, retry_count)
        log_business_history(job.map_id, "JOB_SKIP", "WARN", "DEP_CHECK", "SKIP", state["last_error"], retry_count, mig_kind, generate_sql=_current_generate_sql(state))
        logger.warning(f"[Graph:FINISH] map_id={job.map_id} | >>> SKIP (의존성 실패) <<<")
        return {"elapsed_time": elapsed, "status": "SKIP"}
    elif state["status"] == "WAITING":
        log_business_history(job.map_id, "JOB_WAIT", "INFO", "DEP_CHECK", "WAITING", state["last_error"], _retry_count(state), mig_kind, generate_sql=_current_generate_sql(state))
        logger.info(f"[Graph:FINISH] map_id={job.map_id} | >>> WAITING (의존성 대기) <<<")
        return {"elapsed_time": elapsed, "status": "WAITING"}
    else:
        retry_count = _retry_count(state)
        failure_status = _failure_status(state)
        update_job_status(job.map_id, failure_status, elapsed, retry_count)
        log_business_history(job.map_id, "JOB_FAIL", "ERROR", "FINAL", failure_status, "Max Attempts Reached", retry_count, mig_kind, generate_sql=_current_generate_sql(state))
        logger.error(f"[Graph:FINISH] map_id={job.map_id} | >>> 실패 <<<")
        return {"elapsed_time": elapsed, "status": "FAIL"}

# Routing Logic
def should_continue(state: MigrationState) -> Literal["generate", "finalize", "verify", "execute"]:
    error_type = state.get("error_type")

    if state.get("status") in ("PASS", "SKIP", "WAITING"):
        return "finalize"

    if error_type == "DEPENDENCY_WAIT":
        return "finalize"

    if error_type == "DEPENDENCY_FAIL":
        return "finalize"

    if error_type == "LLM_RETRY":
        last_err = state.get("last_error", "").lower()
        if "429" in last_err or "quota" in last_err or "limit" in last_err:
            logger.critical(f"[Graph:LLM_FATAL] 할당량 초과 또는 인프라 에러 감지. 배치를 즉시 중단합니다: {state['last_error']}")
            raise BatchAbortError(f"LLM 인프라 에러(할당량 초과 등): {state['last_error']}")

        raise BatchAbortError(f"LLM 호출 실패: {state['last_error']}")

    if error_type == "BIZ_RETRY":
        if state["db_attempts"] < state["max_attempts"]:
            return "generate"
        else:
            return "finalize"

    if state.get("status") == "EXECUTED":
        return "verify"

    if not state.get("current_migration_sql"):
        return "generate"

    return "execute"

def biz_retry_prepare_node(state: MigrationState) -> dict:
    job = state["next_sql_info"]
    mig_kind = os.getenv("MIG_KIND", "DB_MIG")
    failure_status = _failure_status(state)
    step_name = "TRUNCATE" if failure_status == "FAIL-TRUNCATE" else ("SQL_EXEC" if failure_status == "FAIL-INSERT" else "VERIFY")

    log_business_history(job.map_id, "ROW_ERROR", "WARN", step_name, failure_status, state["last_error"], _retry_count(state), mig_kind, generate_sql=_current_generate_sql(state))

    time.sleep(1)
    if failure_status == "FAIL-TEST":
        return {
            "db_attempts": state["db_attempts"] + 1,
            "error_type": None,
            "status": "EXECUTED",
            "failure_status": "FAIL-TEST",
        }
    return {"db_attempts": state["db_attempts"] + 1, "error_type": None, "status": None, "failure_status": None}

# Graph Construction
workflow = StateGraph(MigrationState)

workflow.add_node("fetch_ddl", fetch_ddl_node)
workflow.add_node("check_dependency", check_dependency_node)
workflow.add_node("generate", generate_sql_node)
workflow.add_node("execute", execute_sql_node)
workflow.add_node("verify", verify_sql_node)
workflow.add_node("finalize", finalize_node)
workflow.add_node("biz_retry_prepare", biz_retry_prepare_node)

workflow.set_entry_point("fetch_ddl")
workflow.add_edge("fetch_ddl", "check_dependency")

workflow.add_conditional_edges(
    "check_dependency",
    should_continue,
    {
        "generate": "generate",
        "finalize": "finalize",
        "execute": "generate"
    }
)

workflow.add_conditional_edges(
    "generate",
    should_continue,
    {
        "execute": "execute",
        "verify": "verify",
        "finalize": "finalize"
    }
)

workflow.add_conditional_edges(
    "execute",
    should_continue,
    {
        "verify": "verify",
        "generate": "biz_retry_prepare",
        "finalize": "finalize"
    }
)

workflow.add_conditional_edges(
    "verify",
    should_continue,
    {
        "finalize": "finalize",
        "generate": "biz_retry_prepare"
    }
)

workflow.add_edge("biz_retry_prepare", "generate")
workflow.add_edge("finalize", END)

migration_graph = workflow.compile()
