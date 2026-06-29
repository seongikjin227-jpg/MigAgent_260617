"""Supervisor LangGraph — LLM 기반 ReAct 루프.

수퍼바이저 LLM이 poll_jobs → 실행 도구들 → flush_cycle_metrics → request_wait
순서로 도구를 호출하여 한 사이클을 처리합니다.
사이클 반복은 SupervisorAgent.run()의 외부 while 루프가 담당합니다.
"""

from __future__ import annotations

from typing import Literal

from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from server.agents.supervisor.state import SupervisorState
from server.config.settings import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_MAX_TOKENS,
    LLM_MODEL,
)
from server.core.llm_fallback import (
    is_model_fallback_error,
    model_candidates,
    set_active_model,
)
from server.tools.context import (
    _stop_event,
    formatting_registry,
    init_callbacks,
    mig_registry,
    sql_registry,
    tuning_registry,
)
from server.tools.cycle import flush_cycle_metrics, request_wait
from server.tools.migration import run_data_migration
from server.tools.poll import (
    MIGRATION_JOB_BATCH_SIZE,
    SQL_CONVERSION_JOB_BATCH_SIZE,
    SQL_FORMATTING_JOB_BATCH_SIZE,
    SQL_TUNING_JOB_BATCH_SIZE,
    _agent_flags,
    build_poll_jobs_tool,
)
from server.tools.sql_conversion import run_sql_conversion
from server.tools.sql_formatting import run_sql_formatting
from server.tools.sql_tuning import run_sql_tuning


def _build_llm(model_name: str) -> ChatOpenAI:
    kwargs: dict = {
        "model": model_name,
        "api_key": LLM_API_KEY,
        "max_tokens": LLM_MAX_TOKENS,
    }
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    return ChatOpenAI(**kwargs)


def build_supervisor_graph(
    get_migration_jobs,
    get_sql_jobs,
    get_tuning_jobs,
    get_formatting_jobs,
    mig_increment_batch,
    mig_process_job,
    sql_increment_batch,
    sql_process_job,
    tune_process_job,
    format_process_job,
    logger,
):
    def refresh_jobs_after_run() -> None:
        run_mig, run_sql, run_tuning, run_fmt = _agent_flags()
        mig_jobs, sql_jobs, tuning_jobs, formatting_jobs = [], [], [], []

        try:
            if run_mig:
                mig_jobs = get_migration_jobs()
        except Exception as exc:
            logger.error(f"[refresh_jobs] DataMigration query error: {exc}")

        try:
            if run_sql:
                sql_jobs = get_sql_jobs()
            if run_tuning:
                tuning_jobs = get_tuning_jobs()
            if run_fmt:
                formatting_jobs = get_formatting_jobs()
        except Exception as exc:
            logger.error(f"[refresh_jobs] SQL/Tuning/Formatting query error: {exc}")

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

        logger.info(
            "[refresh_jobs] refreshed after job "
            f"(Mig={len(mig_registry)}/{len(mig_jobs)}, "
            f"Sql={len(sql_registry)}/{len(sql_jobs)}, "
            f"Tuning={len(tuning_registry)}/{len(tuning_jobs)}, "
            f"Formatting={len(formatting_registry)}/{len(formatting_jobs)})"
        )

    init_callbacks(
        mig_inc=mig_increment_batch,
        mig_proc=mig_process_job,
        sql_inc=sql_increment_batch,
        sql_proc=sql_process_job,
        tune_proc=tune_process_job,
        format_proc=format_process_job,
        refresh_jobs=refresh_jobs_after_run,
        logger=logger,
    )

    poll_jobs = build_poll_jobs_tool(
        get_migration_jobs,
        get_sql_jobs,
        get_tuning_jobs,
        get_formatting_jobs,
    )

    tools = [
        poll_jobs,
        run_data_migration,
        run_sql_conversion,
        run_sql_tuning,
        run_sql_formatting,
        flush_cycle_metrics,
        request_wait,
    ]

    tool_node = ToolNode(tools)

    def supervisor_node(state: SupervisorState) -> dict:
        if _stop_event.is_set() or state.get("stop_requested"):
            return {"stop_requested": True}

        messages = state.get("messages") or []
        candidates = model_candidates(LLM_MODEL)
        last_exc: Exception | None = None

        for idx, candidate_model in enumerate(candidates):
            try:
                llm_with_tools = _build_llm(candidate_model).bind_tools(tools)
                response = llm_with_tools.invoke(messages)
                set_active_model(candidate_model)
                return {"messages": [response]}
            except Exception as exc:
                message = str(exc)
                if idx < len(candidates) - 1 and is_model_fallback_error(message):
                    logger.warning(
                        f"[Supervisor LLM] model fallback: {candidate_model} failed ({message}); "
                        f"trying {candidates[idx + 1]}"
                    )
                    last_exc = exc
                    continue
                raise

        if last_exc:
            raise last_exc
        raise RuntimeError("No supervisor LLM model candidates are configured.")

    def route_after_supervisor(
        state: SupervisorState,
    ) -> Literal["tools", "__end__"]:
        if _stop_event.is_set() or state.get("stop_requested"):
            return END
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tools"
        return END

    workflow = StateGraph(SupervisorState)
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("tools", tool_node)

    workflow.set_entry_point("supervisor")
    workflow.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {"tools": "tools", END: END},
    )
    workflow.add_edge("tools", "supervisor")

    return workflow.compile()
