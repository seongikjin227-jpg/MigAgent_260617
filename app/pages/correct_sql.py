import pandas as pd
import streamlit as st

from utils.db import get_sql_job_full, get_sql_jobs, update_sql_correct_sql


ALL = "전체"

_SQL_VIEW_OPTIONS = {
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
    "LOG": "LOG",
}

_CORRECT_KIND_OPTIONS = {
    "TOBE Correct SQL": ("TOBE", "TOBE_CORRECT_SQL"),
    "BIND Correct SQL": ("BIND", "BIND_CORRECT_SQL"),
    "TEST Correct SQL": ("TEST", "TEST_CORRECT_SQL"),
}


def _prepare_df(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for col in ("ROW_ID", "SQL_ID", "SPACE_NM", "STATUS", "TUNED_TEST", "MAP_TYPE", "TARGET_TABLE", "LOG"):
        if col not in df.columns:
            df[col] = ""
        df[col] = df[col].fillna("").astype(str)
    return df


def _contains(series: pd.Series, keyword: str) -> pd.Series:
    keyword = keyword.strip()
    if not keyword:
        return pd.Series(True, index=series.index)
    return series.fillna("").astype(str).str.contains(keyword, case=False, na=False, regex=False)


def _options(df: pd.DataFrame, column: str) -> list[str]:
    values = [v for v in df[column].dropna().astype(str).str.strip().unique().tolist() if v]
    return [ALL] + sorted(values)


def _job_label(row: pd.Series) -> str:
    return (
        f"{row.get('SPACE_NM') or '-'} / {row.get('SQL_ID') or '-'} "
        f"| STATUS={row.get('STATUS') or 'NULL'} "
        f"| TUNED_TEST={row.get('TUNED_TEST') or 'NULL'}"
    )


def render():
    st.title("Correct SQL Manager")

    if st.button("새로고침"):
        st.rerun()

    try:
        jobs = get_sql_jobs()
    except Exception as exc:
        st.error(f"DB 연결 실패: {exc}")
        return

    if not jobs:
        st.info("SQL Job 데이터가 없습니다.")
        return

    df_all = _prepare_df(jobs)

    with st.expander("검색 / 필터", expanded=True):
        c1, c2, c3, c4 = st.columns([1.4, 1.4, 1, 1])
        with c1:
            sql_id_query = st.text_input("SQL_ID LIKE")
        with c2:
            namespace_query = st.text_input("SPACE_NM LIKE")
        with c3:
            sel_status = st.selectbox("STATUS", _options(df_all, "STATUS"))
        with c4:
            sel_tuned = st.selectbox("TUNED_TEST", _options(df_all, "TUNED_TEST"))

    df = df_all.copy()
    df = df[_contains(df["SQL_ID"], sql_id_query)]
    df = df[_contains(df["SPACE_NM"], namespace_query)]
    if sel_status != ALL:
        df = df[df["STATUS"] == sel_status]
    if sel_tuned != ALL:
        df = df[df["TUNED_TEST"] == sel_tuned]

    if df.empty:
        st.warning("조건에 맞는 SQL Job이 없습니다.")
        return

    st.caption(f"검색 결과 {len(df)}건 / 전체 {len(df_all)}건")
    records = df.to_dict("records")
    selected_idx = st.selectbox(
        "SQL Job 선택",
        range(len(records)),
        format_func=lambda i: _job_label(pd.Series(records[i])),
    )
    row_id = str(records[selected_idx]["ROW_ID"])
    detail = get_sql_job_full(row_id) or records[selected_idx]

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.write(f"**SQL_ID:** {detail.get('SQL_ID') or '-'}")
    with m2:
        st.write(f"**SPACE_NM:** {detail.get('SPACE_NM') or '-'}")
    with m3:
        st.metric("STATUS", detail.get("STATUS") or "-")
    with m4:
        st.metric("TUNED_TEST", detail.get("TUNED_TEST") or "-")

    st.divider()

    left, right = st.columns(2)
    view_labels = list(_SQL_VIEW_OPTIONS.keys())
    with left:
        st.subheader("컬럼 보기")
        view_label = st.selectbox("왼쪽 컬럼", view_labels, index=0)
        st.code(detail.get(_SQL_VIEW_OPTIONS[view_label]) or "(없음)", language="sql")

    with right:
        st.subheader("Correct SQL 입력")
        correct_label = st.selectbox("저장 대상", list(_CORRECT_KIND_OPTIONS.keys()))
        correct_kind, correct_column = _CORRECT_KIND_OPTIONS[correct_label]
        current_value = detail.get(correct_column) or ""
        correct_sql = st.text_area(
            "Correct SQL",
            value=current_value,
            height=420,
            placeholder="검증된 Correct SQL을 입력하세요.",
        )
        if st.button("Correct SQL 저장", type="primary", width="stretch"):
            ok, message = update_sql_correct_sql(row_id, correct_kind, correct_sql)
            if ok:
                st.success(message)
                st.rerun()
            else:
                st.error(message)
