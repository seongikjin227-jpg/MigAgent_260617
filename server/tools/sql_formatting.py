import time

from langchain_core.tools import tool

from server.tools.context import callbacks, formatting_registry, record_agent_run


@tool
def run_sql_formatting(row_ids: list) -> str:
    """Run SQL formatting jobs for the given NEXT_SQL_INFO row IDs."""
    results = []
    logger = callbacks.get("logger")

    for row_id in row_ids:
        job = formatting_registry.get(str(row_id))
        if job is None:
            results.append(f"row_id={row_id} not found")
            continue

        started = time.perf_counter()
        try:
            callbacks["sql_inc"](row_id)
            final_status = callbacks["format_proc"](job)
            record_agent_run("SQL_FORMATTING", time.perf_counter() - started, final_status)
            results.append(f"row_id={row_id} completed")
        except Exception as exc:
            record_agent_run("SQL_FORMATTING", time.perf_counter() - started, "FAIL")
            if logger:
                logger.error(f"[SqlFormattingTool] row_id={row_id} error: {exc}")
            results.append(f"row_id={row_id} failed: {exc}")

    return "SqlFormatting result: " + " | ".join(results)
