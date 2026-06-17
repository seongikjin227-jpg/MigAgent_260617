import json
import re

import pandas as pd
import streamlit as st

from utils.db import get_mig_dtl, get_mig_jobs, get_mig_logs, get_sql_job_full, get_sql_jobs


_SQL_DETAIL_OPTIONS = {
    "ASIS SQL": "FR_SQL_TEXT",
    "EDIT ASIS SQL": "EDIT_FR_SQL",
    "TOBE SQL": "TO_SQL_TEXT",
    "BIND SQL": "BIND_SQL",
    "BIND SET": "BIND_SET",
    "TEST SQL": "TEST_SQL",
    "TUNED SQL": "TUNED_SQL",
    "TUNED RESULT": "TUNED_RESULT",
    "FORMATTED SQL": "FORMATTED_SQL",
    "BLOCK RAG CONTENT": "BLOCK_RAG_CONTENT",
    "LOG": "LOG",
}

_ALL_DETAIL_COLUMNS = "전체"
_RAW_SQL_COLUMNS = {"FORMATTED_SQL"}
_JSON_DETAIL_COLUMNS = {"BIND_SET", "BLOCK_RAG_CONTENT", "TUNED_RESULT"}

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


def _format_json_for_display(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return "(없음)"
    try:
        return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
    except Exception:
        return None


def _format_detail_for_display(column: str, value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "(없음)", "text"
    if column in _RAW_SQL_COLUMNS:
        return text, "sql"
    if column in _JSON_DETAIL_COLUMNS:
        formatted_json = _format_json_for_display(text)
        if formatted_json is not None:
            return formatted_json, "json"
    return _format_sql_for_display(text), "sql"


def _selected_detail_labels(selected: list[str]) -> list[str]:
    if _ALL_DETAIL_COLUMNS in selected:
        return list(_SQL_DETAIL_OPTIONS.keys())
    return selected


def _render_sql_detail_stack(job: dict, labels: list[str], side: str) -> None:
    if not labels:
        st.info("선택된 컬럼 없음")
        return

    for label in labels:
        column = _SQL_DETAIL_OPTIONS[label]
        value = job.get(column) or ""
        st.caption(label)
        if column == "LOG":
            st.text_area(
                f"{label} 내용",
                value or "(없음)",
                height=180,
                label_visibility="collapsed",
                key=f"job_detail_sql_{side}_{label}",
            )
        else:
            formatted, language = _format_detail_for_display(column, value)
            st.code(formatted, language=language)


def render():
    st.title("Job Detail")

    tab_mig, tab_sql = st.tabs(["Mig Job", "SQL Job"])

    with tab_mig:
        _render_mig_job_detail()

    with tab_sql:
        _render_sql_job_detail()


def _render_mig_job_detail():
    try:
        jobs = get_mig_jobs()
    except Exception as e:
        st.error(f"DB 연결 실패: {e}")
        return

    if not jobs:
        st.info("데이터 없음")
        return

    df = pd.DataFrame(jobs)
    labels = [
        f"MAP_ID={r['MAP_ID']} | {r['FR_TABLE']} -> {r['TO_TABLE']} | {r['STATUS']}"
        for _, r in df.iterrows()
    ]
    idx = st.selectbox(
        "Mig Job 선택",
        range(len(labels)),
        format_func=lambda i: labels[i],
        key="mig_sel",
    )

    row = jobs[idx]
    map_id = int(row["MAP_ID"])

    st.subheader("기본 정보")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.metric("MAP_ID", row["MAP_ID"])
        st.write(f"**FR_TABLE:** {row.get('FR_TABLE')}")
        st.write(f"**TO_TABLE:** {row.get('TO_TABLE')}")
    with c2:
        st.metric("STATUS", row.get("STATUS") or "-")
        st.write(f"**PRIORITY:** {row.get('PRIORITY')}")
        st.write(f"**MAP_TYPE:** {row.get('MAP_TYPE')}")
    with c3:
        st.metric("ELAPSED", f"{row.get('ELAPSED_SECONDS') or 0}s")
        st.write(f"**RETRY_COUNT:** {row.get('RETRY_COUNT')}")
        st.write(f"**BATCH_CNT:** {row.get('BATCH_CNT')}")

    st.divider()

    st.subheader("SQL 전체 흐름")
    t1, t2 = st.tabs(["MIG_SQL", "VERIFY_SQL"])
    with t1:
        st.code(row.get("MIG_SQL") or "(없음)", language="sql")
    with t2:
        st.code(row.get("VERIFY_SQL") or "(없음)", language="sql")

    with st.expander("컬럼 매핑 (NEXT_MIG_INFO_DTL)"):
        dtl = get_mig_dtl(map_id)
        st.dataframe(pd.DataFrame(dtl) if dtl else pd.DataFrame(), width="stretch")

    st.subheader("실행 로그")
    logs = get_mig_logs(map_id)
    if logs:
        st.dataframe(pd.DataFrame(logs), width="stretch", hide_index=True)
    else:
        st.info("로그 없음")


def _render_sql_job_detail():
    try:
        jobs = get_sql_jobs()
    except Exception as e:
        st.error(f"DB 연결 실패: {e}")
        return

    if not jobs:
        st.info("SQL Job 데이터 없음")
        return

    df = pd.DataFrame(jobs)
    for col in ("STATUS", "TUNED_TEST", "SQL_ID", "SPACE_NM"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    filter_cols = st.columns([1.2, 1.2, 2, 2])
    with filter_cols[0]:
        status_options = ["전체"] + sorted([v for v in df["STATUS"].unique().tolist() if v])
        sel_status = st.selectbox("STATUS", status_options, key="sql_job_detail_status")
    with filter_cols[1]:
        tuned_status_options = ["전체"] + sorted([v for v in df["TUNED_TEST"].unique().tolist() if v])
        sel_tuned_status = st.selectbox("TUNED_TEST", tuned_status_options, key="sql_job_detail_tuned_status")
    with filter_cols[2]:
        sql_id_query = st.text_input("SQL_ID 검색", placeholder="예: selectUser", key="sql_job_detail_sql_id")
    with filter_cols[3]:
        namespace_query = st.text_input("Namespace 검색", placeholder="예: userMapper", key="sql_job_detail_namespace")

    filtered = df.copy()
    if sel_status != "전체":
        filtered = filtered[filtered["STATUS"] == sel_status]
    if sel_tuned_status != "전체":
        filtered = filtered[filtered["TUNED_TEST"] == sel_tuned_status]
    if sql_id_query.strip():
        filtered = filtered[filtered["SQL_ID"].str.contains(sql_id_query.strip(), case=False, na=False)]
    if namespace_query.strip():
        filtered = filtered[filtered["SPACE_NM"].str.contains(namespace_query.strip(), case=False, na=False)]

    st.caption(f"검색 결과 {len(filtered)}건 / 전체 {len(df)}건")

    row_id_input = ""
    with st.expander("ROW_ID로 직접 조회"):
        row_id_input = st.text_input("ROW_ID 입력 (ROWIDTOCHAR)", placeholder="예: AAABBBCCCDDDEEEF")

    selected_row_id = row_id_input.strip()
    if not selected_row_id:
        if filtered.empty:
            st.warning("조건에 맞는 SQL Job이 없습니다.")
            return
        filtered_records = filtered.to_dict("records")
        labels = [
            (
                f"{r.get('SPACE_NM') or '-'}.{r.get('SQL_ID') or '-'} "
                f"| STATUS={r.get('STATUS') or 'NULL'} "
                f"| TUNED_TEST={r.get('TUNED_TEST') or 'NULL'} "
                f"| UPD_TS={r.get('UPD_TS') or '-'}"
            )
            for r in filtered_records
        ]
        selected_idx = st.selectbox(
            "SQL Job 선택",
            range(len(labels)),
            format_func=lambda i: labels[i],
            key="sql_job_detail_selected_idx",
        )
        selected_row_id = str(filtered_records[selected_idx]["ROW_ID"])

    job = get_sql_job_full(selected_row_id)
    if not job:
        st.warning("해당 ROW_ID의 데이터를 찾을 수 없습니다.")
        return

    c1, c2, c3 = st.columns(3)
    with c1:
        st.write(f"**SQL_ID:** {job.get('SQL_ID')}")
        st.write(f"**SPACE_NM:** {job.get('SPACE_NM')}")
    with c2:
        st.metric("STATUS", job.get("STATUS") or "-")
        st.write(f"**EDITED_YN:** {job.get('EDITED_YN')}")
    with c3:
        st.write(f"**TARGET_TABLE:** {job.get('TARGET_TABLE')}")
        st.write(f"**UPD_TS:** {job.get('UPD_TS')}")

    st.divider()
    st.subheader("SQL 컬럼 비교")

    left_picker, right_picker = st.columns(2)
    option_labels = list(_SQL_DETAIL_OPTIONS.keys())
    picker_options = [_ALL_DETAIL_COLUMNS] + option_labels
    with left_picker:
        left_labels = st.multiselect(
            "왼쪽 컬럼",
            picker_options,
            default=["ASIS SQL"],
            key="sql_detail_left_col",
        )
    with right_picker:
        right_labels = st.multiselect(
            "오른쪽 컬럼",
            picker_options,
            default=["TOBE SQL"],
            key="sql_detail_right_col",
        )

    left_col, right_col = st.columns(2)
    with left_col:
        _render_sql_detail_stack(job, _selected_detail_labels(left_labels), "left")
    with right_col:
        _render_sql_detail_stack(job, _selected_detail_labels(right_labels), "right")
