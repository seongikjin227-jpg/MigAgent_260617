import json
import re

import pandas as pd
import streamlit as st

from utils.db import get_sql_job_full, get_sql_jobs


ALL = "전체"
_COLS_TABLE = [
    "SQL_ID",
    "SPACE_NM",
    "TAG_KIND",
    "STATUS",
    "TUNED_TEST",
    "SQL_LENGTH",
    "MAP_TYPE",
    "EFFECTIVE_FR_SQL_LEN",
    "TO_SQL_LEN",
    "TARGET_TABLE",
    "UPD_TS",
]

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
    "TOBE CORRECT SQL": "TOBE_CORRECT_SQL",
    "BIND CORRECT SQL": "BIND_CORRECT_SQL",
    "TEST CORRECT SQL": "TEST_CORRECT_SQL",
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


def _prepare_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    required = [
        "ROW_ID",
        "SQL_ID",
        "SPACE_NM",
        "TAG_KIND",
        "STATUS",
        "TUNED_TEST",
        "SQL_LENGTH",
        "MAP_TYPE",
        "TARGET_TABLE",
        "FR_SQL_TEXT",
        "EDIT_FR_SQL",
        "TO_SQL_TEXT",
        "TUNED_SQL",
        "FORMATTED_SQL",
        "LOG",
    ]
    for col in required:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    for col in ("FR_SQL_LEN", "EDIT_FR_SQL_LEN", "EFFECTIVE_FR_SQL_LEN", "TO_SQL_LEN", "TUNED_SQL_LEN", "FORMATTED_SQL_LEN"):
        if col not in df.columns:
            df[col] = 0
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype(int)
    return df


def _options(df: pd.DataFrame, column: str) -> list[str]:
    values = [v for v in df[column].dropna().astype(str).str.strip().unique().tolist() if v]
    return [ALL] + sorted(values)


def _contains(series: pd.Series, keyword: str) -> pd.Series:
    keyword = keyword.strip()
    if not keyword:
        return pd.Series(True, index=series.index)
    return series.fillna("").astype(str).str.contains(keyword, case=False, na=False, regex=False)


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


def _render_detail_stack(detail: dict, labels: list[str], side: str) -> None:
    if not labels:
        st.info("선택된 컬럼 없음")
        return

    for label in labels:
        column = _SQL_DETAIL_OPTIONS[label]
        value = detail.get(column) or ""
        st.caption(label)
        if column == "LOG":
            st.text_area(
                f"{label} 내용",
                value or "(없음)",
                height=180,
                label_visibility="collapsed",
                key=f"sql_monitor_detail_{side}_{label}",
            )
        else:
            formatted, language = _format_detail_for_display(column, value)
            st.code(formatted, language=language)


def render():
    st.title("SQL Agent Monitor")

    if st.button("새로고침"):
        st.rerun()

    try:
        jobs = get_sql_jobs()
    except Exception as e:
        st.error(f"DB 연결 실패: {e}")
        return

    if not jobs:
        st.info("조회할 작업이 없습니다.")
        return

    df_all = _prepare_df(jobs)

    with st.expander("검색 / 필터", expanded=True):
        c1, c2, c3, c4 = st.columns([1.4, 1.4, 1, 1])
        with c1:
            sql_id_query = st.text_input("SQL_ID LIKE", placeholder="예: SEL_001")
        with c2:
            namespace_query = st.text_input("Namespace LIKE", placeholder="예: userMapper")
        with c3:
            target_query = st.text_input("TARGET_TABLE LIKE", placeholder="예: CUSTOMER")
        with c4:
            any_sql_query = st.text_input("SQL/LOG 본문 LIKE", placeholder="FROM, JOIN, 오류 메시지")

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            sel_status = st.selectbox("STATUS", _options(df_all, "STATUS"))
        with c2:
            sel_tuned = st.selectbox("TUNED_TEST", _options(df_all, "TUNED_TEST"))
        with c3:
            sel_map_type = st.selectbox("MAP_TYPE / map_kind", _options(df_all, "MAP_TYPE"))
        with c4:
            sel_sql_length = st.selectbox("SQL_LENGTH", _options(df_all, "SQL_LENGTH"))
        with c5:
            sel_tag_kind = st.selectbox("TAG_KIND", _options(df_all, "TAG_KIND"))

        presence = st.multiselect(
            "생성/로그 여부",
            ["TOBE SQL 있음", "TUNED SQL 있음", "FORMATTED SQL 있음", "LOG 있음"],
        )

    df = df_all.copy()
    df = df[_contains(df["SQL_ID"], sql_id_query)]
    df = df[_contains(df["SPACE_NM"], namespace_query)]
    df = df[_contains(df["TARGET_TABLE"], target_query)]

    if any_sql_query.strip():
        fields = (
            df["FR_SQL_TEXT"]
            + "\n"
            + df["EDIT_FR_SQL"]
            + "\n"
            + df["TO_SQL_TEXT"]
            + "\n"
            + df["TUNED_SQL"]
            + "\n"
            + df["FORMATTED_SQL"]
            + "\n"
            + df["LOG"]
        )
        df = df[_contains(fields, any_sql_query)]

    if sel_status != ALL:
        df = df[df["STATUS"] == sel_status]
    if sel_tuned != ALL:
        df = df[df["TUNED_TEST"] == sel_tuned]
    if sel_map_type != ALL:
        df = df[df["MAP_TYPE"] == sel_map_type]
    if sel_sql_length != ALL:
        df = df[df["SQL_LENGTH"] == sel_sql_length]
    if sel_tag_kind != ALL:
        df = df[df["TAG_KIND"] == sel_tag_kind]
    if "TOBE SQL 있음" in presence:
        df = df[df["TO_SQL_TEXT"].str.strip() != ""]
    if "TUNED SQL 있음" in presence:
        df = df[df["TUNED_SQL"].str.strip() != ""]
    if "FORMATTED SQL 있음" in presence:
        df = df[df["FORMATTED_SQL"].str.strip() != ""]
    if "LOG 있음" in presence:
        df = df[df["LOG"].str.strip() != ""]

    show_cols = [c for c in _COLS_TABLE if c in df.columns]
    with st.expander(f"검색 결과 표 ({len(df)}건 / 전체 {len(df_all)}건)", expanded=False):
        st.dataframe(df[show_cols], width="stretch", hide_index=True)

    st.divider()
    st.subheader("SQL 상세 조회")

    if df.empty:
        st.warning("조건에 맞는 SQL Job이 없습니다.")
        return

    row_ids = df["ROW_ID"].tolist()
    labels = [
        f"{r['SPACE_NM']} / {r['SQL_ID']} | STATUS={r['STATUS'] or 'NULL'} | MAP_TYPE={r['MAP_TYPE'] or '-'} | LEN={r['EFFECTIVE_FR_SQL_LEN']}"
        for _, r in df.iterrows()
    ]
    idx = st.selectbox("목록 선택", range(len(labels)), format_func=lambda i: labels[i])

    sel_row_id = row_ids[idx]
    row = next((j for j in jobs if j["ROW_ID"] == sel_row_id), None)
    if not row:
        return

    detail = get_sql_job_full(sel_row_id) or row

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("STATUS", detail.get("STATUS") or "-")
    with m2:
        st.metric("TUNED_TEST", detail.get("TUNED_TEST") or "-")
    with m3:
        st.metric("SQL_LENGTH", detail.get("SQL_LENGTH") or row.get("SQL_LENGTH") or "-")
    with m4:
        st.metric("MAP_TYPE", detail.get("MAP_TYPE") or row.get("MAP_TYPE") or "-")

    with st.expander("로그", expanded=True):
        log = detail.get("LOG") or ""
        if log:
            st.text_area("LOG", log, height=200, label_visibility="collapsed")
        else:
            st.info("로그 없음")

    st.subheader("SQL 컬럼 비교")
    option_labels = list(_SQL_DETAIL_OPTIONS.keys())
    picker_options = [_ALL_DETAIL_COLUMNS] + option_labels
    left_picker, right_picker = st.columns(2)
    with left_picker:
        left_labels = st.multiselect(
            "왼쪽 컬럼",
            picker_options,
            default=["ASIS SQL"],
            key="sql_monitor_left_col",
        )
    with right_picker:
        right_labels = st.multiselect(
            "오른쪽 컬럼",
            picker_options,
            default=["TOBE SQL"],
            key="sql_monitor_right_col",
        )

    col1, col2 = st.columns(2)
    with col1:
        _render_detail_stack(detail, _selected_detail_labels(left_labels), "left")
    with col2:
        _render_detail_stack(detail, _selected_detail_labels(right_labels), "right")
