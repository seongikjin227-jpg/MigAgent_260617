"""NEXT_MIG_RAG_INFO CRUD helpers."""

import os

import oracledb

from utils.db import get_connection, _s


RAG_TABLE = os.getenv("RAG_INFO_TABLE", "NEXT_MIG_RAG_INFO").strip() or "NEXT_MIG_RAG_INFO"
CATEGORIES = ("SQL_CONVERSION", "SQL_TUNING")
RULE_TYPES = ("SEARCH", "GENERAL")


def _normalize_category(value: str) -> str:
    normalized = (value or "").strip().upper()
    return normalized if normalized in CATEGORIES else "SQL_TUNING"


def _normalize_rule_type(value: str) -> str:
    normalized = (value or "").strip().upper()
    return normalized if normalized in RULE_TYPES else "SEARCH"


def get_all_rules() -> list[dict]:
    q = f"""
        SELECT RAG_ID, CATEGORY, RULE_TYPE, SOURCE_TABLES, USE_YN,
               GUIDANCE_TEXT, SOURCE_SQL, TARGET_SQL,
               NVL(HIT_CNT, 0) AS HIT_CNT,
               TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS CREATED_AT,
               TO_CHAR(UPDATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS UPDATED_AT
        FROM {RAG_TABLE}
        ORDER BY CATEGORY, RULE_TYPE, RAG_ID
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q)
        cols = [d[0] for d in cur.description]
        return [{cols[i]: _s(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


def get_top_rules(limit: int = 5) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT RAG_ID, CATEGORY, RULE_TYPE, SOURCE_TABLES,
                   GUIDANCE_TEXT, SOURCE_SQL, TARGET_SQL,
                   NVL(HIT_CNT, 0) AS HIT_CNT
            FROM {RAG_TABLE}
            WHERE NVL(HIT_CNT, 0) > 0
              AND UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
            ORDER BY HIT_CNT DESC NULLS LAST, UPDATED_AT DESC NULLS LAST
        )
        WHERE ROWNUM <= :1
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, (int(limit),))
        cols = [d[0] for d in cur.description]
        return [{cols[i]: _s(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


def add_rule(
    category: str,
    rule_type: str,
    source_tables: str,
    guidance_text: str,
    source_sql: str,
    target_sql: str = "",
    use_yn: str = "Y",
) -> None:
    q = f"""
        INSERT INTO {RAG_TABLE}
            (CATEGORY, RULE_TYPE, SOURCE_TABLES, USE_YN, GUIDANCE_TEXT, SOURCE_SQL, TARGET_SQL)
        VALUES (:category, :rule_type, :source_tables, :use_yn, :guidance_text, :source_sql, :target_sql)
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.setinputsizes(guidance_text=oracledb.CLOB, source_sql=oracledb.CLOB, target_sql=oracledb.CLOB)
        cur.execute(
            q,
            {
                "category": _normalize_category(category),
                "rule_type": _normalize_rule_type(rule_type),
                "source_tables": (source_tables or "").strip().upper(),
                "use_yn": "Y" if (use_yn or "Y").strip().upper() == "Y" else "N",
                "guidance_text": guidance_text or "",
                "source_sql": source_sql or "",
                "target_sql": target_sql or "",
            },
        )
        conn.commit()


def update_rule(
    rag_id: str | int,
    category: str,
    rule_type: str,
    source_tables: str,
    guidance_text: str,
    source_sql: str,
    target_sql: str = "",
    use_yn: str = "Y",
) -> None:
    q = f"""
        UPDATE {RAG_TABLE}
        SET CATEGORY = :category,
            RULE_TYPE = :rule_type,
            SOURCE_TABLES = :source_tables,
            USE_YN = :use_yn,
            GUIDANCE_TEXT = :guidance_text,
            SOURCE_SQL = :source_sql,
            TARGET_SQL = :target_sql,
            UPDATED_AT = SYSTIMESTAMP
        WHERE RAG_ID = :rag_id
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.setinputsizes(guidance_text=oracledb.CLOB, source_sql=oracledb.CLOB, target_sql=oracledb.CLOB)
        cur.execute(
            q,
            {
                "category": _normalize_category(category),
                "rule_type": _normalize_rule_type(rule_type),
                "source_tables": (source_tables or "").strip().upper(),
                "use_yn": "Y" if (use_yn or "Y").strip().upper() == "Y" else "N",
                "guidance_text": guidance_text or "",
                "source_sql": source_sql or "",
                "target_sql": target_sql or "",
                "rag_id": int(rag_id),
            },
        )
        conn.commit()


def delete_rule(rag_id: str | int) -> bool:
    q = f"DELETE FROM {RAG_TABLE} WHERE RAG_ID = :1"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, (int(rag_id),))
        deleted = cur.rowcount
        conn.commit()
    return deleted > 0
