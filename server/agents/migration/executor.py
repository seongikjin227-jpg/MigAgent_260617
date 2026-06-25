import oracledb

from server.agents.migration.sql_utils import clean_sql_statement, split_sql_script
from server.core.db_migration import get_connection
from server.core.exceptions import DBSqlError
from server.core.logger import logger


def truncate_table(table_name: str):
    """Reset target table data before a migration attempt."""
    logger.info(f"[Executor] TRUNCATE TABLE: {table_name}")
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"TRUNCATE TABLE {table_name}")
            conn.commit()
            logger.info(f"[Executor] TRUNCATE completed: {table_name}")
    except Exception as e:
        logger.error(f"[Executor] TRUNCATE failed: {str(e)}")
        raise DBSqlError(f"Oracle TRUNCATE error: {str(e)}")


def execute_migration(sql_script: str):
    """Execute a generated Oracle SQL script."""
    if not sql_script.strip():
        logger.debug("[Executor] SQL script is empty.")
        return

    try:
        with get_connection() as conn:
            cursor = conn.cursor()

            statements = split_sql_script(sql_script)

            for stmt in statements:
                clean_stmt = clean_sql_statement(stmt)
                if not clean_stmt:
                    continue

                is_plsql = clean_stmt.upper().startswith(("BEGIN", "DECLARE"))

                logger.info(
                    f"[Executor] Executing {'PL/SQL' if is_plsql else 'SQL'}: "
                    f"{clean_stmt[:70]}..."
                )

                try:
                    exec_stmt = clean_stmt if not is_plsql else clean_stmt + "\n"
                    cursor.execute(exec_stmt)
                except oracledb.DatabaseError as e:
                    if "ORA-00955" in str(e):
                        logger.warning("[Executor] Object already exists; skipping.")
                        continue
                    raise e

            conn.commit()
            logger.info("[Executor] All commands executed and committed successfully.")

    except Exception as e:
        logger.error(f"[Executor] SQL execution failed: {str(e)}")
        raise DBSqlError(f"Oracle query execution error: {str(e)}")
