"""Create NEXT_SQL_LOG append-only history table.

Run:
  python scripts/create_sql_log_table.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from scripts._bootstrap import ROOT_DIR

load_dotenv(ROOT_DIR / ".env")

from server.services.sql.db_runtime import get_connection, get_oracle_schema, qualify_table_name


DDL = """
CREATE TABLE {table_name} (
    LOG_ID          NUMBER GENERATED ALWAYS AS IDENTITY,
    CREATED_AT      TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL,
    SPACE_NM        VARCHAR2(200),
    SQL_ID          VARCHAR2(200),
    SQL_INFO_ROWID  VARCHAR2(30),
    SQL_KIND        VARCHAR2(30) NOT NULL,
    SQL_CONTENT     CLOB,
    STATUS          VARCHAR2(20) NOT NULL,
    PROMPT_NAME     VARCHAR2(120),
    MODEL_NAME      VARCHAR2(120),
    BATCH_NO        NUMBER,
    CYCLE_NO        NUMBER,
    ELAPSED_SECONDS NUMBER(12, 3),
    ATTEMPT_NO      NUMBER,
    STAGE_NAME      VARCHAR2(100),
    ERROR_MESSAGE   CLOB,
    CONSTRAINT PK_NEXT_SQL_LOG PRIMARY KEY (LOG_ID)
)
"""

COLUMNS = {
    "CREATED_AT": "TIMESTAMP DEFAULT SYSTIMESTAMP NOT NULL",
    "SPACE_NM": "VARCHAR2(200)",
    "SQL_ID": "VARCHAR2(200)",
    "SQL_INFO_ROWID": "VARCHAR2(30)",
    "SQL_KIND": "VARCHAR2(30)",
    "SQL_CONTENT": "CLOB",
    "STATUS": "VARCHAR2(20)",
    "PROMPT_NAME": "VARCHAR2(120)",
    "MODEL_NAME": "VARCHAR2(120)",
    "BATCH_NO": "NUMBER",
    "CYCLE_NO": "NUMBER",
    "ELAPSED_SECONDS": "NUMBER(12, 3)",
    "ATTEMPT_NO": "NUMBER",
    "STAGE_NAME": "VARCHAR2(100)",
    "ERROR_MESSAGE": "CLOB",
}

INDEXES = {
    "IX_NEXT_SQL_LOG_JOB": "CREATE INDEX {index_name} ON {table_name} (SPACE_NM, SQL_ID, CREATED_AT)",
    "IX_NEXT_SQL_LOG_KIND": "CREATE INDEX {index_name} ON {table_name} (SQL_KIND, STATUS, CREATED_AT)",
    "IX_NEXT_SQL_LOG_CYCLE": "CREATE INDEX {index_name} ON {table_name} (BATCH_NO, CYCLE_NO)",
}


def table_exists(cur, table_name: str) -> bool:
    owner = get_oracle_schema()
    if owner:
        cur.execute(
            "SELECT COUNT(*) FROM ALL_TABLES WHERE OWNER = :1 AND TABLE_NAME = :2",
            (owner, table_name.upper()),
        )
    else:
        cur.execute("SELECT COUNT(*) FROM USER_TABLES WHERE TABLE_NAME = :1", (table_name.upper(),))
    return cur.fetchone()[0] > 0


def column_exists(cur, table_name: str, column_name: str) -> bool:
    owner = get_oracle_schema()
    if owner:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM ALL_TAB_COLUMNS
            WHERE OWNER = :1 AND TABLE_NAME = :2 AND COLUMN_NAME = :3
            """,
            (owner, table_name.upper(), column_name.upper()),
        )
    else:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :1 AND COLUMN_NAME = :2
            """,
            (table_name.upper(), column_name.upper()),
        )
    return cur.fetchone()[0] > 0


def index_exists(cur, index_name: str) -> bool:
    owner = get_oracle_schema()
    if owner:
        cur.execute(
            "SELECT COUNT(*) FROM ALL_INDEXES WHERE OWNER = :1 AND INDEX_NAME = :2",
            (owner, index_name.upper()),
        )
    else:
        cur.execute("SELECT COUNT(*) FROM USER_INDEXES WHERE INDEX_NAME = :1", (index_name.upper(),))
    return cur.fetchone()[0] > 0


def main() -> None:
    table_name = qualify_table_name("NEXT_SQL_LOG")
    schema = get_oracle_schema()
    with get_connection() as conn:
        cur = conn.cursor()
        if table_exists(cur, "NEXT_SQL_LOG"):
            print("NEXT_SQL_LOG table already exists. Checking columns/indexes.")
            for column_name, ddl in COLUMNS.items():
                if not column_exists(cur, "NEXT_SQL_LOG", column_name):
                    cur.execute(f"ALTER TABLE {table_name} ADD ({column_name} {ddl})")
                    print(f"  added column {column_name}")
        else:
            cur.execute(DDL.format(table_name=table_name))
            print("NEXT_SQL_LOG table created.")

        for index_name, ddl_template in INDEXES.items():
            if not index_exists(cur, index_name):
                qualified_index_name = f"{schema}.{index_name}" if schema else index_name
                cur.execute(ddl_template.format(index_name=qualified_index_name, table_name=table_name))
                print(f"  created index {index_name}")

        conn.commit()
        print("Done.")


if __name__ == "__main__":
    main()
