from collections import defaultdict
from html import escape
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import streamlit as st

from utils.db import get_xml_export_sqls

_XML_HEADER = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<!DOCTYPE mapper PUBLIC "-//mybatis.org//DTD Mapper 3.0//EN"'
    ' "http://mybatis.org/dtd/mybatis-3-mapper.dtd">'
)


def _status(value: str | None) -> str:
    return (value or "NULL").strip().upper() or "NULL"


def _is_pass(row: dict) -> bool:
    return _status(row.get("TUNED_TEST")) in {"PASS", "TUNING-PASS", "TUNING_PASS", "PASS_NON_SELECT"}


def _has_formatted_sql(row: dict) -> bool:
    return bool((row.get("FORMATTED_SQL") or "").strip())


def _safe_file_name(namespace: str) -> str:
    unsafe = '\\/:*?"<>|'
    name = "".join("_" if ch in unsafe else ch for ch in namespace).strip()
    return name or "mapper"


def _mapper_file_name(namespace: str) -> str:
    return f"mapper-{_safe_file_name(namespace)}.xml"


def _build_xml(namespace: str, rows: list[dict]) -> str:
    lines = [_XML_HEADER, "", f'<mapper namespace="{escape(namespace, quote=True)}">']
    for row in rows:
        if not _is_pass(row) or not _has_formatted_sql(row):
            continue

        tag = (row.get("TAG_KIND") or "select").strip().lower() or "select"
        sql_id = escape((row.get("SQL_ID") or "").strip(), quote=True)
        sql = (row.get("FORMATTED_SQL") or "").strip()
        open_tag = f'  <{tag} id="{sql_id}">'
        lines.append(open_tag)
        for sql_line in sql.splitlines():
            lines.append(f"    {sql_line}")
        lines.append(f"  </{tag}>")
        lines.append("")
    lines.append("</mapper>")
    return "\n".join(lines)


def _namespace_stats(rows: list[dict]) -> dict[str, int]:
    pass_count = sum(1 for row in rows if _is_pass(row))
    return {
        "total": len(rows),
        "pass": pass_count,
        "fail": len(rows) - pass_count,
        "exportable": sum(1 for row in rows if _is_pass(row) and _has_formatted_sql(row)),
    }


def _build_bulk_zip(grouped: dict[str, list[dict]]) -> bytes:
    buffer = BytesIO()
    with ZipFile(buffer, "w", ZIP_DEFLATED) as zip_file:
        for namespace in sorted(grouped):
            rows = [row for row in grouped[namespace] if _is_pass(row) and _has_formatted_sql(row)]
            if not rows:
                continue
            zip_file.writestr(
                _mapper_file_name(namespace),
                _build_xml(namespace, rows),
            )
    return buffer.getvalue()


def render():
    st.title("MyBatis XML Export")
    st.caption("FORMATTED_SQL 기준으로 MyBatis mapper XML을 생성합니다.")

    col_refresh, _ = st.columns([1, 9])
    with col_refresh:
        if st.button("새로고침"):
            st.rerun()

    try:
        rows = get_xml_export_sqls()
    except Exception as exc:
        st.error(f"DB 연결 실패: {exc}")
        return

    if not rows:
        st.info("XML export 대상 SQL이 없습니다.")
        return

    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row.get("SPACE_NM") or "(없음)").strip()].append(row)

    namespaces = sorted(grouped.keys())
    total_pass = sum(1 for row in rows if _is_pass(row))
    total_fail = len(rows) - total_pass
    exportable_total = sum(1 for row in rows if _is_pass(row) and _has_formatted_sql(row))

    c_ns, c_pass, c_fail, c_export = st.columns(4)
    c_ns.metric("전체 namespace", len(namespaces))
    c_pass.metric("PASS", total_pass)
    c_fail.metric("FAIL", total_fail)
    c_export.metric("다운로드 가능 SQL", exportable_total)

    bulk_zip = _build_bulk_zip(grouped)
    st.download_button(
        "전체 PASS XML 일괄 다운로드",
        data=bulk_zip,
        file_name="mybatis_xml_pass_all.zip",
        mime="application/zip",
        disabled=exportable_total == 0,
        width="stretch",
    )

    st.divider()

    selected_ns = st.selectbox(
        "Namespace 선택",
        options=namespaces,
        format_func=lambda ns: (
            f"{ns} "
            f"(PASS {_namespace_stats(grouped[ns])['pass']} / "
            f"FAIL {_namespace_stats(grouped[ns])['fail']})"
        ),
    )

    if not selected_ns:
        return

    selected_rows = grouped[selected_ns]
    stats = _namespace_stats(selected_rows)
    selected_available = stats["fail"] == 0 and stats["exportable"] > 0
    xml_text = _build_xml(selected_ns, selected_rows)

    col_download, col_selected = st.columns([1, 3])
    with col_download:
        st.download_button(
            "XML 다운로드" if selected_available else "Not available",
            data=xml_text.encode("utf-8"),
            file_name=_mapper_file_name(selected_ns),
            mime="application/xml",
            disabled=not selected_available,
            width="stretch",
        )
    with col_selected:
        if selected_available:
            st.caption(
                f"{selected_ns} · PASS {stats['pass']}건 · 다운로드 가능 {stats['exportable']}건"
            )
        else:
            st.warning(
                f"Not available: {selected_ns}에 FAIL {stats['fail']}건이 있어 namespace별 다운로드가 비활성화되었습니다."
            )

    st.code(xml_text, language="xml")
