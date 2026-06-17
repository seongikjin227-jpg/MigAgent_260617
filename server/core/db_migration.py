import oracledb
import os
import re
from pathlib import Path
from server.core.logger import logger
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=True)

oracledb.defaults.fetch_lobs = False

DB_USER = os.getenv("DB_USER") or "scott"
DB_PASS = os.getenv("DB_PASS") or "tiger"
DB_HOST = os.getenv("DB_HOST") or "localhost"
DB_PORT = os.getenv("DB_PORT") or "1521"
DB_SID = os.getenv("DB_SID") or "xe"

ORACLE_CLIENT_PATH = os.getenv("ORACLE_CLIENT_PATH")


def _safe_schema_identifier(schema: str, env_name: str) -> str:
    clean = (schema or "").strip().upper()
    if not clean:
        return ""
    if not re.fullmatch(r"[A-Z][A-Z0-9_$#]*", clean):
        raise ValueError(f"Invalid {env_name} value: {schema}")
    return clean


def _schema_env(env_name: str) -> str:
    return _safe_schema_identifier(os.getenv(env_name) or "", env_name)


def qualify_system_table(table_name: str) -> str:
    schema = _schema_env("ORACLE_SCHEMA")
    clean_table = (table_name or "").strip()
    if not schema or not clean_table or "." in clean_table:
        return clean_table
    return f"{schema}.{clean_table}"


def qualify_fr_table(table_name: str) -> str:
    schema = _schema_env("ORACLE_SCHEMA_SRC")
    clean_table = (table_name or "").strip()
    if not schema or not clean_table or "." in clean_table:
        return clean_table
    return f"{schema}.{clean_table}"


def qualify_to_table(table_name: str) -> str:
    schema = _schema_env("ORACLE_SCHEMA_TGT")
    clean_table = (table_name or "").strip()
    if not schema or not clean_table or "." in clean_table:
        return clean_table
    return f"{schema}.{clean_table}"


def get_mapping_rule_table() -> str:
    return qualify_system_table("NEXT_MIG_INFO")


def get_mapping_rule_detail_table() -> str:
    return qualify_system_table("NEXT_MIG_INFO_DTL")


def get_migration_log_table() -> str:
    return qualify_system_table("NEXT_MIG_LOG")


def get_migration_log_sequence() -> str:
    return qualify_system_table("MIGRATION_LOG_SEQ")


def fetch_table_ddl(table_name: str) -> list:
    """소스 테이블의 컬럼 메타데이터를 읽기 전용으로 조회합니다."""
    table_name = table_name.strip()
    if "." in table_name:
        owner, tbl = table_name.upper().split(".", 1)
    else:
        owner, tbl = None, table_name.upper()

    try:
        with get_connection() as conn:
            with conn.cursor() as cursor:
                if owner:
                    cursor.execute(
                        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE "
                        "FROM ALL_TAB_COLUMNS "
                        "WHERE OWNER = :1 AND TABLE_NAME = :2 "
                        "ORDER BY COLUMN_ID",
                        [owner, tbl]
                    )
                else:
                    cursor.execute(
                        "SELECT COLUMN_NAME, DATA_TYPE, DATA_LENGTH, DATA_PRECISION, DATA_SCALE, NULLABLE "
                        "FROM USER_TAB_COLUMNS "
                        "WHERE TABLE_NAME = :1 "
                        "ORDER BY COLUMN_ID",
                        [tbl]
                    )
                return cursor.fetchall()
    except Exception as e:
        logger.warning(f"[DB] 소스 테이블 DDL 조회 실패 ({table_name}): {e}")
        return []


def get_connection():
    """Oracle DB에 접속하여 Connection 객체를 반환합니다 (Thin/Thick 동적 전환)."""
    try:
        if ORACLE_CLIENT_PATH and os.path.exists(ORACLE_CLIENT_PATH):
            try:
                oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_PATH)
                logger.debug(f"[DB] Oracle Thick Mode 활성화 (Path: {ORACLE_CLIENT_PATH})")
            except oracledb.ProgrammingError:
                pass
            mode_str = "Thick Mode"
        else:
            logger.debug("[DB] Oracle Thin Mode 접속 시도 (No Client Path set)")
            mode_str = "Thin Mode"

        if "/" in DB_HOST or "(" in DB_HOST:
            dsn = DB_HOST
        else:
            dsn = f"{DB_HOST}:{DB_PORT}/{DB_SID}"

        connection = oracledb.connect(
            user=DB_USER,
            password=DB_PASS,
            dsn=dsn
        )

        with connection.cursor() as cursor:
            cursor.execute("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS'")
            cursor.execute("ALTER SESSION SET NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF'")

        return connection
    except Exception as e:
        logger.error(f"[DB] Oracle 접속 중 에러 발생 ({mode_str}, USER: {DB_USER}): {e}")
        raise e
