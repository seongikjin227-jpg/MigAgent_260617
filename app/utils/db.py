import os
import time
import oracledb
from pathlib import Path
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")

oracledb.defaults.fetch_lobs = False

DB_USER = os.getenv("DB_USER", "scott")
DB_PASS = os.getenv("DB_PASS", "tiger")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "1521")
DB_SID  = os.getenv("DB_SID", "xe")
ORACLE_CLIENT_PATH = os.getenv("ORACLE_CLIENT_PATH", "")

MIG_TABLE    = os.getenv("MAPPING_RULE_TABLE", "NEXT_MIG_INFO")
MIG_DTL_TABLE = os.getenv("MAPPING_RULE_DETAIL_TABLE", "NEXT_MIG_INFO_DTL").strip()
SQL_TABLE    = os.getenv("RESULT_TABLE", "NEXT_SQL_INFO")
AGENT_METRICS_TABLE = os.getenv("AGENT_METRICS_TABLE", "AG_AGENT_RUN_METRICS")
SQL_LOG_TABLE = os.getenv("SQL_LOG_TABLE", "NEXT_SQL_LOG")

CONVERSION_PASS_STATUSES = ("PASS", "CONVERSION-PASS")
TUNING_PASS_STATUSES = ("PASS", "TUNING-PASS", "TUNING_PASS", "PASS_NON_SELECT")
CONVERSION_FAIL_STATUSES = ("FAIL", "FAIL-TOBE", "FAIL-BIND", "FAIL-TEST")
TUNING_FAIL_STATUSES = ("FAIL", "FAIL-TUNED", "FAIL-BIND", "FAIL-TEST")
FAIL_STATUSES = ("FAIL", "FAIL-TOBE", "FAIL-TUNED", "FAIL-BIND", "FAIL-TEST")

_thick_done = False
_AVAILABLE_COLUMNS_CACHE: dict[str, set[str]] = {}


def _sql_in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def get_connection():
    global _thick_done
    if ORACLE_CLIENT_PATH and os.path.exists(ORACLE_CLIENT_PATH) and not _thick_done:
        try:
            oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_PATH)
        except oracledb.ProgrammingError:
            pass
        _thick_done = True
    dsn = DB_HOST if ("/" in DB_HOST or "(" in DB_HOST) else f"{DB_HOST}:{DB_PORT}/{DB_SID}"
    conn = oracledb.connect(user=DB_USER, password=DB_PASS, dsn=dsn)
    with conn.cursor() as cur:
        cur.execute("ALTER SESSION SET NLS_DATE_FORMAT='YYYY-MM-DD HH24:MI:SS'")
    return conn


def _s(val, default="") -> str:
    if val is None:
        return default
    if hasattr(val, "read"):
        val = val.read()
    if val is None:
        return default
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="ignore")
    return str(val)


def _to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [{cols[i]: _s(row[i]) for i in range(len(cols))} for row in cur.fetchall()]


def _split_table_owner_and_name(table: str) -> tuple[str | None, str]:
    value = (table or "").strip().upper()
    if "." in value:
        owner, table_name = value.split(".", 1)
        return owner.strip('"'), table_name.strip('"')
    return None, value.strip('"')


def _get_available_columns(table: str) -> set[str]:
    owner, table_name = _split_table_owner_and_name(table)
    cache_key = f"{owner or ''}.{table_name}"
    if cache_key in _AVAILABLE_COLUMNS_CACHE:
        return _AVAILABLE_COLUMNS_CACHE[cache_key]

    if owner:
        q = """
            SELECT COLUMN_NAME
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :1
              AND TABLE_NAME = :2
        """
        params = (owner, table_name)
    else:
        q = """
            SELECT COLUMN_NAME
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :1
        """
        params = (table_name,)

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q, params)
        columns = {_s(row[0]).upper() for row in cur.fetchall()}

    _AVAILABLE_COLUMNS_CACHE[cache_key] = columns
    return columns


def _optional_column_expr(column_name: str, available_columns: set[str], data_type: str = "VARCHAR2(4000)") -> str:
    column = column_name.upper()
    if column in available_columns:
        return column
    return f"CAST(NULL AS {data_type}) AS {column}"


# ── Mig ──────────────────────────────────────────────────────────────────────

def get_mig_jobs() -> list[dict]:
    q = f"""
        SELECT MAP_ID, MAP_TYPE, FR_TABLE, TO_TABLE,
               USE_YN, TARGET_YN, PRIORITY, STATUS,
               PRIOR_MAP_ID, MIG_SQL, VERIFY_SQL,
               BATCH_CNT, ELAPSED_SECONDS, RETRY_COUNT,
               TO_CHAR(CREATED_AT) AS CREATED_AT,
               TO_CHAR(UPD_TS) AS UPD_TS
        FROM {MIG_TABLE}
        ORDER BY PRIORITY ASC, MAP_ID ASC
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q)
        return _to_dicts(cur)


def get_mig_status_summary() -> dict[str, int]:
    q = f"SELECT NVL(TO_CHAR(STATUS),'NULL'), COUNT(*) FROM {MIG_TABLE} GROUP BY STATUS"
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(q)
        return {_s(r[0]) or "NULL": r[1] for r in cur.fetchall()}


def get_mig_dtl(map_id: int) -> list[dict]:
    q = f"""
        SELECT MAP_DTL, FR_COL, TO_COL
        FROM {MIG_DTL_TABLE}
        WHERE MAP_ID = :1
        ORDER BY MAP_DTL
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (map_id,))
            return _to_dicts(cur)
    except Exception:
        return []


def get_mig_logs(map_id: int) -> list[dict]:
    q = """
        SELECT LOG_ID, MIG_KIND, LOG_TYPE, LOG_LEVEL,
               STEP_NAME, STATUS, MESSAGE, RETRY_COUNT
        FROM NEXT_MIG_LOG
        WHERE MAP_ID = :1
        ORDER BY LOG_ID ASC
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (map_id,))
            return _to_dicts(cur)
    except Exception:
        return []


def get_recent_fails(limit: int = 10) -> list[dict]:
    q = f"""
        SELECT * FROM (
            SELECT MAP_ID, FR_TABLE, TO_TABLE, STATUS,
                   TO_CHAR(UPD_TS) AS UPD_TS
            FROM {MIG_TABLE}
            WHERE UPPER(NVL(STATUS,'X')) = 'FAIL'
            ORDER BY UPD_TS DESC NULLS LAST
        ) WHERE ROWNUM <= {limit}
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return _to_dicts(cur)
    except Exception:
        return []


# ── Tuning 전용 요약 ──────────────────────────────────────────────────────────

def get_tuning_status_summary() -> dict[str, int]:
    """TUNED_TEST 컬럼 기준 상태 요약 (SQL이 변환된 행만)."""
    q = f"""
        SELECT NVL(TO_CHAR(TUNED_TEST), 'NULL'), COUNT(*)
        FROM {SQL_TABLE}
        WHERE TO_SQL_TEXT IS NOT NULL
        GROUP BY TUNED_TEST
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return {_s(r[0]) or "NULL": r[1] for r in cur.fetchall()}
    except Exception:
        return {}


def get_formatting_summary() -> dict[str, int]:
    """Return formatting guide application counts for completed tuning rows."""
    q = f"""
        SELECT
            COUNT(*) AS TOTAL,
            SUM(
                CASE
                    WHEN FORMATTED_SQL IS NOT NULL
                     AND DBMS_LOB.GETLENGTH(FORMATTED_SQL) > 0
                    THEN 1
                    ELSE 0
                END
            ) AS APPLIED
        FROM {SQL_TABLE}
        WHERE UPPER(TRIM(TUNED_TEST)) IN ({_sql_in(TUNING_PASS_STATUSES)})
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            row = cur.fetchone()
            total = int(row[0] or 0) if row else 0
            applied = int(row[1] or 0) if row else 0
            return {
                "TOTAL": total,
                "APPLIED": applied,
                "PENDING": max(total - applied, 0),
            }
    except Exception:
        return {}


# ── SQL / Tuning ──────────────────────────────────────────────────────────────

def get_sql_jobs() -> list[dict]:
    available_columns = _get_available_columns(SQL_TABLE)
    target_table_column = _optional_column_expr("TARGET_TABLE", available_columns)
    edit_fr_sql_column = _optional_column_expr("EDIT_FR_SQL", available_columns)
    tuned_sql_column = _optional_column_expr("TUNED_SQL", available_columns)
    tuned_test_column = _optional_column_expr("TUNED_TEST", available_columns)
    tuned_result_column = _optional_column_expr("TUNED_RESULT", available_columns)
    formatted_sql_column = _optional_column_expr("FORMATTED_SQL", available_columns)
    block_rag_column = _optional_column_expr("BLOCK_RAG_CONTENT", available_columns)
    tobe_correct_column = _optional_column_expr("TOBE_CORRECT_SQL", available_columns)
    bind_correct_column = _optional_column_expr("BIND_CORRECT_SQL", available_columns)
    test_correct_column = _optional_column_expr("TEST_CORRECT_SQL", available_columns)
    sql_length_column = _optional_column_expr("SQL_LENGTH", available_columns)
    map_type_column = _optional_column_expr("MAP_TYPE", available_columns)
    edited_yn_column = _optional_column_expr("EDITED_YN", available_columns)
    edit_len_expr = "DBMS_LOB.GETLENGTH(EDIT_FR_SQL)" if "EDIT_FR_SQL" in available_columns else "0"
    tuned_len_expr = "DBMS_LOB.GETLENGTH(TUNED_SQL)" if "TUNED_SQL" in available_columns else "0"
    formatted_len_expr = "DBMS_LOB.GETLENGTH(FORMATTED_SQL)" if "FORMATTED_SQL" in available_columns else "0"

    q = f"""
        SELECT ROWIDTOCHAR(ROWID) AS ROW_ID,
               TAG_KIND, SPACE_NM, SQL_ID,
               FR_SQL_TEXT, {edit_fr_sql_column}, {target_table_column},
               TO_SQL_TEXT, {tuned_sql_column}, {tuned_test_column}, {tuned_result_column},
               {formatted_sql_column}, {block_rag_column},
               {tobe_correct_column}, {bind_correct_column}, {test_correct_column},
               {sql_length_column}, {map_type_column}, {edited_yn_column},
               DBMS_LOB.GETLENGTH(FR_SQL_TEXT) AS FR_SQL_LEN,
               {edit_len_expr} AS EDIT_FR_SQL_LEN,
               CASE
                   WHEN NVL({edit_len_expr}, 0) > 0
                   THEN {edit_len_expr}
                   ELSE DBMS_LOB.GETLENGTH(FR_SQL_TEXT)
               END AS EFFECTIVE_FR_SQL_LEN,
               DBMS_LOB.GETLENGTH(TO_SQL_TEXT) AS TO_SQL_LEN,
               {tuned_len_expr} AS TUNED_SQL_LEN,
               {formatted_len_expr} AS FORMATTED_SQL_LEN,
               STATUS, LOG, TO_CHAR(UPD_TS) AS UPD_TS
        FROM {SQL_TABLE}
        ORDER BY UPD_TS DESC NULLS LAST
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_status_summary() -> dict[str, int]:
    q = f"SELECT NVL(TO_CHAR(STATUS),'NULL'), COUNT(*) FROM {SQL_TABLE} GROUP BY STATUS"
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return {_s(r[0]) or "NULL": r[1] for r in cur.fetchall()}
    except Exception:
        return {}


def get_sql_length_success_summary(short_limit: int = 5000) -> dict[str, dict[str, int]]:
    """Return SQL conversion PASS/FAIL success base split by effective SQL length."""
    q = f"""
        SELECT LENGTH_GROUP,
               SUM(CASE WHEN UPPER(TRIM(STATUS)) IN ({_sql_in(CONVERSION_PASS_STATUSES)}) THEN 1 ELSE 0 END) AS PASS_COUNT,
               SUM(CASE WHEN UPPER(TRIM(STATUS)) IN ({_sql_in(CONVERSION_FAIL_STATUSES)}) THEN 1 ELSE 0 END) AS FAIL_COUNT
        FROM (
            SELECT
                CASE
                    WHEN NVL(DBMS_LOB.GETLENGTH(FR_SQL_TEXT), 0) <= :1
                     AND (
                         EDIT_FR_SQL IS NULL
                         OR NVL(DBMS_LOB.GETLENGTH(EDIT_FR_SQL), 0) <= :1
                     )
                    THEN 'SHORT'
                    ELSE 'LONG'
                END AS LENGTH_GROUP,
                STATUS
            FROM {SQL_TABLE}
            WHERE UPPER(TRIM(NVL(STATUS, 'NULL'))) IN ({_sql_in(CONVERSION_PASS_STATUSES + CONVERSION_FAIL_STATUSES)})
        )
        GROUP BY LENGTH_GROUP
    """
    result = {
        "SHORT": {"PASS": 0, "FAIL": 0},
        "LONG": {"PASS": 0, "FAIL": 0},
    }
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(short_limit),))
            for group_name, pass_count, fail_count in cur.fetchall():
                key = _s(group_name).upper() or "LONG"
                result.setdefault(key, {"PASS": 0, "FAIL": 0})
                result[key]["PASS"] = int(pass_count or 0)
                result[key]["FAIL"] = int(fail_count or 0)
    except Exception:
        pass
    return result


def get_xml_export_sqls() -> list[dict]:
    """Return tuning rows used by XML export, including namespace status counts."""
    q = f"""
        SELECT SPACE_NM, TAG_KIND, SQL_ID, TUNED_TEST, FORMATTED_SQL
        FROM {SQL_TABLE}
        WHERE SPACE_NM IS NOT NULL
          AND SQL_ID IS NOT NULL
          AND (TUNED_TEST IS NOT NULL OR FORMATTED_SQL IS NOT NULL)
        ORDER BY SPACE_NM, SQL_ID
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q)
            return _to_dicts(cur)
    except Exception:
        return []


def get_tuned_pass_sqls() -> list[dict]:
    """Backward-compatible alias for older XML export callers."""
    return get_xml_export_sqls()


# ── Agent operation metrics ─────────────────────────────────────────────

def get_recent_agent_run_metrics(limit: int = 200) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT RUN_ID, BATCH_NO, CYCLE_NO, AGENT_NAME,
                   JOB_COUNT, SUCCESS_COUNT, FAIL_COUNT, SKIP_COUNT,
                   TO_CHAR(STARTED_AT, 'YYYY-MM-DD HH24:MI:SS') AS STARTED_AT,
                   TO_CHAR(FINISHED_AT, 'YYYY-MM-DD HH24:MI:SS') AS FINISHED_AT,
                   ELAPSED_SECONDS
            FROM {AGENT_METRICS_TABLE}
            ORDER BY RUN_ID DESC
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_agent_batch_summary(limit: int = 50) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT BATCH_NO,
                   MIN(STARTED_AT) AS STARTED_AT,
                   MAX(FINISHED_AT) AS FINISHED_AT,
                   ROUND((MAX(FINISHED_AT) - MIN(STARTED_AT)) * 86400, 3) AS WALL_SECONDS,
                   SUM(NVL(ELAPSED_SECONDS, 0)) AS SUM_AGENT_SECONDS,
                   SUM(NVL(JOB_COUNT, 0)) AS JOB_COUNT,
                   SUM(NVL(SUCCESS_COUNT, 0)) AS SUCCESS_COUNT,
                   SUM(NVL(FAIL_COUNT, 0)) AS FAIL_COUNT,
                   SUM(NVL(SKIP_COUNT, 0)) AS SKIP_COUNT,
                   COUNT(*) AS AGENT_RUNS
            FROM {AGENT_METRICS_TABLE}
            GROUP BY BATCH_NO
            ORDER BY BATCH_NO DESC
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            rows = []
            for r in cur.fetchall():
                rows.append({
                    "BATCH_NO": _s(r[0]),
                    "STARTED_AT": _s(r[1]),
                    "FINISHED_AT": _s(r[2]),
                    "WALL_SECONDS": _s(r[3]),
                    "SUM_AGENT_SECONDS": _s(r[4]),
                    "JOB_COUNT": _s(r[5]),
                    "SUCCESS_COUNT": _s(r[6]),
                    "FAIL_COUNT": _s(r[7]),
                    "SKIP_COUNT": _s(r[8]),
                    "AGENT_RUNS": _s(r[9]),
                })
            return rows
    except Exception:
        return []


def get_agent_name_summary(limit: int = 500) -> list[dict]:
    q = f"""
        SELECT AGENT_NAME,
               COUNT(*) AS RUN_COUNT,
               SUM(NVL(JOB_COUNT, 0)) AS JOB_COUNT,
               ROUND(AVG(NVL(ELAPSED_SECONDS, 0)), 3) AS AVG_SECONDS,
               ROUND(MIN(NVL(ELAPSED_SECONDS, 0)), 3) AS MIN_SECONDS,
               ROUND(MAX(NVL(ELAPSED_SECONDS, 0)), 3) AS MAX_SECONDS,
               SUM(NVL(SUCCESS_COUNT, 0)) AS SUCCESS_COUNT,
               SUM(NVL(FAIL_COUNT, 0)) AS FAIL_COUNT,
               SUM(NVL(SKIP_COUNT, 0)) AS SKIP_COUNT
        FROM (
            SELECT *
            FROM {AGENT_METRICS_TABLE}
            ORDER BY RUN_ID DESC
        )
        WHERE ROWNUM <= :1
        GROUP BY AGENT_NAME
        ORDER BY AVG_SECONDS DESC
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_stage_summary(limit: int = 100) -> list[dict]:
    q = f"""
        SELECT NVL(STAGE_NAME, SQL_KIND) AS STAGE_NAME,
               COUNT(*) AS LOG_COUNT,
               ROUND(AVG(NVL(ELAPSED_SECONDS, 0)), 3) AS AVG_SECONDS,
               ROUND(MIN(NVL(ELAPSED_SECONDS, 0)), 3) AS MIN_SECONDS,
               ROUND(MAX(NVL(ELAPSED_SECONDS, 0)), 3) AS MAX_SECONDS,
               SUM(CASE WHEN UPPER(NVL(STATUS, '')) IN ('PASS', 'SUCCESS', 'CONVERSION-PASS', 'TUNING-PASS', 'TUNING_PASS') THEN 1 ELSE 0 END) AS PASS_COUNT,
               SUM(CASE WHEN UPPER(NVL(STATUS, '')) IN ({_sql_in(FAIL_STATUSES)}) THEN 1 ELSE 0 END) AS FAIL_COUNT,
               SUM(CASE WHEN ERROR_MESSAGE IS NOT NULL THEN 1 ELSE 0 END) AS ERROR_COUNT
        FROM (
            SELECT *
            FROM {SQL_LOG_TABLE}
            ORDER BY LOG_ID DESC
        )
        WHERE ROWNUM <= :1
        GROUP BY NVL(STAGE_NAME, SQL_KIND)
        ORDER BY AVG_SECONDS DESC, LOG_COUNT DESC
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_recent_sql_stage_logs(limit: int = 100) -> list[dict]:
    q = f"""
        SELECT *
        FROM (
            SELECT LOG_ID,
                   TO_CHAR(CREATED_AT, 'YYYY-MM-DD HH24:MI:SS') AS CREATED_AT,
                   SPACE_NM, SQL_ID, SQL_KIND, STATUS, STAGE_NAME,
                   PROMPT_NAME, MODEL_NAME, BATCH_NO, CYCLE_NO,
                   ELAPSED_SECONDS, ATTEMPT_NO, ERROR_MESSAGE
            FROM {SQL_LOG_TABLE}
            ORDER BY LOG_ID DESC
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


# ── Re-run / 재실행 DB 초기화 ────────────────────────────────────────────────

def reset_mig_job_for_rerun(map_id: int) -> bool:
    """Migration 작업을 최우선 재실행 가능 상태로 초기화합니다."""
    q = f"""
        UPDATE {MIG_TABLE}
        SET USE_YN = 'Y',
            STATUS = 'URGENT',
            RETRY_COUNT = 0,
            MIG_SQL = NULL,
            UPD_TS = CURRENT_TIMESTAMP
        WHERE MAP_ID = :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(map_id),))
            rowcount = cur.rowcount
            conn.commit()
            return rowcount > 0
    except Exception:
        return False

def find_sql_job_spaces(sql_id: str) -> list[str]:
    """Return SPACE_NM values matching a SQL_ID, used to avoid ambiguous reruns."""
    q = f"""
        SELECT DISTINCT TO_CHAR(SPACE_NM) AS SPACE_NM
        FROM {SQL_TABLE}
        WHERE TO_CHAR(SQL_ID) = :1
        ORDER BY TO_CHAR(SPACE_NM)
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (sql_id,))
            return [str(row[0] or "") for row in cur.fetchall()]
    except Exception:
        return []


def reset_sql_conversion_job(sql_id: str, space_nm: str | None = None) -> int:
    """SQL 변환 작업을 URGENT 상태로 재설정합니다. 업데이트된 행 수를 반환합니다."""
    if space_nm:
        q = f"""
            UPDATE {SQL_TABLE}
            SET STATUS = 'URGENT',
                UPD_TS = CURRENT_TIMESTAMP
            WHERE TO_CHAR(SQL_ID) = :1
              AND TO_CHAR(SPACE_NM) = :2
        """
        params = (sql_id, space_nm)
    else:
        q = f"""
            UPDATE {SQL_TABLE}
            SET STATUS = 'URGENT',
                UPD_TS = CURRENT_TIMESTAMP
            WHERE TO_CHAR(SQL_ID) = :1
        """
        params = (sql_id,)
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, params)
            rowcount = cur.rowcount
            conn.commit()
            return rowcount
    except Exception:
        return 0


def reset_sql_tuning_job(sql_id: str, space_nm: str | None = None) -> int:
    """SQL 튜닝 작업을 URGENT 상태로 재설정합니다. 업데이트된 행 수를 반환합니다."""
    if space_nm:
        q = f"""
            UPDATE {SQL_TABLE}
            SET TUNED_TEST = 'URGENT',
                UPD_TS = CURRENT_TIMESTAMP
            WHERE TO_CHAR(SQL_ID) = :1
              AND TO_CHAR(SPACE_NM) = :2
        """
        params = (sql_id, space_nm)
    else:
        q = f"""
            UPDATE {SQL_TABLE}
            SET TUNED_TEST = 'URGENT',
                UPD_TS = CURRENT_TIMESTAMP
            WHERE TO_CHAR(SQL_ID) = :1
        """
        params = (sql_id,)
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, params)
            rowcount = cur.rowcount
            conn.commit()
            return rowcount
    except Exception:
        return 0


# ── 실패 로그 조회 ─────────────────────────────────────────────────────────────

def get_sql_failure_log(sql_id: str, space_nm: str | None = None) -> list[dict]:
    """NEXT_SQL_INFO의 LOG 컬럼을 통해 SQL 작업 실패 로그를 조회합니다."""
    if space_nm:
        q = f"""
            SELECT TO_CHAR(SQL_ID) AS SQL_ID, TO_CHAR(SPACE_NM) AS SPACE_NM,
                   TO_CHAR(STATUS) AS STATUS, TO_CHAR(TUNED_TEST) AS TUNED_TEST, LOG
            FROM {SQL_TABLE}
            WHERE TO_CHAR(SQL_ID) = :1
              AND TO_CHAR(SPACE_NM) = :2
        """
        params = (sql_id, space_nm)
    else:
        q = f"""
            SELECT TO_CHAR(SQL_ID) AS SQL_ID, TO_CHAR(SPACE_NM) AS SPACE_NM,
                   TO_CHAR(STATUS) AS STATUS, TO_CHAR(TUNED_TEST) AS TUNED_TEST, LOG
            FROM {SQL_TABLE}
            WHERE TO_CHAR(SQL_ID) = :1
        """
        params = (sql_id,)
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, params)
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_conversion_failure_analysis_rows(limit: int = 200) -> list[dict]:
    """Return recent SQL conversion FAIL rows for supervisor aggregate analysis."""
    available_columns = _get_available_columns(SQL_TABLE)
    map_kind_column = "TO_CHAR(MAP_KIND) AS MAP_KIND" if "MAP_KIND" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_KIND"
    map_type_column = "TO_CHAR(MAP_TYPE) AS MAP_TYPE" if "MAP_TYPE" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_TYPE"
    tag_kind_column = "TO_CHAR(TAG_KIND) AS TAG_KIND" if "TAG_KIND" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS TAG_KIND"
    sql_length_column = "SQL_LENGTH" if "SQL_LENGTH" in available_columns else "CAST(NULL AS NUMBER)"
    edit_len_expr = "DBMS_LOB.GETLENGTH(EDIT_FR_SQL)" if "EDIT_FR_SQL" in available_columns else "0"

    q = f"""
        SELECT *
        FROM (
            SELECT TO_CHAR(SQL_ID) AS SQL_ID,
                   TO_CHAR(SPACE_NM) AS SPACE_NM,
                   {map_kind_column},
                   {tag_kind_column},
                   {map_type_column},
                   {sql_length_column} AS SQL_LENGTH,
                   DBMS_LOB.GETLENGTH(FR_SQL_TEXT) AS FR_SQL_LEN,
                   {edit_len_expr} AS EDIT_FR_SQL_LEN,
                   CASE
                       WHEN NVL({edit_len_expr}, 0) > 0
                       THEN {edit_len_expr}
                       ELSE DBMS_LOB.GETLENGTH(FR_SQL_TEXT)
                   END AS EFFECTIVE_SQL_LEN,
                   TO_CHAR(STATUS) AS STATUS,
                   TO_CHAR(TUNED_TEST) AS TUNED_TEST,
                   LOG,
                   TO_CHAR(UPD_TS, 'YYYY-MM-DD HH24:MI:SS') AS UPD_TS
            FROM {SQL_TABLE}
            WHERE UPPER(TRIM(STATUS)) IN ({_sql_in(CONVERSION_FAIL_STATUSES)})
            ORDER BY UPD_TS DESC NULLS LAST
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


def get_sql_tuning_failure_analysis_rows(limit: int = 200) -> list[dict]:
    """Return recent SQL tuning FAIL rows for supervisor aggregate analysis."""
    available_columns = _get_available_columns(SQL_TABLE)
    map_kind_column = "TO_CHAR(MAP_KIND) AS MAP_KIND" if "MAP_KIND" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_KIND"
    map_type_column = "TO_CHAR(MAP_TYPE) AS MAP_TYPE" if "MAP_TYPE" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS MAP_TYPE"
    tag_kind_column = "TO_CHAR(TAG_KIND) AS TAG_KIND" if "TAG_KIND" in available_columns else "CAST(NULL AS VARCHAR2(4000)) AS TAG_KIND"
    sql_length_column = "SQL_LENGTH" if "SQL_LENGTH" in available_columns else "CAST(NULL AS NUMBER)"
    tuned_len_expr = "DBMS_LOB.GETLENGTH(TUNED_SQL)" if "TUNED_SQL" in available_columns else "0"

    q = f"""
        SELECT *
        FROM (
            SELECT TO_CHAR(SQL_ID) AS SQL_ID,
                   TO_CHAR(SPACE_NM) AS SPACE_NM,
                   {map_kind_column},
                   {tag_kind_column},
                   {map_type_column},
                   {sql_length_column} AS SQL_LENGTH,
                   DBMS_LOB.GETLENGTH(FR_SQL_TEXT) AS FR_SQL_LEN,
                   DBMS_LOB.GETLENGTH(TO_SQL_TEXT) AS TO_SQL_LEN,
                   {tuned_len_expr} AS TUNED_SQL_LEN,
                   TO_CHAR(STATUS) AS STATUS,
                   TO_CHAR(TUNED_TEST) AS TUNED_TEST,
                   LOG,
                   TO_CHAR(UPD_TS, 'YYYY-MM-DD HH24:MI:SS') AS UPD_TS
            FROM {SQL_TABLE}
            WHERE UPPER(TRIM(TUNED_TEST)) IN ({_sql_in(TUNING_FAIL_STATUSES)})
            ORDER BY UPD_TS DESC NULLS LAST
        )
        WHERE ROWNUM <= :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (int(limit),))
            return _to_dicts(cur)
    except Exception:
        return []


# ── 작업 완료 대기 (챗봇 재실행 후 결과 반환용) ──────────────────────────────────

_MIG_RUNNING  = {"", "RUNNING", "URGENT", "READY", "PENDING"}
_SQL_RUNNING  = {"URGENT", "RUNNING", ""}

def poll_mig_job_result(map_id: int, timeout_sec: int = 300, interval: float = 3.0) -> dict:
    """Migration 작업이 완료될 때까지 대기하고 최종 상태와 로그를 반환합니다."""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            jobs = {int(j["MAP_ID"]): j for j in get_mig_jobs()}
            job = jobs.get(map_id, {})
            status = str(job.get("STATUS") or "").strip().upper()
            if status not in _MIG_RUNNING:
                logs = get_mig_logs(map_id)
                return {
                    "completed": True,
                    "map_id":    map_id,
                    "status":    status,
                    "fr_table":  job.get("FR_TABLE"),
                    "to_table":  job.get("TO_TABLE"),
                    "elapsed":   job.get("ELAPSED_SECONDS"),
                    "retry":     job.get("RETRY_COUNT"),
                    "last_logs": [
                        {
                            "step":    lg.get("STEP_NAME"),
                            "level":   lg.get("LOG_LEVEL"),
                            "message": str(lg.get("MESSAGE") or "")[:300],
                        }
                        for lg in logs[-5:]
                    ],
                }
        except Exception:
            pass
        time.sleep(interval)
    return {"completed": False, "map_id": map_id, "reason": f"{timeout_sec}초 내에 완료되지 않았습니다."}


def poll_sql_job_result(
    sql_id: str,
    field: str,
    space_nm: str | None = None,
    timeout_sec: int = 300,
    interval: float = 3.0,
) -> dict:
    """SQL 변환/튜닝 작업이 완료될 때까지 대기하고 최종 상태와 로그를 반환합니다.

    field: 'STATUS' (변환) 또는 'TUNED_TEST' (튜닝)
    """
    deadline = time.time() + timeout_sec
    field_upper = field.upper()
    while time.time() < deadline:
        try:
            rows = get_sql_failure_log(sql_id, space_nm)
            if rows:
                row = rows[0]
                val = str(row.get(field_upper) or "").strip().upper()
                if val not in _SQL_RUNNING:
                    return {
                        "completed":  True,
                        "sql_id":     sql_id,
                        "space_nm":   row.get("SPACE_NM"),
                        "field":      field_upper,
                        "result":     val,
                        "log":        str(row.get("LOG") or "")[:500],
                        "status":     row.get("STATUS"),
                        "tuned_test": row.get("TUNED_TEST"),
                    }
        except Exception:
            pass
        time.sleep(interval)
    return {"completed": False, "sql_id": sql_id, "reason": f"{timeout_sec}초 내에 완료되지 않았습니다."}


def get_sql_job_full(row_id: str) -> dict | None:
    available_columns = _get_available_columns(SQL_TABLE)
    tobe_correct_column = _optional_column_expr("TOBE_CORRECT_SQL", available_columns)
    bind_correct_column = _optional_column_expr("BIND_CORRECT_SQL", available_columns)
    test_correct_column = _optional_column_expr("TEST_CORRECT_SQL", available_columns)
    q = f"""
        SELECT ROWIDTOCHAR(ROWID) AS ROW_ID,
               TAG_KIND, SPACE_NM, SQL_ID,
               FR_SQL_TEXT, EDIT_FR_SQL, TARGET_TABLE,
               TO_SQL_TEXT, TUNED_SQL, TUNED_TEST, TUNED_RESULT,
               BIND_SQL, BIND_SET, TEST_SQL,
               FORMATTED_SQL, BLOCK_RAG_CONTENT,
               {tobe_correct_column}, {bind_correct_column}, {test_correct_column},
               STATUS, LOG, TO_CHAR(UPD_TS) AS UPD_TS, EDITED_YN
        FROM {SQL_TABLE}
        WHERE ROWIDTOCHAR(ROWID) = :1
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (row_id,))
            row = cur.fetchone()
            if row:
                cols = [d[0] for d in cur.description]
                return {cols[i]: _s(row[i]) for i in range(len(cols))}
    except Exception:
        pass
    return None


def update_sql_correct_sql(row_id: str, correct_kind: str, correct_sql: str) -> tuple[bool, str]:
    column_map = {
        "TOBE": "TOBE_CORRECT_SQL",
        "BIND": "BIND_CORRECT_SQL",
        "TEST": "TEST_CORRECT_SQL",
    }
    kind = (correct_kind or "").strip().upper()
    column = column_map.get(kind)
    if not column:
        return False, "지원하지 않는 Correct SQL 유형입니다."

    try:
        available_columns = _get_available_columns(SQL_TABLE)
    except Exception as exc:
        return False, f"컬럼 정보를 조회하지 못했습니다: {exc}"

    if column not in available_columns:
        return False, f"{SQL_TABLE} 테이블에 {column} 컬럼이 없습니다."

    q = f"""
        UPDATE {SQL_TABLE}
        SET {column} = :1,
            UPD_TS = CURRENT_TIMESTAMP
        WHERE ROWIDTOCHAR(ROWID) = :2
    """
    try:
        with get_connection() as conn:
            cur = conn.cursor()
            cur.execute(q, (correct_sql, row_id))
            rowcount = cur.rowcount
            conn.commit()
            if rowcount <= 0:
                return False, "대상 SQL Job을 찾지 못했습니다."
            return True, f"{column} 저장 완료"
    except Exception as exc:
        return False, str(exc)
