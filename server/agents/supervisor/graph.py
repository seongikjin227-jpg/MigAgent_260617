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
from server.tools.context import _stop_event, init_callbacks
from server.tools.cycle import flush_cycle_metrics, request_wait
from server.tools.migration import run_data_migration
from server.tools.poll import build_poll_jobs_tool
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
    init_callbacks(
        mig_inc=mig_increment_batch,
        mig_proc=mig_process_job,
        sql_inc=sql_increment_batch,
        sql_proc=sql_process_job,
        tune_proc=tune_process_job,
        format_proc=format_process_job,
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
