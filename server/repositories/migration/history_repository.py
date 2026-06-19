from server.core.logger import logger
from server.core.db_migration import get_connection, get_mapping_rule_table, get_migration_log_sequence, get_migration_log_table


def _to_text(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        value = value.read()
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _split_table_owner_and_name(table: str) -> tuple[str | None, str]:
    value = (table or "").strip().upper()
    if "." in value:
        owner, table_name = value.split(".", 1)
        return owner.strip('"'), table_name.strip('"')
    return None, value.strip('"')


def _table_columns(table: str) -> set[str]:
    owner, table_name = _split_table_owner_and_name(table)
    if owner:
        query = """
            SELECT COLUMN_NAME
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :1
              AND TABLE_NAME = :2
        """
        params = [owner, table_name]
    else:
        query = """
            SELECT COLUMN_NAME
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :1
        """
        params = [table_name]

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return {_to_text(row[0]).upper() for row in cursor.fetchall()}


def log_generated_sql(map_id: int, migration_sql: str, verification_sql: str):
    def ensure_string(val):
        if isinstance(val, list):
            return "\n".join(map(str, val))
        return str(val) if val is not None else ""

    safe_mig_sql = ensure_string(migration_sql)
    safe_v_sql = ensure_string(verification_sql)

    logger.info(f"[HistoryRepo] map_id={map_id} | 마이그레이션 SQL(DML/VERIFY) DB 기록 진행")

    map_table = get_mapping_rule_table()
    query = f"""
        UPDATE {map_table}
        SET MIG_SQL = :1, VERIFY_SQL = :2, UPD_TS = CURRENT_TIMESTAMP
        WHERE MAP_ID = :3
    """

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (safe_mig_sql, safe_v_sql, map_id))
            conn.commit()
    except Exception as e:
        logger.error(f"[HistoryRepo] SQL 생성 내역 기록 중 오류: {e}")

def log_business_history(map_id: int, log_type: str, log_level: str, step_name: str, status: str, message: str, retry_count: int = 0, mig_kind: str = "DB_MIG"):
    msg_str = str(message)
    if len(msg_str) > 4000:
        msg_str = msg_str[:3996] + "..."

    logger.info(f"[HistoryRepo] map_id={map_id} | Business Log 저장 -> [{step_name}][{status}] : {msg_str[:50]}")

    log_table = get_migration_log_table()
    log_sequence = get_migration_log_sequence()

    try:
        timestamp_columns = [
            column
            for column in ("CREATED_AT", "UPD_TS")
            if column in _table_columns(log_table)
        ]
        timestamp_column_sql = "".join(f", {column}" for column in timestamp_columns)
        timestamp_value_sql = "".join(", CURRENT_TIMESTAMP" for _ in timestamp_columns)
        query = f"""
            INSERT INTO {log_table} (
                LOG_ID, MAP_ID, MIG_KIND, LOG_TYPE, LOG_LEVEL, STEP_NAME, STATUS, MESSAGE, RETRY_COUNT{timestamp_column_sql}
            ) VALUES ({log_sequence}.NEXTVAL, :1, :2, :3, :4, :5, :6, :7, :8{timestamp_value_sql})
        """
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (map_id, mig_kind, log_type, log_level, step_name, status, msg_str, retry_count))
            conn.commit()
    except Exception as e:
        logger.error(f"[HistoryRepo] 비즈니스 이력 기록 중 오류: {e}")
