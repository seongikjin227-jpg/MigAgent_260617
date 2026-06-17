"""mapper XML을 파싱해 NEXT_SQL_INFO로 동기화하는 유틸 파이프라인."""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

import oracledb

from server.services.sql.db_runtime import get_connection, get_result_table
from server.core.logger import logger


SUPPORTED_TAGS = {"select", "insert", "update", "delete", "sql"}
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "DATA"
INCLUDE_PATTERN = re.compile(
    r"<include\b[^>]*\brefid\s*=\s*['\"]([^'\"]+)['\"][^>]*>(?:.*?)</include>"
    r"|<include\b[^>]*\brefid\s*=\s*['\"]([^'\"]+)['\"][^>]*/\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_SQL_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", flags=re.DOTALL)
_SQL_LINE_COMMENT_PATTERN = re.compile(r"--[^\n]*")
_SQL_SINGLE_QUOTE_PATTERN = re.compile(r"'(?:''|[^'])*'")
_SQL_MYBATIS_PLACEHOLDER_PATTERN = re.compile(r"[#$]\{\s*[^}]+\s*\}")
_SQL_XML_TAG_PATTERN = re.compile(r"<[^>]+>")
_SQL_SCHEMA_STRIP_PROTECTED_PATTERN = re.compile(
    r"/\*.*?\*/|--[^\n]*|'(?:''|[^'])*'|[#$]\{\s*[^}]+\s*\}|<[^>]+>",
    flags=re.DOTALL,
)
_SQL_TABLE_NOISE_WORDS = {
    "AS",
    "BY",
    "CASE",
    "COALESCE",
    "COUNT",
    "DECODE",
    "DUAL",
    "ELSE",
    "END",
    "FROM",
    "GROUP",
    "HAVING",
    "JOIN",
    "MAX",
    "MIN",
    "NVL",
    "ON",
    "ORDER",
    "SELECT",
    "SET",
    "SUBSTR",
    "SUM",
    "THEN",
    "TO_CHAR",
    "TO_DATE",
    "TO_NUMBER",
    "TRIM",
    "USING",
    "WHEN",
    "WHERE",
}


@dataclass
class ParsedSqlItem:
    tag_kind: str
    space_nm: str
    sql_id: str
    fr_sql_text: str
    target_table: list[str]
    source_file: str

    def to_json_payload(self) -> dict[str, Any]:
        return {
            "TAG_KIND": self.tag_kind,
            "SPACE_NM": self.space_nm,
            "SQL_ID": self.sql_id,
            "FR_SQL_TEXT": self.fr_sql_text,
            "TARGET_TABLE": self.target_table,
            "SOURCE_FILE": self.source_file,
        }


def _to_text(value: Any, default: str = "") -> str:
    """DB/JSON 값을 안전하게 문자열로 정규화한다."""
    if value is None:
        return default
    if hasattr(value, "read"):
        value = value.read()
    if value is None:
        return default
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _require_env(name: str) -> str:
    """필수 환경변수를 읽고 누락 시 즉시 예외를 발생시킨다."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Required environment variable '{name}' is not set.")
    return value


def _safe_filename_component(text: str) -> str:
    """상대 경로를 파일시스템 안전한 파일명으로 변환한다."""
    return re.sub(r'[<>:"/\\|?*]+', "_", text.strip()) or "_"


def _local_tag_name(tag_name: str) -> str:
    # Handles namespaced XML tag names such as {namespace}select.
    if "}" in tag_name:
        return tag_name.split("}", 1)[1].lower()
    return tag_name.lower()


def _inner_xml(element: ET.Element) -> str:
    """자식 태그/테일을 포함한 inner XML 문자열을 추출한다."""
    parts: list[str] = []
    if element.text:
        parts.append(element.text)
    for child in list(element):
        child_tail = child.tail
        try:
            child.tail = None
            parts.append(ET.tostring(child, encoding="unicode"))
        finally:
            child.tail = child_tail
        if child_tail:
            parts.append(child_tail)
    return "".join(parts).strip()


def _normalize_table_name(token: str) -> str:
    """자유형식 문자열/JSON에서 테이블명을 정규화해 대문자로 반환한다."""
    value = token.strip()
    if not value:
        return ""

    # 흔한 감싸기 문자/구분자 정리: ["SCHEMA.TB_A"], (TB_A), {TB_A}, TB_A;
    value = value.strip().strip(",").strip(";")
    value = value.strip('"').strip("'").strip()
    value = value.strip("[](){}").strip()
    value = value.strip('"').strip("'").strip()

    # 잔여 접미 구두점 제거
    value = re.sub(r"[;,]+$", "", value).strip()
    if not value:
        return ""
    # Ignore non-table noise tokens such as '-', '--', separators.
    if not re.search(r"[A-Za-z0-9_]", value):
        return ""
    return value.upper()


def _short_table_name(table_name: str) -> str:
    """SCHEMA.TABLE 형태에서 테이블명만 반환한다."""
    normalized = _normalize_table_name(table_name)
    if not normalized:
        return ""
    return normalized.split(".")[-1].strip('"')


def _is_valid_target_table_name(table_name: str) -> bool:
    """TARGET_TABLE에 저장해도 되는 실제 테이블명 후보인지 검사한다."""
    normalized = _normalize_table_name(table_name)
    if not normalized:
        return False
    table_short = normalized.split(".")[-1].strip('"')
    if not table_short:
        return False
    if table_short.isdigit() or normalized.replace(".", "").replace('"', "").isdigit():
        return False
    if normalized in _SQL_TABLE_NOISE_WORDS or table_short in _SQL_TABLE_NOISE_WORDS:
        return False
    return True


def _strip_schema_from_target_tables(value: Any) -> list[str]:
    """TARGET_TABLE 값을 스키마 제거/중복 제거된 테이블명 목록으로 변환한다."""
    results: list[str] = []
    seen = set()
    for table_name in _parse_stored_target_table(value):
        short_name = _short_table_name(table_name)
        if not short_name or not _is_valid_target_table_name(short_name) or short_name in seen:
            continue
        results.append(short_name)
        seen.add(short_name)
    return results


def parse_single_mapper_xml(xml_path: Path) -> list[ParsedSqlItem]:
    """mapper XML 1개를 파싱해 ParsedSqlItem 목록으로 반환한다."""
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as exc:
        logger.warning(f"[XMLParser] Failed to parse XML file: {xml_path} ({exc})")
        return []

    root = tree.getroot()
    namespace = (root.attrib.get("namespace") or "").strip()
    if not namespace:
        logger.warning(f"[XMLParser] Skipping mapper without namespace: {xml_path}")
        return []

    parsed_items: list[ParsedSqlItem] = []
    for elem in root.iter():
        local_name = _local_tag_name(elem.tag)
        if local_name not in SUPPORTED_TAGS:
            continue

        sql_id = (elem.attrib.get("id") or "").strip()
        if not sql_id:
            continue

        sql_text = _inner_xml(elem)
        parsed_items.append(
            ParsedSqlItem(
                tag_kind=local_name.upper(),
                space_nm=namespace,
                sql_id=sql_id,
                fr_sql_text=sql_text,
                target_table=[],
                source_file=str(xml_path),
            )
        )

    return parsed_items


def _resolve_output_dir(output_dir: str | None = None) -> Path:
    """출력 디렉터리를 인자/환경변수/기본값 순서로 결정하고 생성한다."""
    if output_dir:
        resolved = Path(output_dir)
    else:
        configured = os.getenv("XML_PARSER_DATA_DIR", "").strip()
        resolved = Path(configured) if configured else DEFAULT_OUTPUT_DIR
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _cleanup_output_json_files(output_dir: Path) -> int:
    """stage1 재생성 전에 기존 JSON 산출물을 정리한다."""
    removed = 0
    for file_path in output_dir.glob("*.json"):
        try:
            file_path.unlink()
            removed += 1
        except Exception as exc:
            logger.warning(f"[XMLParser] Failed to remove old JSON file: {file_path} ({exc})")
    return removed


def _parse_target_tables_from_active_columns(*values: Any) -> list[str]:
    """ACTIVE 테이블의 C/R/U/D 컬럼 문자열에서 테이블 목록을 파싱한다."""
    results: list[str] = []
    seen = set()
    for value in values:
        if value is None:
            continue
        # 값 타입이 리스트/튜플/셋이면 그대로 펼쳐서 사용한다.
        if isinstance(value, (list, tuple, set)):
            tokens = [str(item) for item in value]
        else:
            text = _to_text(value).strip()
            if not text:
                continue

            # 1) JSON 배열 시도
            # 2) 단일 JSON 문자열 시도
            # 3) 실패 시 구분자 split
            tokens: list[str] = []
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    tokens = [str(item) for item in parsed]
                elif isinstance(parsed, str):
                    tokens = [parsed]
            except Exception:
                tokens = []

            if not tokens:
                tokens = re.split(r"[,\s;|]+", text)

        for token in tokens:
            normalized = _normalize_table_name(token)
            if not normalized or not _is_valid_target_table_name(normalized) or normalized in seen:
                continue
            results.append(normalized)
            seen.add(normalized)
    return results


def _load_target_table_map_from_active_table() -> dict[str, list[str]]:
    """ACTIVE SQL ID 테이블에서 full_id -> target_table[] 맵을 읽는다."""
    active_table = _validate_sql_identifier(_require_env("ACTIVE_SQL_ID_TABLE"))
    active_column = _validate_sql_identifier(os.getenv("ACTIVE_SQL_ID_COLUMN", "SQL_ID"))

    query = f"""
        SELECT TO_CHAR({active_column}),
               C_TABLES, R_TABLES, U_TABLES, D_TABLES
        FROM {active_table}
    """
    mapped: dict[str, list[str]] = {}
    invalid_ids: list[str] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query)
        for row in cursor.fetchall():
            full_id = _to_text(row[0]).strip().upper()
            if not full_id:
                continue
            if "." not in full_id:
                if len(invalid_ids) < 10:
                    invalid_ids.append(full_id)
                continue
            tables = _parse_target_tables_from_active_columns(row[1], row[2], row[3], row[4])
            if full_id in mapped:
                existing = mapped[full_id]
                seen = set(existing)
                for table_name in tables:
                    if table_name not in seen:
                        existing.append(table_name)
                        seen.add(table_name)
            else:
                mapped[full_id] = tables

    if invalid_ids:
        sample = ", ".join(invalid_ids[:5])
        raise ValueError(
            "ACTIVE_SQL_ID_COLUMN must contain full id format 'NAMESPACE.SQL_ID'. "
            f"Invalid sample values: {sample}"
        )
    return mapped


def parse_mapper_dir_to_json(
    source_dir: str | None = None,
    output_dir: str | None = None,
) -> dict[str, int]:
    """Stage1: mapper XML 트리를 파싱해 파일별 JSON payload를 생성한다."""
    source_path = Path(source_dir or _require_env("MAPPER_XML_SOURCE_DIR"))
    if not source_path.exists() or not source_path.is_dir():
        raise ValueError(f"Mapper source directory does not exist: {source_path}")

    out_dir = _resolve_output_dir(output_dir)
    removed_json = _cleanup_output_json_files(out_dir)
    xml_files = sorted(source_path.rglob("*.xml"))
    target_table_map = _load_target_table_map_from_active_table()
    logger.info(
        f"[XMLParser] Stage1 started (source={source_path}, files={len(xml_files)}, removed_old_json={removed_json})"
    )

    total_items = 0
    written_files = 0
    for xml_file in xml_files:
        items = parse_single_mapper_xml(xml_file)
        if not items:
            continue

        json_rows: list[dict[str, Any]] = []
        for item in items:
            total_items += 1
            full_id = f"{item.space_nm}.{item.sql_id}".upper()
            item.target_table = target_table_map.get(full_id, [])
            json_rows.append(item.to_json_payload())

        rel_path = xml_file.relative_to(source_path).as_posix()
        file_stem = rel_path.rsplit(".", 1)[0].replace("/", "__")
        file_name = f"{_safe_filename_component(file_stem)}.json"
        file_path = out_dir / file_name
        file_path.write_text(
            json.dumps(json_rows, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        written_files += 1

    logger.info(
        f"[XMLParser] Stage1 completed (parsed_items={total_items}, json_files={written_files}, out_dir={out_dir})"
    )
    return {
        "xml_files": len(xml_files),
        "parsed_items": total_items,
        "json_files": written_files,
    }


def _load_json_payloads(data_dir: str | None = None) -> list[dict[str, Any]]:
    """stage1 JSON 파일들을 평탄화된 payload 목록으로 로드한다."""
    root = _resolve_output_dir(data_dir)
    payloads: list[dict[str, Any]] = []
    for file_path in sorted(root.glob("*.json")):
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                payloads.append(raw)
                continue
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        payloads.append(item)
                continue
            logger.warning(f"[XMLParser] Unsupported JSON root type skipped: {file_path}")
        except Exception as exc:
            logger.warning(f"[XMLParser] Invalid JSON skipped: {file_path} ({exc})")
    return payloads


def _count_json_files(data_dir: str | None = None) -> int:
    """stage 로깅용 JSON 파일 개수를 반환한다."""
    root = _resolve_output_dir(data_dir)
    return len(list(root.glob("*.json")))


def upsert_json_to_next_sql_info(data_dir: str | None = None) -> dict[str, int]:
    """Stage2: stage1 payload를 NEXT_SQL_INFO에 MERGE upsert 한다."""
    table = get_result_table()
    json_file_count = _count_json_files(data_dir)
    payloads = _load_json_payloads(data_dir)
    logger.info(
        f"[XMLParser] Stage2 started (json_files={json_file_count}, payloads={len(payloads)})"
    )

    if not payloads:
        return {"json_files": json_file_count, "payloads": 0, "upserted": 0}

    merge_sql = f"""
        MERGE INTO {table} T
        USING (
            SELECT :tag_kind AS TAG_KIND,
                   :space_nm AS SPACE_NM,
                   :sql_id AS SQL_ID,
                   :fr_sql_text AS FR_SQL_TEXT,
                   :target_table AS TARGET_TABLE
            FROM DUAL
        ) S
        ON (TO_CHAR(T.SPACE_NM) = TO_CHAR(S.SPACE_NM) AND TO_CHAR(T.SQL_ID) = TO_CHAR(S.SQL_ID))
        WHEN MATCHED THEN
            UPDATE SET
                T.TAG_KIND = S.TAG_KIND,
                T.FR_SQL_TEXT = S.FR_SQL_TEXT,
                T.TARGET_TABLE = S.TARGET_TABLE,
                T.EDIT_FR_SQL = NULL,
                T.UPD_TS = CURRENT_TIMESTAMP
        WHEN NOT MATCHED THEN
            INSERT (
                TAG_KIND, SPACE_NM, SQL_ID, FR_SQL_TEXT, TARGET_TABLE, UPD_TS
            )
            VALUES (
                S.TAG_KIND, S.SPACE_NM, S.SQL_ID, S.FR_SQL_TEXT, S.TARGET_TABLE, CURRENT_TIMESTAMP
            )
    """

    upserted = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.setinputsizes(
            tag_kind=oracledb.DB_TYPE_VARCHAR,
            space_nm=oracledb.DB_TYPE_VARCHAR,
            sql_id=oracledb.DB_TYPE_VARCHAR,
            fr_sql_text=oracledb.DB_TYPE_CLOB,
            target_table=oracledb.DB_TYPE_CLOB,
        )
        for payload in payloads:
            tag_kind = _to_text(payload.get("TAG_KIND")).strip().upper()
            space_nm = _to_text(payload.get("SPACE_NM")).strip()
            sql_id = _to_text(payload.get("SQL_ID")).strip()
            fr_sql_text = _to_text(payload.get("FR_SQL_TEXT"))
            target_table_value = payload.get("TARGET_TABLE")
            if isinstance(target_table_value, list):
                target_table = json.dumps(target_table_value, ensure_ascii=False)
            else:
                target_table = _to_text(target_table_value)

            if not (tag_kind and space_nm and sql_id):
                logger.warning(
                    f"[XMLParser] Missing key fields; skipped payload (space_nm={space_nm}, sql_id={sql_id})"
                )
                continue

            cursor.execute(
                merge_sql,
                {
                    "tag_kind": tag_kind,
                    "space_nm": space_nm,
                    "sql_id": sql_id,
                    "fr_sql_text": fr_sql_text,
                    "target_table": target_table,
                },
            )
            upserted += 1

        conn.commit()

    logger.info(
        f"[XMLParser] Stage2 completed (json_files={json_file_count}, payloads={len(payloads)}, upserted={upserted})"
    )
    return {"json_files": json_file_count, "payloads": len(payloads), "upserted": upserted}


def _parse_refid(refid: str, current_space: str) -> tuple[str, str]:
    """include refid를 (namespace, sql_id) 형태로 해석한다."""
    clean_refid = (refid or "").strip()
    if not clean_refid:
        return current_space, clean_refid

    # Most common style: namespace.sqlId
    if "." in clean_refid:
        namespace, sql_id = clean_refid.rsplit(".", 1)
        if namespace and sql_id:
            return namespace, sql_id

    # Fallback: same namespace local ref.
    return current_space, clean_refid


def _resolve_include_text(
    sql_text: str,
    current_space: str,
    fragment_map: dict[tuple[str, str], str],
    stack: set[tuple[str, str]] | None = None,
    max_depth: int = 20,
) -> str:
    """cycle guard를 적용해 `<include refid>`를 재귀적으로 해소한다."""
    resolved = sql_text
    active_stack = set(stack or set())

    for _ in range(max_depth):
        changed = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal changed
            refid = (match.group(1) or match.group(2) or "").strip()
            ref_space, ref_sql_id = _parse_refid(refid, current_space)
            key = (ref_space, ref_sql_id)

            # Support local fragment if qualified lookup misses.
            if key not in fragment_map and "." in refid:
                key = (current_space, refid)
                ref_space, ref_sql_id = key

            if key in active_stack:
                logger.warning(f"[XMLParser] include cycle detected: {ref_space}.{ref_sql_id}")
                return match.group(0)

            fragment_sql = fragment_map.get(key)
            if fragment_sql is None:
                logger.warning(f"[XMLParser] include ref not found: {refid}")
                return match.group(0)

            changed = True
            nested_stack = set(active_stack)
            nested_stack.add(key)
            return _resolve_include_text(
                sql_text=fragment_sql,
                current_space=ref_space,
                fragment_map=fragment_map,
                stack=nested_stack,
                max_depth=max_depth,
            )

        replaced = INCLUDE_PATTERN.sub(_replace, resolved)
        if not changed or replaced == resolved:
            resolved = replaced
            break
        resolved = replaced

    return resolved


def expand_include_to_edit_sql() -> dict[str, int]:
    """Stage3: include 확장 SQL을 EDIT_FR_SQL에 저장한다."""
    table = get_result_table()
    logger.info("[XMLParser] Stage3 started")
    fetch_sql = f"""
        SELECT TO_CHAR(SPACE_NM), TO_CHAR(SQL_ID), TO_CHAR(TAG_KIND), FR_SQL_TEXT, EDIT_FR_SQL
        FROM {table}
    """
    rows: list[tuple[str, str, str, str, str]] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(fetch_sql)
        for row in cursor.fetchall():
            rows.append(
                (
                    _to_text(row[0]).strip(),
                    _to_text(row[1]).strip(),
                    _to_text(row[2]).strip().upper(),
                    _to_text(row[3]),
                    _to_text(row[4]),
                )
            )

    fragment_map: dict[tuple[str, str], str] = {}
    for space_nm, sql_id, _tag_kind, fr_sql_text, edit_fr_sql in rows:
        fragment_map[(space_nm, sql_id)] = fr_sql_text or ""

    updates: list[tuple[str, str, str]] = []
    include_candidates = 0
    for space_nm, sql_id, _tag_kind, fr_sql_text, edit_fr_sql in rows:
        # FR_SQL_TEXT를 source of truth로 삼아 include 확장을 매번 재생성한다.
        base_sql = fr_sql_text or ""
        if "<include" not in base_sql.lower():
            continue
        include_candidates += 1
        resolved = _resolve_include_text(
            sql_text=base_sql,
            current_space=space_nm,
            fragment_map=fragment_map,
        ).strip()
        if resolved and resolved != base_sql:
            updates.append((resolved, space_nm, sql_id))

    if not updates:
        logger.info(
            f"[XMLParser] Stage3 completed (include_candidates={include_candidates}, updated=0)"
        )
        return {"include_candidates": include_candidates, "updated": 0}

    update_sql = f"""
        UPDATE {table}
        SET EDIT_FR_SQL = :edit_fr_sql,
            UPD_TS = CURRENT_TIMESTAMP
        WHERE TO_CHAR(SPACE_NM) = :space_nm
          AND TO_CHAR(SQL_ID) = :sql_id
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.setinputsizes(
            edit_fr_sql=oracledb.DB_TYPE_CLOB,
            space_nm=oracledb.DB_TYPE_VARCHAR,
            sql_id=oracledb.DB_TYPE_VARCHAR,
        )
        for edit_fr_sql, space_nm, sql_id in updates:
            cursor.execute(
                update_sql,
                {"edit_fr_sql": edit_fr_sql, "space_nm": space_nm, "sql_id": sql_id},
            )
        conn.commit()

    logger.info(
        f"[XMLParser] Stage3 completed (include_candidates={include_candidates}, updated={len(updates)})"
    )
    return {"include_candidates": include_candidates, "updated": len(updates)}



def _validate_sql_identifier(name: str) -> str:
    """유틸 SQL에서 식별자 인젝션을 막기 위해 허용 문자만 통과시킨다."""
    normalized = (name or "").strip()
    if not normalized:
        raise ValueError("SQL identifier is empty.")
    if not re.fullmatch(r"[A-Za-z0-9_$.#]+", normalized):
        raise ValueError(f"Unsafe SQL identifier: {normalized}")
    return normalized


def _parse_stored_target_table(value: Any) -> list[str]:
    """TARGET_TABLE(JSON 배열/CSV 문자열)를 테이블 목록으로 복원한다."""
    text = _to_text(value).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            result = []
            for item in parsed:
                normalized = _normalize_table_name(_to_text(item))
                if normalized and _is_valid_target_table_name(normalized):
                    result.append(normalized)
            return result
    except Exception:
        pass
    # Fallback: CSV-like storage.
    items = []
    for token in re.split(r"[,\s]+", text):
        normalized = _normalize_table_name(token)
        if normalized and _is_valid_target_table_name(normalized):
            items.append(normalized)
    return items


def _strip_sql_for_table_parse(sql_text: str) -> str:
    """테이블명 파싱 전 주석/리터럴/MyBatis 토큰/XML 태그를 제거한다."""
    text = _to_text(sql_text)
    text = _SQL_BLOCK_COMMENT_PATTERN.sub(" ", text)
    text = _SQL_LINE_COMMENT_PATTERN.sub(" ", text)
    text = _SQL_SINGLE_QUOTE_PATTERN.sub(" ", text)
    text = _SQL_MYBATIS_PLACEHOLDER_PATTERN.sub(" ", text)
    text = _SQL_XML_TAG_PATTERN.sub(" ", text)
    return text


def _skip_balanced_parentheses(text: str, start_idx: int) -> int:
    """`(` 위치에서 시작해 짝이 맞는 `)` 다음 인덱스를 반환한다."""
    if start_idx >= len(text) or text[start_idx] != "(":
        return start_idx
    depth = 0
    idx = start_idx
    while idx < len(text):
        ch = text[idx]
        if ch == "(":
            depth += 1
            idx += 1
            continue
        if ch == ")":
            depth -= 1
            idx += 1
            if depth <= 0:
                return idx
            continue
        if ch == "'":
            idx += 1
            while idx < len(text):
                if text[idx] == "'":
                    idx += 1
                    if idx < len(text) and text[idx] == "'":
                        idx += 1
                        continue
                    break
                idx += 1
            continue
        idx += 1
    return idx


def _read_sql_identifier(text: str, start_idx: int) -> tuple[str, int]:
    """`SCHEMA.TABLE` 또는 `"TABLE"` 형태 식별자를 읽는다."""
    idx = start_idx
    length = len(text)
    parts: list[str] = []
    while idx < length:
        while idx < length and text[idx].isspace():
            idx += 1
        if idx >= length:
            break

        if text[idx] == '"':
            end = idx + 1
            while end < length and text[end] != '"':
                end += 1
            part = text[idx : min(end + 1, length)]
            idx = min(end + 1, length)
        else:
            match = re.match(r"[A-Z0-9_$#]+", text[idx:])
            if not match:
                break
            part = match.group(0)
            idx += len(part)

        parts.append(part)
        while idx < length and text[idx].isspace():
            idx += 1
        if idx < length and text[idx] == ".":
            parts.append(".")
            idx += 1
            continue
        break

    return "".join(parts), idx


def _extract_cte_names(sql_text: str) -> set[str]:
    """WITH 절에 선언된 CTE 이름을 추출한다."""
    upper = sql_text.upper().lstrip()
    match = re.match(r"WITH\b", upper)
    if not match:
        return set()

    cte_names: set[str] = set()
    idx = match.end()
    length = len(upper)
    while idx < length:
        while idx < length and upper[idx].isspace():
            idx += 1
        ident, idx = _read_sql_identifier(upper, idx)
        normalized = _normalize_table_name(ident)
        if not normalized:
            break

        while idx < length and upper[idx].isspace():
            idx += 1
        if idx < length and upper[idx] == "(":
            idx = _skip_balanced_parentheses(upper, idx)
        while idx < length and upper[idx].isspace():
            idx += 1
        if not upper[idx : idx + 2] == "AS":
            break
        cte_names.add(normalized)
        cte_names.add(normalized.split(".")[-1])
        idx += 2
        while idx < length and upper[idx].isspace():
            idx += 1
        if idx >= length or upper[idx] != "(":
            break
        idx = _skip_balanced_parentheses(upper, idx)
        while idx < length and upper[idx].isspace():
            idx += 1
        if idx < length and upper[idx] == ",":
            idx += 1
            continue
        break
    return cte_names


def _extract_all_cte_names(sql_text: str) -> set[str]:
    """SQL 전체에서 WITH 절에 선언된 CTE 이름을 모두 추출한다."""
    cleaned = _strip_sql_for_table_parse(sql_text)
    cte_names: set[str] = set()
    for match in re.finditer(r"\bWITH\b", cleaned, flags=re.IGNORECASE):
        cte_names.update(_extract_cte_names(cleaned[match.start() :]))
    return cte_names


def _remove_cte_names_from_target_tables(target_tables: list[str], sql_text: str) -> list[str]:
    """TARGET_TABLE 목록에서 WITH 절 CTE 이름을 제거한다."""
    cte_names = _extract_all_cte_names(sql_text)
    if not cte_names:
        return target_tables

    filtered: list[str] = []
    for table_name in target_tables:
        normalized = _normalize_table_name(table_name)
        table_short = normalized.split(".")[-1].strip('"') if normalized else ""
        if normalized in cte_names or table_short in cte_names:
            continue
        filtered.append(table_name)
    return filtered


def _split_top_level_commas(text: str) -> list[str]:
    """괄호/문자열 내부 comma를 보존하면서 최상위 comma만 분리한다."""
    chunks: list[str] = []
    start = 0
    depth = 0
    in_quote = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if in_quote:
            if ch == "'":
                if idx + 1 < len(text) and text[idx + 1] == "'":
                    idx += 2
                    continue
                in_quote = False
            idx += 1
            continue
        if ch == "'":
            in_quote = True
            idx += 1
            continue
        if ch == "(":
            depth += 1
            idx += 1
            continue
        if ch == ")":
            if depth > 0:
                depth -= 1
            idx += 1
            continue
        if ch == "," and depth == 0:
            chunks.append(text[start:idx])
            start = idx + 1
        idx += 1
    chunks.append(text[start:])
    return chunks


def _starts_from_clause_stop(text: str, idx: int) -> bool:
    """FROM 절 종료 키워드가 현재 위치에서 시작하는지 확인한다."""
    return bool(
        re.match(
            r"\s+(WHERE|GROUP|ORDER|HAVING|CONNECT|START|UNION|MINUS|INTERSECT)\b",
            text[idx:],
            flags=re.IGNORECASE,
        )
    )


def _extract_parenthesized_sql_segments(text: str) -> list[str]:
    """괄호 안에 있는 SELECT/FROM SQL 조각을 재귀 파싱 후보로 추출한다."""
    segments: list[str] = []
    idx = 0
    while idx < len(text):
        if text[idx] != "(":
            idx += 1
            continue
        end = _skip_balanced_parentheses(text, idx)
        if end <= idx + 1:
            idx += 1
            continue
        inner = text[idx + 1 : end - 1].strip()
        if re.search(r"\b(SELECT|FROM|WITH)\b", inner, flags=re.IGNORECASE):
            segments.append(inner)
        idx = end
    return segments


def _extract_from_clause_tables(sql_text: str, depth: int = 0) -> list[str]:
    """FROM 절의 comma join 패턴을 포함해 테이블명을 추출한다."""
    if depth > 20:
        return []

    tables: list[str] = []
    idx = 0
    upper = sql_text.upper()
    while True:
        match = re.search(r"\bFROM\b", upper[idx:])
        if not match:
            break
        pos = idx + match.start() + len("FROM")
        while pos < len(upper) and upper[pos].isspace():
            pos += 1
        if pos >= len(upper):
            break
        if upper[pos] == "(":
            idx = pos + 1
            continue

        end = pos
        depth = 0
        while end < len(upper):
            ch = upper[end]
            if ch == "(":
                depth += 1
                end += 1
                continue
            if ch == ")":
                if depth > 0:
                    depth -= 1
                end += 1
                continue
            if depth == 0:
                if _starts_from_clause_stop(upper, end):
                    break
            end += 1

        clause = upper[pos:end]
        for chunk in _split_top_level_commas(clause):
            token = chunk.strip()
            if not token:
                continue
            if token.startswith("("):
                for inner_sql in _extract_parenthesized_sql_segments(token):
                    tables.extend(_extract_from_clause_tables(inner_sql, depth + 1))
                continue
            ident, _ = _read_sql_identifier(token, 0)
            after_ident = token[len(ident) :].lstrip() if ident else ""
            if after_ident.startswith("("):
                continue
            normalized = _normalize_table_name(ident)
            if normalized:
                tables.append(normalized)
            for inner_sql in _extract_parenthesized_sql_segments(token):
                tables.extend(_extract_from_clause_tables(inner_sql, depth + 1))

        idx = end
    return tables


def _extract_target_tables_from_sql(sql_text: str) -> list[str]:
    """SQL에서 target table 후보를 추출한다. EDIT_FR_SQL 우선 입력을 권장한다."""
    cleaned = _strip_sql_for_table_parse(sql_text)
    upper = cleaned.upper()
    cte_names = _extract_cte_names(cleaned)
    ignored = _SQL_TABLE_NOISE_WORDS | cte_names

    candidates: list[str] = []

    def _add_candidate(token: str) -> None:
        normalized = _normalize_table_name(token)
        if not normalized or not _is_valid_target_table_name(normalized):
            return
        table_short = normalized.split(".")[-1].strip('"')
        if normalized in ignored or table_short in ignored:
            return
        if normalized not in candidates:
            candidates.append(normalized)

    for pattern in (
        r"\bUPDATE\s+([A-Z0-9_$#\"\.]+)",
        r"\bINSERT\s+INTO\s+([A-Z0-9_$#\"\.]+)",
        r"\bDELETE\s+FROM\s+([A-Z0-9_$#\"\.]+)",
        r"\bMERGE\s+INTO\s+([A-Z0-9_$#\"\.]+)",
        r"\bJOIN\s+([A-Z0-9_$#\"\.]+)",
    ):
        for match in re.finditer(pattern, upper):
            _add_candidate(match.group(1))

    for table_name in _extract_from_clause_tables(cleaned):
        _add_candidate(table_name)

    return candidates


def _schema_table_parts(table_name: str) -> tuple[str, str] | None:
    """SCHEMA.TABLE 토큰을 (schema, table)로 분해한다."""
    normalized = _normalize_table_name(table_name)
    if "." not in normalized:
        return None
    parts = [part.strip().strip('"') for part in normalized.split(".") if part.strip()]
    if len(parts) < 2:
        return None
    schema_name = parts[-2]
    table_short = parts[-1]
    if not schema_name or not table_short:
        return None
    return schema_name, table_short


def _collect_schema_table_pairs(target_table_value: Any, *sql_texts: Any) -> list[tuple[str, str]]:
    """TARGET_TABLE/SQL 테이블 후보에서 스키마가 붙은 테이블 목록을 모은다."""
    pairs: list[tuple[str, str]] = []
    seen = set()

    def _add_candidates(candidates: list[str]) -> None:
        for candidate in candidates:
            parts = _schema_table_parts(candidate)
            if not parts or parts in seen:
                continue
            pairs.append(parts)
            seen.add(parts)

    if isinstance(target_table_value, (list, tuple, set)):
        _add_candidates([_to_text(item) for item in target_table_value])
    else:
        _add_candidates(_parse_stored_target_table(target_table_value))

    for sql_text in sql_texts:
        _add_candidates(_extract_target_tables_from_sql(_to_text(sql_text)))

    return pairs


def _replace_schema_table_segment(segment: str, pairs: list[tuple[str, str]]) -> str:
    """보호되지 않은 SQL 조각에서 SCHEMA.TABLE을 TABLE로 치환한다."""
    result = segment
    # 긴 이름부터 처리해 부분 치환 가능성을 낮춘다.
    for schema_name, table_name in sorted(pairs, key=lambda item: len(".".join(item)), reverse=True):
        unquoted = re.compile(
            rf"(?<![A-Za-z0-9_$#\"])"
            rf"({re.escape(schema_name)})\s*\.\s*({re.escape(table_name)})"
            rf"(?![A-Za-z0-9_$#\"])",
            flags=re.IGNORECASE,
        )
        result = unquoted.sub(lambda match: match.group(2), result)

        quoted = re.compile(
            rf'"{re.escape(schema_name)}"\s*\.\s*"{re.escape(table_name)}"',
            flags=re.IGNORECASE,
        )
        result = quoted.sub(lambda match: f'"{table_name}"', result)
    return result


def _strip_schema_from_sql_text(sql_text: str, pairs: list[tuple[str, str]]) -> str:
    """SQL 텍스트에서 테이블 식별자의 스키마 접두어만 제거한다."""
    text = _to_text(sql_text)
    if not text or not pairs:
        return text

    parts: list[str] = []
    last_end = 0
    for match in _SQL_SCHEMA_STRIP_PROTECTED_PATTERN.finditer(text):
        parts.append(_replace_schema_table_segment(text[last_end : match.start()], pairs))
        parts.append(match.group(0))
        last_end = match.end()
    parts.append(_replace_schema_table_segment(text[last_end:], pairs))
    return "".join(parts)


def strip_schema_qualifiers_from_next_sql_info() -> dict[str, int]:
    """Stage5: TARGET_TABLE/FR_SQL_TEXT/EDIT_FR_SQL에서 스키마명을 제거하고 READY 처리한다."""
    result_table = get_result_table()
    logger.info("[XMLParser] Stage5 started")

    rows: list[tuple[str, str, str, str]] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT ROWIDTOCHAR(ROWID), TARGET_TABLE, FR_SQL_TEXT, EDIT_FR_SQL
            FROM {result_table}
            """
        )
        for row in cursor.fetchall():
            rows.append(
                (
                    _to_text(row[0]).strip(),
                    _to_text(row[1]),
                    _to_text(row[2]),
                    _to_text(row[3]),
                )
            )

    updates: list[tuple[str, str, str, str]] = []
    updated_target_table = 0
    updated_fr_sql_text = 0
    updated_edit_fr_sql = 0

    for rowid, target_table_value, fr_sql_text, edit_fr_sql in rows:
        pairs = _collect_schema_table_pairs(target_table_value, fr_sql_text, edit_fr_sql)
        stripped_target_tables = _strip_schema_from_target_tables(target_table_value)
        serialized_target_table = (
            json.dumps(stripped_target_tables, ensure_ascii=False) if stripped_target_tables else ""
        )
        stripped_fr_sql_text = _strip_schema_from_sql_text(fr_sql_text, pairs)
        stripped_edit_fr_sql = _strip_schema_from_sql_text(edit_fr_sql, pairs)

        current_target_table = target_table_value.strip()
        if (
            serialized_target_table == current_target_table
            and stripped_fr_sql_text == fr_sql_text
            and stripped_edit_fr_sql == edit_fr_sql
        ):
            continue

        if serialized_target_table != current_target_table:
            updated_target_table += 1
        if stripped_fr_sql_text != fr_sql_text:
            updated_fr_sql_text += 1
        if stripped_edit_fr_sql != edit_fr_sql:
            updated_edit_fr_sql += 1
        updates.append((serialized_target_table, stripped_fr_sql_text, stripped_edit_fr_sql, rowid))

    if updates:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.setinputsizes(
                target_table=oracledb.DB_TYPE_CLOB,
                fr_sql_text=oracledb.DB_TYPE_CLOB,
                edit_fr_sql=oracledb.DB_TYPE_CLOB,
                rid=oracledb.DB_TYPE_VARCHAR,
            )
            cursor.executemany(
                f"""
                UPDATE {result_table}
                SET TARGET_TABLE = :target_table,
                    FR_SQL_TEXT = :fr_sql_text,
                    EDIT_FR_SQL = :edit_fr_sql,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE ROWID = CHARTOROWID(:rid)
                """,
                [
                    {
                        "target_table": target_table,
                        "fr_sql_text": fr_sql_text,
                        "edit_fr_sql": edit_fr_sql,
                        "rid": rowid,
                    }
                    for target_table, fr_sql_text, edit_fr_sql, rowid in updates
                ],
            )
            conn.commit()

    ready_updated = 0
    if updates:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                f"""
                UPDATE {result_table}
                SET STATUS = 'READY',
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE ROWID = CHARTOROWID(:1)
                """,
                [(rowid,) for _target_table, _fr_sql_text, _edit_fr_sql, rowid in updates],
            )
            ready_updated = max(int(cursor.rowcount or 0), 0)
            conn.commit()

    logger.info(
        "[XMLParser] Stage5 completed "
        f"(updated_rows={len(updates)}, "
        f"updated_target_table={updated_target_table}, "
        f"updated_fr_sql_text={updated_fr_sql_text}, "
        f"updated_edit_fr_sql={updated_edit_fr_sql}, "
        f"ready_updated={ready_updated})"
    )
    return {
        "updated_rows": len(updates),
        "updated_target_table": updated_target_table,
        "updated_fr_sql_text": updated_fr_sql_text,
        "updated_edit_fr_sql": updated_edit_fr_sql,
        "ready_updated": ready_updated,
    }


def cleanup_next_sql_info_rows() -> dict[str, int]:
    """Stage4: 비활성 SQL을 정리하고 TARGET_TABLE을 보정한다."""
    result_table = get_result_table()
    active_table = _validate_sql_identifier(_require_env("ACTIVE_SQL_ID_TABLE"))
    active_column = _validate_sql_identifier(os.getenv("ACTIVE_SQL_ID_COLUMN", "SQL_ID"))

    logger.info(
        "[XMLParser] Stage4 started "
        f"(active_table={active_table}, active_column={active_column})"
    )

    # ACTIVE 테이블은 반드시 full id(NAMESPACE.SQL_ID) 형식을 사용해야 한다.
    # SQL_ID 단독 값은 namespace 충돌 위험이 있어 허용하지 않는다.
    active_full_ids: set[str] = set()
    invalid_ids: list[str] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT TO_CHAR({active_column}) FROM {active_table}")
        for (value,) in cursor.fetchall():
            raw_id = _to_text(value).strip()
            if not raw_id:
                continue
            normalized = raw_id.upper()
            if "." not in normalized:
                if len(invalid_ids) < 10:
                    invalid_ids.append(normalized)
                continue
            active_full_ids.add(normalized)

    if invalid_ids:
        sample = ", ".join(invalid_ids[:5])
        raise ValueError(
            "ACTIVE_SQL_ID_COLUMN must contain full id format 'NAMESPACE.SQL_ID'. "
            f"Invalid sample values: {sample}"
        )

    rows: list[tuple[str, str, str, str, str, str]] = []
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""
            SELECT ROWIDTOCHAR(ROWID), TO_CHAR(SPACE_NM), TO_CHAR(SQL_ID),
                   TARGET_TABLE, FR_SQL_TEXT, EDIT_FR_SQL
            FROM {result_table}
            """
        )
        for row in cursor.fetchall():
            rows.append(
                (
                    _to_text(row[0]).strip(),
                    _to_text(row[1]).strip(),
                    _to_text(row[2]).strip(),
                    _to_text(row[3]),
                    _to_text(row[4]),
                    _to_text(row[5]),
                )
            )

    to_delete_rowids: list[str] = []
    target_table_updates: list[tuple[str, str]] = []
    deleted_not_active = 0
    updated_target_table = 0

    for rowid, space_nm, sql_id, target_table_value, fr_sql_text, edit_fr_sql in rows:
        space_text = space_nm
        sql_text = sql_id
        full_id = f"{space_text}.{sql_text}".upper()
        if full_id not in active_full_ids:
            to_delete_rowids.append(rowid)
            deleted_not_active += 1
            continue

        stored_target_tables = _parse_stored_target_table(target_table_value)
        base_sql = (edit_fr_sql.strip() or fr_sql_text)
        parsed_target_tables = _extract_target_tables_from_sql(base_sql)
        target_tables = parsed_target_tables if parsed_target_tables else stored_target_tables
        target_tables = _remove_cte_names_from_target_tables(target_tables, base_sql)
        serialized_target_table = json.dumps(target_tables, ensure_ascii=False) if target_tables else ""
        current_serialized = target_table_value.strip()
        if serialized_target_table != current_serialized:
            target_table_updates.append((serialized_target_table, rowid))
            updated_target_table += 1

    if target_table_updates:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                f"""
                UPDATE {result_table}
                SET TARGET_TABLE = :1,
                    UPD_TS = CURRENT_TIMESTAMP
                WHERE ROWID = CHARTOROWID(:2)
                """,
                target_table_updates,
            )
            conn.commit()

    if to_delete_rowids:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(
                f"DELETE FROM {result_table} WHERE ROWID = CHARTOROWID(:1)",
                [(rid,) for rid in to_delete_rowids],
            )
            conn.commit()

    remaining_total = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(f"SELECT COUNT(*) FROM {result_table}")
        remaining_total = int(cursor.fetchone()[0] or 0)

    logger.info(
        "[XMLParser] Stage4 completed "
        f"(updated_target_table={updated_target_table}, "
        f"deleted_total={len(to_delete_rowids)}, "
        f"deleted_not_active={deleted_not_active}, "
        f"remaining_total={remaining_total})"
    )
    return {
        "updated_target_table": updated_target_table,
        "deleted_total": len(to_delete_rowids),
        "deleted_not_active": deleted_not_active,
        "remaining_total": remaining_total,
    }


def run_all_xml_parser_stages(
    source_dir: str | None = None,
    output_dir: str | None = None,
) -> dict[str, dict[str, int]]:
    """stage1~stage5를 순서대로 실행하고 통계를 반환한다."""
    stage1 = parse_mapper_dir_to_json(source_dir=source_dir, output_dir=output_dir)
    stage2 = upsert_json_to_next_sql_info(data_dir=output_dir)
    stage3 = expand_include_to_edit_sql()
    stage4 = cleanup_next_sql_info_rows()
    stage5 = strip_schema_qualifiers_from_next_sql_info()
    return {
        "stage1": stage1,
        "stage2": stage2,
        "stage3": stage3,
        "stage4": stage4,
        "stage5": stage5,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    """단일 stage 또는 all 실행용 CLI 인자 파서를 만든다."""
    parser = argparse.ArgumentParser(description="MyBatis XML parser utility stages")
    parser.add_argument(
        "stage",
        choices=["stage1", "stage2", "stage3", "stage4", "stage5", "all"],
        help="Stage to run",
    )
    parser.add_argument("--source-dir", dest="source_dir", default=None, help="Mapper XML source directory")
    parser.add_argument("--output-dir", dest="output_dir", default=None, help="JSON output directory")
    return parser


def _main():
    """XML parser 유틸 CLI 진입점."""
    parser = _build_arg_parser()
    args = parser.parse_args()

    if args.stage == "stage1":
        parse_mapper_dir_to_json(source_dir=args.source_dir, output_dir=args.output_dir)
        return
    if args.stage == "stage2":
        upsert_json_to_next_sql_info(data_dir=args.output_dir)
        return
    if args.stage == "stage3":
        expand_include_to_edit_sql()
        return
    if args.stage == "stage4":
        cleanup_next_sql_info_rows()
        return
    if args.stage == "stage5":
        strip_schema_qualifiers_from_next_sql_info()
        return
    run_all_xml_parser_stages(source_dir=args.source_dir, output_dir=args.output_dir)


if __name__ == "__main__":
    _main()



