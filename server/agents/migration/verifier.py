from server.core.db_migration import get_connection
from server.core.logger import logger
from server.agents.migration.sql_utils import split_sql_script, clean_sql_statement
from decimal import Decimal, InvalidOperation


def _read_lob_value(value):
    if value is not None and hasattr(value, "read"):
        return value.read()
    return value


def _is_zero(value) -> bool:
    value = _read_lob_value(value)
    if value is None:
        return False
    try:
        return Decimal(str(value).strip()) == Decimal("0")
    except (InvalidOperation, ValueError):
        return str(value).strip() == "0"


def execute_verification(sql: str) -> tuple[bool, str]:
    """Execute DB migration verification SQL."""
    if not sql.strip():
        return False, "No verification SQL provided"

    logger.debug(f"[Verifier] Start verification query: {sql[:50]}...")

    try:
        statements = split_sql_script(sql)
        if not statements:
            return False, "No valid SQL statements found"

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
                return False, "Verification SQL returned no rows"

            for row in last_rows:
                for col_val in row:
                    if not _is_zero(col_val):
                        return False, f"Mismatch found (NULL or non-zero DIFF): {row}"

            return True, "All Verification Passed"

    except Exception as e:
        error_message = str(e)
        logger.error(f"[Verifier] Verification query error: {error_message}")
        return False, f"Verification Query Error: {error_message}"
