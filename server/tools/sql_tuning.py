from langchain_core.tools import tool
import time

from server.tools.context import callbacks, record_agent_run, tuning_registry

@tool
def run_sql_tuning(row_ids: list) -> str:
    """SQL 튜닝 작업을 실행합니다. row_ids 목록으로 대상 작업들을 지정합니다."""
    results = []
    logger = callbacks.get("logger")
    
    for row_id in row_ids:
        job = tuning_registry.get(str(row_id))
        if job is None:
            results.append(f"row_id={row_id} 없음")
            continue
        started = time.perf_counter()
        try:
            callbacks["sql_inc"](row_id)
            final_status = callbacks["tune_proc"](job)
            record_agent_run("SQL_TUNING", time.perf_counter() - started, final_status)
            results.append(f"row_id={row_id} 완료")
        except Exception as exc:
            record_agent_run("SQL_TUNING", time.perf_counter() - started, "FAIL")
            if logger:
                logger.error(f"[SqlTuningTool] row_id={row_id} 오류: {exc}")
            results.append(f"row_id={row_id} 실패 — {exc}")
            
    return "SqlTuning 결과: " + " | ".join(results)
