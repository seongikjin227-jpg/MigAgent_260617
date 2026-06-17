import pandas as pd
import streamlit as st

from utils.db import (
    get_agent_batch_summary,
    get_agent_name_summary,
    get_recent_agent_run_metrics,
    get_recent_sql_stage_logs,
    get_sql_stage_summary,
)


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows or [])


def _num(df: pd.DataFrame, column: str) -> float:
    if df.empty or column not in df.columns:
        return 0.0
    return float(pd.to_numeric(df[column], errors="coerce").fillna(0).sum())


def _fmt_seconds(value) -> str:
    try:
        return f"{float(value):,.1f}s"
    except Exception:
        return "-"


def _show_table(title: str, rows: list[dict], *, height: int = 320):
    st.subheader(title)
    df = _df(rows)
    if df.empty:
        st.info("No data")
        return
    st.dataframe(df, width="stretch", height=height)


def render():
    st.title("Agent Metrics")
    st.caption("Operational metrics from AG_AGENT_RUN_METRICS and NEXT_SQL_LOG.")

    col_a, col_b, col_c = st.columns([1, 1, 1])
    with col_a:
        run_limit = st.number_input("Recent agent runs", min_value=50, max_value=1000, value=200, step=50)
    with col_b:
        batch_limit = st.number_input("Recent batches", min_value=10, max_value=200, value=50, step=10)
    with col_c:
        sql_log_limit = st.number_input("Recent SQL logs", min_value=50, max_value=1000, value=100, step=50)

    if st.button("Refresh", width="stretch"):
        st.rerun()

    recent_runs = get_recent_agent_run_metrics(int(run_limit))
    batch_rows = get_agent_batch_summary(int(batch_limit))
    agent_rows = get_agent_name_summary(int(run_limit))
    stage_rows = get_sql_stage_summary(int(sql_log_limit))
    recent_sql_logs = get_recent_sql_stage_logs(int(sql_log_limit))

    run_df = _df(recent_runs)
    batch_df = _df(batch_rows)
    stage_df = _df(stage_rows)

    st.divider()
    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Recent Agent Runs", len(run_df))
    with m2:
        st.metric("Processed Jobs", int(_num(run_df, "JOB_COUNT")))
    with m3:
        st.metric("Success/Fail", f"{int(_num(run_df, 'SUCCESS_COUNT'))}/{int(_num(run_df, 'FAIL_COUNT'))}")
    with m4:
        avg_elapsed = 0.0
        if not run_df.empty and "ELAPSED_SECONDS" in run_df.columns:
            avg_elapsed = pd.to_numeric(run_df["ELAPSED_SECONDS"], errors="coerce").fillna(0).mean()
        st.metric("Avg Run Time", _fmt_seconds(avg_elapsed))

    if not batch_df.empty:
        latest_batch = batch_df.iloc[0]
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            st.metric("Latest BATCH_NO", latest_batch.get("BATCH_NO", "-"))
        with c2:
            st.metric("Latest Batch Wall Time", _fmt_seconds(latest_batch.get("WALL_SECONDS")))
        with c3:
            st.metric("Latest Batch Jobs", latest_batch.get("JOB_COUNT", "-"))
        with c4:
            st.metric("Latest Batch Agent Runs", latest_batch.get("AGENT_RUNS", "-"))

    st.divider()
    tab_batch, tab_agent, tab_stage, tab_recent = st.tabs(
        ["Batch Summary", "Agent Average", "SQL Stage Average", "Recent SQL Stage Logs"]
    )

    with tab_batch:
        st.caption("Batch-level wall time and summed agent elapsed time.")
        _show_table("Recent Batch Summary", batch_rows)

    with tab_agent:
        st.caption("Average elapsed time and job counts by AGENT_NAME.")
        _show_table("Agent Run Summary", agent_rows)

    with tab_stage:
        st.caption("Average elapsed time by STAGE_NAME from recent NEXT_SQL_LOG rows.")
        if not stage_df.empty and "AVG_SECONDS" in stage_df.columns and "STAGE_NAME" in stage_df.columns:
            chart_df = stage_df.copy()
            chart_df["AVG_SECONDS"] = pd.to_numeric(chart_df["AVG_SECONDS"], errors="coerce").fillna(0)
            st.bar_chart(chart_df.set_index("STAGE_NAME")["AVG_SECONDS"])
        _show_table("SQL Stage Summary", stage_rows)

    with tab_recent:
        st.caption("Recent SQL stage logs ordered by LOG_ID descending.")
        _show_table("Recent SQL Stage Logs", recent_sql_logs, height=460)
