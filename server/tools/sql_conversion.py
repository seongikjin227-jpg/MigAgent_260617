import time

from langchain_core.tools import tool

from server.tools.context import (
    callbacks,
    record_agent_run,
    refresh_jobs_after_tool,
    sql_registry,
)


@tool
def run_sql_conversion(row_id: str) -> str:
    """Run one SQL conversion job selected by row_id."""
    row_key = str(row_id)
    job = sql_registry.get(row_key)
    logger = callbacks.get("logger")

    if job is None:
        return f"ERROR: row_id={row_key} was not found in the current registry."

    started = time.perf_counter()
    try:
        callbacks["sql_inc"](row_key)
        final_status = callbacks["sql_proc"](job)
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, final_status)
        if logger:
            logger.info(f"[SqlConversionTool] row_id={row_key} completed")
        return f"SqlConversion row_id={row_key} completed"
    except Exception as exc:
        record_agent_run("SQL_MIGRATION", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[SqlConversionTool] row_id={row_key} error: {exc}")
        return f"ERROR: row_id={row_key} failed: {exc}"
    finally:
        refresh_jobs_after_tool()
