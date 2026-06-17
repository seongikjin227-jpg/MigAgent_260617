from langchain_core.tools import tool
import time

from server.tools.context import callbacks, mig_registry, record_agent_run

@tool
def run_data_migration(map_id: int) -> str:
    """데이터 이관 작업 1건을 실행합니다. map_id로 작업을 지정합니다."""
    job = mig_registry.get(map_id)
    logger = callbacks.get("logger")
    
    if job is None:
        return f"ERROR: map_id={map_id} 를 현재 사이클에서 찾을 수 없습니다."
    
    started = time.perf_counter()
    try:
        final_status = callbacks["mig_proc"](job)
        record_agent_run("DB_MIGRATION", time.perf_counter() - started, final_status)
        if logger:
            logger.info(f"[DataMigrationTool] map_id={map_id} 완료")
        return f"DataMigration map_id={map_id} 완료"
    except Exception as exc:
        record_agent_run("DB_MIGRATION", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[DataMigrationTool] map_id={map_id} 오류: {exc}")
        return f"ERROR: map_id={map_id} 실패 — {exc}"
