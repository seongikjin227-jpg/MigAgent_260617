import re

import pandas as pd
import streamlit as st

from utils.db import get_mig_dtl, get_mig_jobs, get_mig_logs

_COLS_TABLE = [
    "MAP_ID",
    "STATUS",
    "FR_TABLE",
    "TO_TABLE",
    "USE_YN",
    "TRUNC_YN",
    "USER_EDITED",
    "PRIORITY",
    "PRIOR_MAP_ID",
    "RETRY_COUNT",
    "ELAPSED_SECONDS",
    "UPD_TS",
]

_MIG_DETAIL_OPTIONS = {
    "MIG SQL": "MIG_SQL",
    "VERIFY SQL": "VERIFY_SQL",
    "LOG": "__LOG__",
}

_ALL_DETAIL_COLUMNS = "전체"

_SQL_BREAK_KEYWORDS = (
    "SELECT",
    "INSERT INTO",
    "UPDATE",
    "DELETE FROM",
    "FROM",
    "WHERE",
    "GROUP BY",
    "ORDER BY",
    "HAVING",
    "UNION ALL",
    "UNION",
    "INNER JOIN",
    "LEFT OUTER JOIN",
    "RIGHT OUTER JOIN",
    "FULL OUTER JOIN",
    "LEFT JOIN",
    "RIGHT JOIN",
    "FULL JOIN",
    "JOIN",
    "AND",
    "OR",
    "VALUES",
    "SET",
)


def _format_sql_for_display(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "(없음)"

    compact = " ".join(text.replace("\r", "\n").split())
    keyword_pattern = "|".join(
        re.escape(keyword) for keyword in sorted(_SQL_BREAK_KEYWORDS, key=len, reverse=True)
    )
    compact = re.sub(
        rf"(?i)\b({keyword_pattern})\b",
        lambda match: f"\n{match.group(0).upper()}",
        compact,
    )

    compact = compact.replace(", ", ",\n  ")
    lines = [line.strip() for line in compact.splitlines() if line.strip()]
    return "\n".join(lines) if lines else text


def _selected_detail_labels(selected: list[str]) -> list[str]:
    if _ALL_DETAIL_COLUMNS in selected:
        return list(_MIG_DETAIL_OPTIONS.keys())
    return selected


def _selected_row_position(table_event) -> int | None:
    selection = getattr(table_event, "selection", None)
    if selection is None and isinstance(table_event, dict):
        selection = table_event.get("selection")
    if not selection:
        return None

    rows = getattr(selection, "rows", None)
    if rows is None and isinstance(selection, dict):
        rows = selection.get("rows")
    if not rows:
        return None
    return int(rows[0])


def _render_detail_stack(row: dict, labels: list[str], logs: list[dict]) -> None:
    if not labels:
        st.info("선택한 컬럼 없음")
        return

    for label in labels:
        column = _MIG_DETAIL_OPTIONS[label]
        st.caption(label)
        if column == "__LOG__":
            if logs:
                st.dataframe(pd.DataFrame(logs), width="stretch", hide_index=True)
            else:
                st.info("로그 없음")
        else:
            st.code(_format_sql_for_display(row.get(column)), language="sql")


def render():
    st.title("Mig Agent Monitor")

    if st.button("새로고침"):
        st.rerun()

    try:
        jobs = get_mig_jobs()
    except Exception as e:
        st.error(f"DB 연결 실패: {e}")
        return

    if not jobs:
        st.info("조회된 작업이 없습니다.")
        return

    df_all = pd.DataFrame(jobs)

    with st.expander("검색 / 필터", expanded=True):
        c1, c2, c3 = st.columns(3)
        with c1:
            keyword = st.text_input("MAP_ID 검색", placeholder="예: 1")
        with c2:
            statuses = ["전체"] + sorted(df_all["STATUS"].dropna().unique().tolist())
            sel_status = st.selectbox("STATUS", statuses)
        with c3:
            use_opts = ["전체"] + sorted(df_all["USE_YN"].dropna().unique().tolist())
            sel_use = st.selectbox("USE_YN", use_opts)

    df = df_all.copy()
    if keyword:
        df = df[df["MAP_ID"].astype(str).str.contains(keyword, case=False)]
    if sel_status != "전체":
        df = df[df["STATUS"] == sel_status]
    if sel_use != "전체":
        df = df[df["USE_YN"] == sel_use]

    show_cols = [c for c in _COLS_TABLE if c in df.columns]
    st.write(f"**{len(df)}건** 조회됨")

    grid_df = df[show_cols].reset_index(drop=True)
    table_event = st.dataframe(
        grid_df,
        width="stretch",
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
        key="mig_monitor_grid",
    )

    selected_pos = _selected_row_position(table_event)
    if selected_pos is not None and selected_pos < len(grid_df):
        st.session_state["mig_monitor_selected_map_id"] = str(grid_df.iloc[selected_pos]["MAP_ID"])

    st.divider()
    st.subheader("작업 상세 조회")

    map_ids = df["MAP_ID"].astype(str).tolist()
    if not map_ids:
        return

    selected_from_grid = st.session_state.get("mig_monitor_selected_map_id")
    default_idx = map_ids.index(selected_from_grid) if selected_from_grid in map_ids else 0
    selected = st.selectbox("MAP_ID 선택", map_ids, index=default_idx)
    st.session_state["mig_monitor_selected_map_id"] = str(selected)

    row = next((j for j in jobs if str(j.get("MAP_ID")) == str(selected)), None)
    if not row:
        return

    map_id = int(selected)

    c1, c2 = st.columns(2)
    with c1:
        st.write(f"**FR_TABLE:** {row.get('FR_TABLE')}")
        st.write(f"**TO_TABLE:** {row.get('TO_TABLE')}")
        st.write(f"**STATUS:** {row.get('STATUS')}")
    with c2:
        st.write(f"**PRIORITY:** {row.get('PRIORITY')}")
        st.write(f"**RETRY_COUNT:** {row.get('RETRY_COUNT')}")
        st.write(f"**ELAPSED_SECONDS:** {row.get('ELAPSED_SECONDS')}s")

    st.subheader("컬럼 비교")
    detail_labels = list(_MIG_DETAIL_OPTIONS.keys())
    picker_options = [_ALL_DETAIL_COLUMNS] + detail_labels
    left_picker, right_picker = st.columns(2)
    with left_picker:
        left_labels = st.multiselect(
            "왼쪽 컬럼",
            picker_options,
            default=["MIG SQL"],
            key="mig_monitor_left_col",
        )
    with right_picker:
        right_labels = st.multiselect(
            "오른쪽 컬럼",
            picker_options,
            default=["VERIFY SQL"],
            key="mig_monitor_right_col",
        )

    logs = get_mig_logs(map_id)

    c_left, c_right = st.columns(2)
    with c_left:
        _render_detail_stack(row, _selected_detail_labels(left_labels), logs)
    with c_right:
        _render_detail_stack(row, _selected_detail_labels(right_labels), logs)

    with st.expander("컬럼 매핑 정보"):
        dtl = get_mig_dtl(map_id)
        if dtl:
            st.dataframe(pd.DataFrame(dtl), width="stretch", hide_index=True)
        else:
            st.info("매핑 정보 없음")
