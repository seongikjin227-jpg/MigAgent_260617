import json
import re

import pandas as pd
import streamlit as st

from utils.db import get_sql_jobs


ALL = "전체"
_COLS_TABLE = [
    "ROW_ID",
    "SPACE_NM",
    "SQL_ID",
    "STATUS",
    "TUNED_TEST",
    "SQL_LENGTH",
    "MAP_TYPE",
    "EFFECTIVE_FR_SQL_LEN",
    "TUNED_SQL_LEN",
    "FORMATTED_SQL_LEN",
    "UPD_TS",
]

_TUNING_DETAIL_OPTIONS = {
    "TOBE SQL": "TO_SQL_TEXT",
    "TUNED SQL": "TUNED_SQL",
    "FORMATTED SQL": "FORMATTED_SQL",
    "TUNED RESULT": "TUNED_RESULT",
    "BLOCK RAG CONTENT": "BLOCK_RAG_CONTENT",
    "LOG": "LOG",
}

_ALL_DETAIL_COLUMNS = "전체"
_RAW_SQL_COLUMNS = {"FORMATTED_SQL"}
_JSON_DETAIL_COLUMNS = {"BLOCK_RAG_CONTENT", "TUNED_RESULT"}

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
        return list(_TUNING_DETAIL_OPTIONS.keys())
    return selected


def _render_detail_stack(row: dict, labels: list[str], side: str) -> None:
    if not labels:
        st.info("선택된 컬럼 없음")
        return

    for label in labels:
        column = _TUNING_DETAIL_OPTIONS[label]
        value = row.get(column) or ""
        st.caption(label)
        if column == "LOG":
            st.text_area(
                f"{label} 내용",
                value or "(없음)",
                height=180,
                label_visibility="collapsed",
                key=f"tuning_monitor_detail_{side}_{label}",
            )
        else:
            formatted, language = _format_detail_for_display(column, value)
            st.code(formatted, language=language)


def _label(row: pd.Series) -> str:
    namespace = row.get("SPACE_NM") or "-"
    sql_id = row.get("SQL_ID") or "-"
    tuned_test = row.get("TUNED_TEST") or "-"
    tuned_result = "NO TUNING" if _is_no_tuning(row.get("TUNED_RESULT")) else "TUNED"
    return f"{namespace} / {sql_id} | TUNED_TEST={tuned_test} | {tuned_result}"


def _prepare_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    required = [
        "ROW_ID",
        "SQL_ID",
        "SPACE_NM",
        "TAG_KIND",
        "STATUS",
        "TUNED_TEST",
        "TUNED_RESULT",
        "BLOCK_RAG_CONTENT",
        "SQL_LENGTH",
        "MAP_TYPE",
        "TARGET_TABLE",
        "TO_SQL_TEXT",
        "TUNED_SQL",
        "FORMATTED_SQL",
        "LOG",
    ]
    for col in required:
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)

    for col in ("EFFECTIVE_FR_SQL_LEN", "TO_SQL_LEN", "TUNED_SQL_LEN", "FORMATTED_SQL_LEN"):
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


def _is_no_tuning(value: object) -> bool:
    return "NO TUNING" in str(value or "").upper()


def _apply_length_filter(df: pd.DataFrame, preset: str, min_len: int, max_len: int) -> pd.DataFrame:
    length = df["EFFECTIVE_FR_SQL_LEN"]
    if preset == "5000 이하":
        df = df[length <= 5000]
        length = df["EFFECTIVE_FR_SQL_LEN"]
    elif preset == "5000 초과":
        df = df[length > 5000]
        length = df["EFFECTIVE_FR_SQL_LEN"]

    if min_len > 0:
        df = df[length >= min_len]
        length = df["EFFECTIVE_FR_SQL_LEN"]
    if max_len > 0:
        df = df[length <= max_len]
    return df


def render():
    st.title("Tuning Agent Monitor")

    if st.button("새로고침"):
        st.rerun()

    try:
        all_jobs = get_sql_jobs()
    except Exception as e:
        st.error(f"DB 연결 실패: {e}")
        return

    jobs = [j for j in all_jobs if j.get("TUNED_SQL") or j.get("TUNED_TEST") or j.get("TUNED_RESULT")]

    if not jobs:
        st.info("튜닝 대상 작업이 없습니다.")
        return

    df_all = _prepare_df(jobs)

    with st.expander("검색 / 필터", expanded=True):
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            sql_id_query = st.text_input("SQL_ID LIKE")
        with c2:
            namespace_query = st.text_input("Namespace LIKE")
        with c3:
            target_query = st.text_input("TARGET_TABLE LIKE")
        with c4:
            tuned_result_query = st.text_input("TUNED_RESULT LIKE", placeholder="예: 인덱스, 조인, NO TUNING")

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            block_rag_query = st.text_input("BLOCK_RAG_CONTENT LIKE", placeholder="적용 후보 룰 검색")
        with c2:
            sql_text_query = st.text_input("SQL 본문 LIKE", placeholder="TOBE/TUNED SQL 검색")
        with c3:
            log_query = st.text_input("LOG LIKE", placeholder="검증 오류 검색")
        with c4:
            result_kind = st.selectbox("튜닝 결과", [ALL, "튜닝 적용", "NO TUNING", "TUNED_RESULT 있음", "TUNED_RESULT 없음"])

        c1, c2, c3, c4, c5 = st.columns(5)
        with c1:
            sel_status = st.selectbox("STATUS", _options(df_all, "STATUS"))
        with c2:
            sel_tune = st.selectbox("TUNED_TEST", _options(df_all, "TUNED_TEST"))
        with c3:
            sel_map_type = st.selectbox("MAP_TYPE / map_kind", _options(df_all, "MAP_TYPE"))
        with c4:
            sel_sql_length = st.selectbox("SQL_LENGTH", _options(df_all, "SQL_LENGTH"))
        with c5:
            sel_tag_kind = st.selectbox("TAG_KIND", _options(df_all, "TAG_KIND"))

        c1, c2, c3, c4 = st.columns(4)
        with c1:
            length_preset = st.selectbox("ASIS 길이 프리셋", [ALL, "5000 이하", "5000 초과"])
        with c2:
            min_len = st.number_input("ASIS 길이 이상", min_value=0, value=0, step=100)
        with c3:
            max_len = st.number_input("ASIS 길이 이하", min_value=0, value=0, step=100)
        with c4:
            output_presence = st.multiselect(
                "산출물 여부",
                ["TUNED SQL 있음", "FORMATTED SQL 있음", "FORMATTED SQL 없음"],
            )

    df = df_all.copy()
    df = df[_contains(df["SQL_ID"], sql_id_query)]
    df = df[_contains(df["SPACE_NM"], namespace_query)]
    df = df[_contains(df["TARGET_TABLE"], target_query)]
    df = df[_contains(df["TUNED_RESULT"], tuned_result_query)]
    df = df[_contains(df["BLOCK_RAG_CONTENT"], block_rag_query)]
    df = df[_contains(df["LOG"], log_query)]

    if sql_text_query.strip():
        sql_fields = df["TO_SQL_TEXT"] + "\n" + df["TUNED_SQL"] + "\n" + df["FORMATTED_SQL"]
        df = df[_contains(sql_fields, sql_text_query)]

    if result_kind == "튜닝 적용":
        df = df[(df["TUNED_RESULT"].str.strip() != "") & (~df["TUNED_RESULT"].map(_is_no_tuning))]
    elif result_kind == "NO TUNING":
        df = df[df["TUNED_RESULT"].map(_is_no_tuning)]
    elif result_kind == "TUNED_RESULT 있음":
        df = df[df["TUNED_RESULT"].str.strip() != ""]
    elif result_kind == "TUNED_RESULT 없음":
        df = df[df["TUNED_RESULT"].str.strip() == ""]

    if sel_status != ALL:
        df = df[df["STATUS"] == sel_status]
    if sel_tune != ALL:
        df = df[df["TUNED_TEST"] == sel_tune]
    if sel_map_type != ALL:
        df = df[df["MAP_TYPE"] == sel_map_type]
    if sel_sql_length != ALL:
        df = df[df["SQL_LENGTH"] == sel_sql_length]
    if sel_tag_kind != ALL:
        df = df[df["TAG_KIND"] == sel_tag_kind]

    df = _apply_length_filter(df, length_preset, int(min_len), int(max_len))

    if "TUNED SQL 있음" in output_presence:
        df = df[df["TUNED_SQL"].str.strip() != ""]
    if "FORMATTED SQL 있음" in output_presence:
        df = df[df["FORMATTED_SQL"].str.strip() != ""]
    if "FORMATTED SQL 없음" in output_presence:
        df = df[df["FORMATTED_SQL"].str.strip() == ""]

    show_cols = [c for c in _COLS_TABLE if c in df.columns]
    st.caption(f"검색 결과 {len(df)}건 / 전체 {len(df_all)}건")
    st.dataframe(df[show_cols], width="stretch", hide_index=True)

    st.divider()
    st.subheader("튜닝 결과 비교")

    if df.empty:
        st.warning("조건에 맞는 튜닝 작업이 없습니다.")
        return

    row_ids = df["ROW_ID"].tolist()
    labels = [_label(r) for _, r in df.iterrows()]
    idx = st.selectbox("목록 선택", range(len(labels)), format_func=lambda i: labels[i])

    sel_row_id = row_ids[idx]
    row = next((j for j in jobs if j["ROW_ID"] == sel_row_id), None)
    if not row:
        return

    st.markdown("#### TUNED_RESULT")
    tuned_result = row.get("TUNED_RESULT") or "(없음)"
    if tuned_result == "NO TUNING":
        st.info(tuned_result)
    else:
        st.text_area(
            "TUNED_RESULT",
            tuned_result,
            height=140,
            label_visibility="collapsed",
        )

    with st.expander("BLOCK_RAG_CONTENT", expanded=False):
        block_rag_content = row.get("BLOCK_RAG_CONTENT") or "(없음)"
        st.text_area(
            "BLOCK_RAG_CONTENT",
            block_rag_content,
            height=260,
            label_visibility="collapsed",
        )

    st.subheader("컬럼 비교")
    detail_labels = list(_TUNING_DETAIL_OPTIONS.keys())
    picker_options = [_ALL_DETAIL_COLUMNS] + detail_labels
    left_picker, right_picker = st.columns(2)
    with left_picker:
        left_labels = st.multiselect(
            "왼쪽 컬럼",
            picker_options,
            default=["TOBE SQL"],
            key="tuning_monitor_left_col",
        )
    with right_picker:
        right_labels = st.multiselect(
            "오른쪽 컬럼",
            picker_options,
            default=["TUNED SQL"],
            key="tuning_monitor_right_col",
        )

    c1, c2 = st.columns(2)
    with c1:
        _render_detail_stack(row, _selected_detail_labels(left_labels), "left")
    with c2:
        _render_detail_stack(row, _selected_detail_labels(right_labels), "right")

    c_test, c_log = st.columns(2)
    with c_test:
        st.write(f"**최종 검증:** {row.get('TUNED_TEST') or '-'}")
    with c_log:
        log = row.get("LOG") or ""
        if log:
            with st.expander("실패 로그"):
                st.text(log[:2000])
