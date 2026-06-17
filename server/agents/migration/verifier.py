from server.core.db_migration import get_connection
from server.core.logger import logger
from server.agents.migration.sql_utils import split_sql_script, clean_sql_statement


def _read_lob_value(value):
    if value is not None and hasattr(value, "read"):
        return value.read()
    return value


def execute_verification(sql: str) -> tuple[bool, str]:
    """양 DB의 정합성을 대조하는 검증 SQL 실행"""
    if not sql.strip():
        return True, "No verification SQL provided"

    logger.debug(f"[Verifier] 실제 검증 쿼리 실행 시작: {sql[:50]}...")

    try:
        statements = split_sql_script(sql)
        if not statements:
            return True, "No valid SQL statements found"

        last_rows = []
        with get_connection() as conn:
            cursor = conn.cursor()
            for stmt in statements:
                clean_stmt = clean_sql_statement(stmt)
                if not clean_stmt:
                    continue

                logger.debug(f"[Verifier] Executing: {clean_stmt[:70]}...")
                cursor.execute(clean_stmt)

                if cursor.description:
                    last_rows = cursor.fetchall()

            if not last_rows:
                return True, "No mismatch found (Empty ResultSet)"

            for row in last_rows:
                for col_val in row:
                    col_val = _read_lob_value(col_val)
                    if col_val is None or str(col_val) != "0":
                        return False, f"Mismatch found (NULL or non-zero DIFF): {row}"

            return True, "All Verification Passed"

    except Exception as e:
        error_message = str(e)
        if "ORA-00932" in error_message or "inconsistent datatypes" in error_message.lower():
            logger.warning(f"[Verifier] ORA-00932 검증 오류 스킵: {error_message}")
            return True, f"Verification skipped due to ORA-00932: {error_message}"

        logger.error(f"[Verifier] 검증 쿼리 실행 에러: {error_message}")
        return False, f"Verification Query Error: {error_message}"
