"""공통 Oracle DB 연결 모듈.

기존 에이전트 내부 DB 코드는 그대로 유지합니다.
신규 코드(Planner, config 등)는 이 모듈을 사용합니다.
"""
import oracledb
from server.config.settings import (
    DB_USER, DB_PASS, DB_HOST, DB_PORT, DB_SID, ORACLE_CLIENT_PATH
)

oracledb.defaults.fetch_lobs = False

_thick_initialized = False


def get_connection():
    """Oracle DB 커넥션 반환 (Thick/Thin 자동 선택)."""
    global _thick_initialized
    if ORACLE_CLIENT_PATH and not _thick_initialized:
        try:
            oracledb.init_oracle_client(lib_dir=ORACLE_CLIENT_PATH)
        except oracledb.ProgrammingError:
            pass
        _thick_initialized = True

    dsn = DB_HOST if ("/" in DB_HOST or "(" in DB_HOST) else f"{DB_HOST}:{DB_PORT}/{DB_SID}"
    conn = oracledb.connect(user=DB_USER, password=DB_PASS, dsn=dsn)
    with conn.cursor() as cur:
        cur.execute("ALTER SESSION SET NLS_DATE_FORMAT='YYYY-MM-DD HH24:MI:SS'")
    return conn


def to_str(val, default="") -> str:
    if val is None:
        return default
    if hasattr(val, "read"):
        val = val.read()
    if val is None:
        return default
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="ignore")
    return str(val)
