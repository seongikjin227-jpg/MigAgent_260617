import streamlit as st

from pages.dashboard import (
    _call_llm,
    _counter_markdown,
    _delete_chat,
    _list_chats,
    _load_chat,
    _new_chat,
    _save_chat,
    _summarize_sql_fail_rows,
)
from utils.db import (
    get_sql_conversion_failure_analysis_rows,
    get_sql_tuning_failure_analysis_rows,
)


_AGENT_OPTIONS = {
    "SQL Conversion": "SQL_CONVERSION",
    "SQL Tuning": "SQL_TUNING",
}
_AGENT_LABELS = {value: label for label, value in _AGENT_OPTIONS.items()}


def _metric_card(label: str, value: object, sub: str = "", compact: bool = False) -> None:
    compact_cls = " fail-metric-compact" if compact else ""
    st.markdown(
        f"""
        <div class="fail-metric{compact_cls}">
          <div class="fail-metric-label">{label}</div>
          <div class="fail-metric-value">{value}</div>
          <div class="fail-metric-sub">{sub}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _default_agent_index() -> int:
    agent = st.query_params.get("agent", "SQL_CONVERSION")
    values = list(_AGENT_OPTIONS.values())
    return values.index(agent) if agent in values else 0


def _load_rows(agent: str, limit: int) -> list[dict]:
    if agent == "SQL_TUNING":
        return get_sql_tuning_failure_analysis_rows(limit=limit)
    return get_sql_conversion_failure_analysis_rows(limit=limit)


def _render_summary(agent: str, limit: int) -> None:
    rows = _load_rows(agent, limit)
    summary = _summarize_sql_fail_rows(rows, stage=agent)
    total = int(summary.get("total_fail_rows") or 0)
    label = _AGENT_LABELS.get(agent, agent)

    st.markdown(f"### {label} FAIL 원인 통계")
    st.caption(f"최근 FAIL 최대 {limit}건 기준")

    if total <= 0:
        st.info("최근 FAIL 데이터가 없습니다.")
        return

    top_stage = (summary.get("fail_stage_counts") or [{}])[0]
    top_log = (summary.get("log_type_counts") or [{}])[0]
    m1, m2, m3 = st.columns([0.62, 1.15, 1.23], gap="medium")
    with m1:
        _metric_card("FAIL", total, "건", compact=True)
    with m2:
        _metric_card("최다 Stage", top_stage.get("name", "-"), f"{top_stage.get('count', 0)}건")
    with m3:
        _metric_card("최다 LOG 유형", top_log.get("name", "-"), f"{top_log.get('count', 0)}건")

    c1, c2 = st.columns(2)
    with c1:
        _counter_markdown("Stage 분포", summary["fail_stage_counts"], total)
        _counter_markdown("SQL 길이", summary["length_counts"], total)
    with c2:
        _counter_markdown("LOG 유형", summary["log_type_counts"], total)
        _counter_markdown("MAP_TYPE/TAG_KIND", summary["map_kind_counts"], total)

    samples = summary.get("recent_samples") or []
    if samples:
        st.markdown("#### 최근 FAIL 샘플")
        st.dataframe(
            [
                {
                    "SQL_ID": item.get("sql_id"),
                    "SPACE_NM": item.get("space_nm"),
                    "STAGE": item.get("fail_stage"),
                    "LOG_TYPE": item.get("log_type"),
                    "MAP": item.get("map_kind"),
                    "LEN": item.get("length_bucket"),
                    "UPD_TS": item.get("upd_ts"),
                    "LOG": item.get("log"),
                }
                for item in samples[:30]
            ],
            hide_index=True,
            width="stretch",
            height=360,
        )


def _ensure_chat_state() -> dict:
    if "fail_analysis_chat" not in st.session_state:
        st.session_state.fail_analysis_chat = _new_chat()
    if "fail_chat_pending_response" not in st.session_state:
        st.session_state.fail_chat_pending_response = False
    if "fail_chat_pending_id" not in st.session_state:
        st.session_state.fail_chat_pending_id = None
    return st.session_state.fail_analysis_chat


def _render_chat(agent: str) -> None:
    chat = _ensure_chat_state()
    label = _AGENT_LABELS.get(agent, agent)

    st.markdown("### Supervisor 챗봇")
    st.caption("종합 분석 버튼을 누르거나 직접 질문하면 Supervisor 도구로 FAIL 데이터를 조회합니다.")

    q1, q2 = st.columns(2)
    quick_prompt = None
    with q1:
        if st.button("SQL Conversion Fail 종합 분석", width="stretch", type="primary"):
            quick_prompt = "최근 SQL Conversion Fail 원인 종합 분석해줘."
    with q2:
        if st.button("SQL Tuning Fail 종합 분석", width="stretch"):
            quick_prompt = "최근 SQL Tuning Fail 원인 종합 분석해줘."

    with st.expander("대화 기록", expanded=False):
        if st.button("새 대화", width="stretch", key="fail_new_chat"):
            st.session_state.fail_analysis_chat = _new_chat()
            st.rerun()
        chats = _list_chats()
        for c in chats[:12]:
            cols = st.columns([5, 1])
            with cols[0]:
                if st.button(c.get("title", "대화")[:24], key=f"fail_chat_{c['id']}", width="stretch"):
                    loaded = _load_chat(c["id"])
                    if loaded:
                        st.session_state.fail_analysis_chat = loaded
                        st.rerun()
            with cols[1]:
                if st.button("삭제", key=f"fail_del_{c['id']}"):
                    _delete_chat(c["id"])
                    st.rerun()

    msg_container = st.container(height=520)
    with msg_container:
        if not chat["messages"]:
            st.info(f"{label} 실패 원인을 종합 분석하려면 아래 버튼이나 입력창을 사용하세요.")
        for msg in chat["messages"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
        if (
            st.session_state.fail_chat_pending_response
            and st.session_state.fail_chat_pending_id == chat["id"]
        ):
            with st.chat_message("assistant"):
                placeholder = st.empty()
                placeholder.markdown("분석중...")
                try:
                    answer = _call_llm(chat["messages"], supervisor_mode=True)
                except Exception as exc:
                    answer = f"LLM 호출 실패: {exc}"
                placeholder.markdown(answer)
            chat["messages"].append({"role": "assistant", "content": answer})
            _save_chat(chat)
            st.session_state.fail_analysis_chat = chat
            st.session_state.fail_chat_pending_response = False
            st.session_state.fail_chat_pending_id = None
            st.rerun()

    user_input = st.chat_input("FAIL 원인 분석 질문을 입력하세요...", key="fail_analysis_chat_input")
    selected_input = quick_prompt or user_input
    if selected_input and selected_input.strip():
        user_text = selected_input.strip()
        chat["messages"].append({"role": "user", "content": user_text})
        if chat["title"] == "새 대화":
            chat["title"] = user_text[:24]
        _save_chat(chat)
        st.session_state.fail_analysis_chat = chat
        st.session_state.fail_chat_pending_response = True
        st.session_state.fail_chat_pending_id = chat["id"]
        st.rerun()


def render():
    st.markdown(
        """
        <style>
        .fail-metric {
            min-height: 104px;
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            background: #ffffff;
            padding: 14px 14px 12px 14px;
            overflow-wrap: anywhere;
            word-break: break-word;
        }
        .fail-metric-compact {
            padding-left: 12px;
            padding-right: 12px;
        }
        .fail-metric-label {
            font-size: 12px;
            line-height: 1.25;
            color: #6b7280;
            font-weight: 700;
            margin-bottom: 8px;
        }
        .fail-metric-value {
            font-size: 22px;
            line-height: 1.22;
            color: #111827;
            font-weight: 800;
            letter-spacing: 0;
        }
        .fail-metric-sub {
            min-height: 18px;
            margin-top: 8px;
            font-size: 12px;
            line-height: 1.25;
            color: #6b7280;
        }
        .fail-metric-compact .fail-metric-value {
            font-size: 30px;
            line-height: 1.1;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.markdown("## FAIL Analysis")
    st.caption("SQL Conversion / SQL Tuning 실패 원인을 stage, log, 길이, 유형별로 크게 확인합니다.")

    top = st.columns([2.3, 0.95, 0.8], gap="large")
    with top[0]:
        agent_label = st.radio(
            "분석 대상",
            list(_AGENT_OPTIONS.keys()),
            index=_default_agent_index(),
            horizontal=True,
        )
    with top[1]:
        limit = st.number_input("분석 건수", min_value=50, max_value=1000, value=200, step=50)
    with top[2]:
        st.markdown("<div style='height: 28px'></div>", unsafe_allow_html=True)
        if st.button("새로고침", width="stretch"):
            st.rerun()

    st.markdown("<div style='height: 8px'></div>", unsafe_allow_html=True)

    agent = _AGENT_OPTIONS[agent_label]
    st.query_params["agent"] = agent

    left, right = st.columns([1.35, 1], gap="large")
    with left:
        _render_summary(agent, int(limit))
    with right:
        _render_chat(agent)
