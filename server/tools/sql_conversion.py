from langchain_core.tools import tool
import time

from server.tools.context import callbacks, record_agent_run, sql_registry

@tool
def run_sql_conversion(row_id: str) -> str:
    """SQL 변환 작업 1건을 실행합니다. row_id로 작업을 지정합니다."""
    job = sql_registry.get(row_id)
    logger = callbacks.get("logger")
    
    if job is None:
        return f"ERROR: row_id={row_id} 를 현재 사이클에서 찾을 수 없습니다."
    
    started = time.perf_counter()
    try:
        callbacks["sql_inc"](row_id)
        final_status = callbacks["sql_proc"](job)
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, final_status)
        if logger:
            logger.info(f"[SqlConversionTool] row_id={row_id} 완료")
        return f"SqlConversion row_id={row_id} 완료"
    except Exception as exc:
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[SqlConversionTool] row_id={row_id} 오류: {exc}")
        return f"ERROR: row_id={row_id} 실패 — {exc}"
