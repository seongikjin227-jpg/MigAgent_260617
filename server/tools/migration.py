import time

from langchain_core.tools import tool

from server.tools.context import (
    callbacks,
    mig_registry,
    record_agent_run,
    refresh_jobs_after_tool,
)


@tool
def run_data_migration(map_id: int) -> str:
    """Run one DB migration job selected by map_id."""
    job = mig_registry.get(map_id)
    logger = callbacks.get("logger")

    if job is None:
        return f"ERROR: map_id={map_id} was not found in the current registry."

    started = time.perf_counter()
    try:
        final_status = callbacks["mig_proc"](job)
        record_agent_run("DB_MIGRATION", time.perf_counter() - started, final_status)
        if logger:
            logger.info(f"[DataMigrationTool] map_id={map_id} completed")
        return f"DataMigration map_id={map_id} completed"
    except Exception as exc:
        record_agent_run("DB_MIGRATION", time.perf_counter() - started, "FAIL")
        if logger:
            logger.error(f"[DataMigrationTool] map_id={map_id} error: {exc}")
        return f"ERROR: map_id={map_id} failed: {exc}"
    finally:
        refresh_jobs_after_tool()
