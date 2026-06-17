import pandas as pd
import streamlit as st

from utils.rag_db import add_rule, delete_rule, get_all_rules, get_top_rules, update_rule


CATEGORIES = ["SQL_CONVERSION", "SQL_TUNING"]
RULE_TYPES = ["SEARCH", "GENERAL"]
USE_OPTIONS = ["Y", "N"]


def _option_index(options: list[str], value: str, default: int = 0) -> int:
    try:
        return options.index((value or "").strip().upper())
    except ValueError:
        return default


def _rule_label(rule: dict) -> str:
    return (
        f"{rule.get('RAG_ID')} | {rule.get('CATEGORY')} | "
        f"{rule.get('RULE_TYPE')} | {rule.get('SOURCE_TABLES') or 'ALL'}"
    )


def render():
    st.title("RAG Rule Manager")
    st.caption("Oracle DB NEXT_MIG_RAG_INFO based SQL Conversion / SQL Tuning RAG manager")

    _, col_refresh = st.columns([9, 1])
    with col_refresh:
        if st.button("Refresh"):
            st.rerun()

    try:
        rules = get_all_rules()
    except Exception as exc:
        st.error(f"DB 조회 실패: {exc}")
        return

    st.write(f"**총 {len(rules)}개 RAG rule 등록됨**")

    try:
        top_rules = get_top_rules(5)
    except Exception:
        top_rules = []

    if top_rules:
        st.subheader("Top HIT_CNT")
        for rank, rule in enumerate(top_rules, 1):
            hit = int(rule.get("HIT_CNT", 0) or 0)
            with st.expander(f"{rank}. RAG_ID={rule.get('RAG_ID')} | {hit} hits", expanded=(rank == 1)):
                st.write(f"**category:** `{rule.get('CATEGORY')}`")
                st.write(f"**rule_type:** `{rule.get('RULE_TYPE')}`")
                st.write(f"**source_tables:** `{rule.get('SOURCE_TABLES') or 'ALL'}`")
                st.write("**guidance_text:**")
                st.write(rule.get("GUIDANCE_TEXT") or "(empty)")
                if rule.get("SOURCE_SQL"):
                    st.write("**source_sql:**")
                    st.code(rule["SOURCE_SQL"], language="sql")
                if rule.get("TARGET_SQL"):
                    st.write("**target_sql:**")
                    st.code(rule["TARGET_SQL"], language="sql")
        st.divider()

    col_kw, col_category, col_type = st.columns([4, 2, 2])
    with col_kw:
        keyword = st.text_input("검색", placeholder="RAG_ID, SOURCE_TABLES, GUIDANCE_TEXT, SQL")
    with col_category:
        category_filter = st.selectbox("CATEGORY", ["ALL"] + CATEGORIES)
    with col_type:
        type_filter = st.selectbox("RULE_TYPE", ["ALL"] + RULE_TYPES)

    filtered = rules
    if category_filter != "ALL":
        filtered = [rule for rule in filtered if rule.get("CATEGORY") == category_filter]
    if type_filter != "ALL":
        filtered = [rule for rule in filtered if rule.get("RULE_TYPE") == type_filter]
    if keyword:
        kw = keyword.lower()
        filtered = [
            rule
            for rule in filtered
            if kw in str(rule.get("RAG_ID") or "").lower()
            or kw in (rule.get("SOURCE_TABLES") or "").lower()
            or kw in (rule.get("GUIDANCE_TEXT") or "").lower()
            or kw in (rule.get("SOURCE_SQL") or "").lower()
            or kw in (rule.get("TARGET_SQL") or "").lower()
        ]

    if filtered:
        table_rows = [
            {
                "RAG_ID": rule.get("RAG_ID"),
                "CATEGORY": rule.get("CATEGORY"),
                "RULE_TYPE": rule.get("RULE_TYPE"),
                "SOURCE_TABLES": rule.get("SOURCE_TABLES") or "ALL",
                "USE_YN": rule.get("USE_YN"),
                "HIT_CNT": int(rule.get("HIT_CNT", 0) or 0),
                "GUIDANCE": (rule.get("GUIDANCE_TEXT") or "")[:100],
                "UPDATED_AT": rule.get("UPDATED_AT", ""),
            }
            for rule in filtered
        ]
        st.dataframe(pd.DataFrame(table_rows), width="stretch", hide_index=True)
    else:
        st.info("검색 결과 없음")

    st.divider()

    if filtered:
        st.subheader("상세 조회 / 수정 / 삭제")
        selected_label = st.selectbox("RAG rule 선택", [_rule_label(rule) for rule in filtered])
        selected_id = selected_label.split("|", 1)[0].strip()
        selected = next((rule for rule in filtered if str(rule.get("RAG_ID")) == selected_id), None)

        if selected:
            with st.expander(f"RAG_ID={selected_id} 상세", expanded=True):
                tab_view, tab_edit = st.tabs(["조회", "수정"])

                with tab_view:
                    st.write(f"**category:** `{selected.get('CATEGORY')}`")
                    st.write(f"**rule_type:** `{selected.get('RULE_TYPE')}`")
                    st.write(f"**source_tables:** `{selected.get('SOURCE_TABLES') or 'ALL'}`")
                    st.write(f"**use_yn:** `{selected.get('USE_YN')}`")
                    st.write("**guidance_text:**")
                    st.write(selected.get("GUIDANCE_TEXT") or "(empty)")
                    st.write("**source_sql:**")
                    st.code(selected.get("SOURCE_SQL") or "(empty)", language="sql")
                    st.write("**target_sql:**")
                    st.code(selected.get("TARGET_SQL") or "(empty)", language="sql")
                    st.caption(
                        f"created: {selected.get('CREATED_AT','')} | updated: {selected.get('UPDATED_AT','')}"
                    )

                with tab_edit:
                    with st.form(f"edit_form_{selected_id}"):
                        edit_category = st.selectbox(
                            "CATEGORY",
                            CATEGORIES,
                            index=_option_index(CATEGORIES, selected.get("CATEGORY")),
                        )
                        edit_type = st.selectbox(
                            "RULE_TYPE",
                            RULE_TYPES,
                            index=_option_index(RULE_TYPES, selected.get("RULE_TYPE")),
                        )
                        edit_use = st.selectbox(
                            "USE_YN",
                            USE_OPTIONS,
                            index=_option_index(USE_OPTIONS, selected.get("USE_YN"), default=0),
                        )
                        edit_tables = st.text_input(
                            "SOURCE_TABLES",
                            value=selected.get("SOURCE_TABLES") or "",
                            placeholder="ASIS_CODE,ASIS_USER (empty = ALL)",
                        )
                        edit_guidance = st.text_area(
                            "GUIDANCE_TEXT",
                            value=selected.get("GUIDANCE_TEXT") or "",
                            height=140,
                        )
                        edit_source_sql = st.text_area(
                            "SOURCE_SQL",
                            value=selected.get("SOURCE_SQL") or "",
                            height=160,
                        )
                        edit_target_sql = st.text_area(
                            "TARGET_SQL",
                            value=selected.get("TARGET_SQL") or "",
                            height=160,
                        )
                        if st.form_submit_button("수정 저장", type="primary"):
                            if not edit_guidance.strip():
                                st.error("GUIDANCE_TEXT를 입력하세요.")
                            elif edit_type == "SEARCH" and not edit_source_sql.strip():
                                st.error("SEARCH rule은 SOURCE_SQL이 필요합니다.")
                            else:
                                try:
                                    update_rule(
                                        selected_id,
                                        edit_category,
                                        edit_type,
                                        edit_tables,
                                        edit_guidance,
                                        edit_source_sql,
                                        edit_target_sql,
                                        edit_use,
                                    )
                                    st.success(f"RAG_ID={selected_id} 수정 완료")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(f"수정 실패: {exc}")

            if st.button(f"RAG_ID={selected_id} 삭제", type="secondary", key=f"del_{selected_id}"):
                st.session_state["confirm_delete_rag"] = selected_id

            if st.session_state.get("confirm_delete_rag") == selected_id:
                st.warning(f"RAG_ID={selected_id}를 삭제할까요?")
                col_yes, col_no = st.columns(2)
                with col_yes:
                    if st.button("삭제 확인", type="primary", key="do_delete_rag"):
                        try:
                            ok = delete_rule(selected_id)
                            if ok:
                                st.success(f"RAG_ID={selected_id} 삭제 완료")
                                st.session_state.pop("confirm_delete_rag", None)
                                st.rerun()
                            else:
                                st.error("삭제할 row를 찾지 못했습니다.")
                        except Exception as exc:
                            st.error(f"삭제 실패: {exc}")
                with col_no:
                    if st.button("취소", key="cancel_delete_rag"):
                        st.session_state.pop("confirm_delete_rag", None)
                        st.rerun()

    st.divider()
    st.subheader("새 RAG rule 생성")

    with st.form("new_rag_rule_form", clear_on_submit=True):
        new_category = st.selectbox("CATEGORY", CATEGORIES)
        new_type = st.selectbox("RULE_TYPE", RULE_TYPES)
        new_use = st.selectbox("USE_YN", USE_OPTIONS)
        new_tables = st.text_input(
            "SOURCE_TABLES",
            placeholder="ASIS_CODE,ASIS_USER (empty = ALL)",
        )
        new_guidance = st.text_area(
            "GUIDANCE_TEXT",
            placeholder="LLM에 전달할 가이드 문구",
            height=120,
        )
        new_source_sql = st.text_area(
            "SOURCE_SQL",
            placeholder="SQL_CONVERSION: FROM SQL / SQL_TUNING: Bad SQL",
            height=140,
        )
        new_target_sql = st.text_area(
            "TARGET_SQL",
            placeholder="SQL_CONVERSION: TOBE SQL / SQL_TUNING: Tuned SQL",
            height=140,
        )
        submitted = st.form_submit_button("저장", type="primary")

    if submitted:
        if not new_guidance.strip():
            st.error("GUIDANCE_TEXT를 입력하세요.")
        elif new_type == "SEARCH" and not new_source_sql.strip():
            st.error("SEARCH rule은 SOURCE_SQL이 필요합니다.")
        else:
            try:
                add_rule(
                    new_category,
                    new_type,
                    new_tables,
                    new_guidance,
                    new_source_sql,
                    new_target_sql,
                    new_use,
                )
                st.success("RAG rule 생성 완료")
                st.rerun()
            except Exception as exc:
                st.error(f"저장 실패: {exc}")
