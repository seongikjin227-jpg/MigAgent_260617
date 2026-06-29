import time

from langchain_core.tools import tool

from server.tools.context import (
    callbacks,
    record_agent_run,
    refresh_jobs_after_tool,
    tuning_registry,
)


@tool
def run_sql_tuning(row_ids: list) -> str:
    """Run SQL tuning jobs for the given NEXT_SQL_INFO row IDs."""
    results = []
    logger = callbacks.get("logger")

    for row_id in row_ids:
        job = tuning_registry.get(str(row_id))
        if job is None:
            results.append(f"row_id={row_id} not found")
            continue

        started = time.perf_counter()
        try:
            callbacks["sql_inc"](row_id)
            final_status = callbacks["tune_proc"](job)
            record_agent_run("SQL_TUNING", time.perf_counter() - started, final_status)
            results.append(f"row_id={row_id} completed")
        except Exception as exc:
            record_agent_run("SQL_TUNING", time.perf_counter() - started, "FAIL")
            if logger:
                logger.error(f"[SqlTuningTool] row_id={row_id} error: {exc}")
            results.append(f"row_id={row_id} failed: {exc}")
        finally:
            refresh_jobs_after_tool()

    return "SqlTuning result: " + " | ".join(results)
