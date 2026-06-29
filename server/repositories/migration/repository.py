from server.core.logger import logger
from server.services.migration.domain_models import MappingRule, MappingDetail
from server.core.db_migration import (
    get_connection,
    get_mapping_rule_detail_table,
    get_mapping_rule_table,
)


def ensure_str(val):
    if val is not None and hasattr(val, "read"):
        return val.read()
    return val


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
        params = (owner, table_name)
    else:
        query = """
            SELECT COLUMN_NAME
            FROM USER_TAB_COLUMNS
            WHERE TABLE_NAME = :1
        """
        params = (table_name,)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return {str(row[0]).upper() for row in cursor.fetchall()}


_STATUS_PRIORITY = {
    "": 0,
}


def _job_sort_key(job: MappingRule):
    status = (job.status or "").strip().upper()
    status_rank = _STATUS_PRIORITY.get(status, 9)
    priority = job.priority if job.priority is not None else 999999999
    return (priority, status_rank)


def _user_edit_expr(map_table: str) -> str:
    available_columns = _table_columns(map_table)
    if "USER_EDITED" in available_columns:
        return "R.USER_EDITED"
    return "CAST(NULL AS VARCHAR2(1))"


def get_pending_jobs() -> list[MappingRule]:
    """Return the highest-priority pending DB migration job."""
    logger.debug("[Repository] Fetching pending DB migration jobs.")
    jobs = {}
    map_table = get_mapping_rule_table()
    detail_table = get_mapping_rule_detail_table()
    user_edit_expr = _user_edit_expr(map_table)

    query = f"""
        WITH PICKED AS (
            SELECT M.MAP_ID
            FROM {map_table} M
            LEFT JOIN {map_table} P ON P.MAP_ID = M.PRIOR_MAP_ID
            WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
              AND M.STATUS IS NULL
              AND (M.PRIOR_MAP_ID IS NULL OR M.PRIOR_MAP_ID <= 0 OR UPPER(TRIM(NVL(P.STATUS, ''))) = 'PASS')
            ORDER BY
                M.PRIORITY ASC,
                M.MAP_ID ASC
            FETCH FIRST 1 ROW ONLY
        )
        SELECT
            R.MAP_ID, R.MAP_TYPE, R.FR_TABLE, R.TO_TABLE,
            R.USE_YN, R.TRUNC_YN, R.PRIORITY,
            R.MIG_SQL, R.VERIFY_SQL, R.STATUS, {user_edit_expr} AS USER_EDITED,
            R.BATCH_CNT, R.ELAPSED_SECONDS, R.RETRY_COUNT,
            R.CREATED_AT, R.UPD_TS, R.CONDITION, R.PRIOR_MAP_ID,
            D.MAP_DTL, D.FR_COL, D.TO_COL
        FROM PICKED P
        JOIN {map_table} R ON R.MAP_ID = P.MAP_ID
        LEFT JOIN {detail_table} D ON R.MAP_ID = D.MAP_ID
        ORDER BY D.FR_COL ASC
    """

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()

            for row in rows:
                map_id = row[0]
                if map_id not in jobs:
                    jobs[map_id] = MappingRule(
                        map_id=map_id,
                        map_type=ensure_str(row[1]),
                        fr_table=ensure_str(row[2]),
                        to_table=ensure_str(row[3]),
                        use_yn=ensure_str(row[4]),
                        trunc_yn=ensure_str(row[5]),
                        priority=row[6],
                        mig_sql=ensure_str(row[7]),
                        verify_sql=ensure_str(row[8]),
                        status=ensure_str(row[9]),
                        user_edited=ensure_str(row[10]),
                        batch_cnt=row[11] if row[11] is not None else 0,
                        elapsed_seconds=row[12] if row[12] is not None else 0,
                        retry_count=row[13] if row[13] is not None else 0,
                        created_at=row[14],
                        upd_ts=row[15],
                        condition=ensure_str(row[16]),
                        prior_map_id=row[17],
                        details=[],
                    )

                if row[18] is not None:
                    jobs[map_id].details.append(
                        MappingDetail(
                            map_dtl=row[18],
                            map_id=map_id,
                            fr_col=ensure_str(row[19]),
                            to_col=ensure_str(row[20]),
                        )
                    )

    except Exception as e:
        logger.error(f"[Repository] Failed to fetch pending DB migration jobs: {e}")

    return sorted(jobs.values(), key=_job_sort_key)


def increment_batch_count(map_id: int):
    logger.debug(f"[Repository] map_id={map_id} | BATCH_CNT +1")
    map_table = get_mapping_rule_table()
    query = f"UPDATE {map_table} SET BATCH_CNT = COALESCE(BATCH_CNT, 0) + 1, UPD_TS = CURRENT_TIMESTAMP WHERE MAP_ID = :1"
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (map_id,))
            conn.commit()
    except Exception as e:
        logger.error(f"[Repository] Failed to update BATCH_CNT: {e}")


def update_job_status(map_id: int, status: str, elapsed_seconds: int = 0, retry_count: int = 0) -> bool:
    logger.info(f"[Repository] map_id={map_id} | status={status}, retry={retry_count}")

    map_table = get_mapping_rule_table()
    query = f"""
        UPDATE {map_table}
        SET STATUS = :1,
            UPD_TS = CURRENT_TIMESTAMP,
            ELAPSED_SECONDS = :2,
            RETRY_COUNT = :3
        WHERE MAP_ID = :4
    """

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (status, elapsed_seconds, retry_count, map_id))
            rowcount = cursor.rowcount
            conn.commit()
            if rowcount > 0:
                logger.debug(f"[Repository] map_id={map_id} updated (rowcount={rowcount})")
                return True
            logger.warning(f"[Repository] map_id={map_id} update affected no rows.")
            return False
    except Exception as e:
        logger.error(f"[Repository] Failed to update job status map_id={map_id}: {e}")
        return False


def check_dependencies(map_id: int, prior_map_id: int | None) -> str:
    logger.debug(f"[Repository] map_id={map_id} | PRIOR_MAP_ID={prior_map_id}")

    if prior_map_id is None:
        return "READY"

    try:
        prior_map_id = int(prior_map_id)
    except (TypeError, ValueError):
        logger.warning(f"[Repository] map_id={map_id} | invalid PRIOR_MAP_ID={prior_map_id}")
        return "PENDING"

    if prior_map_id <= 0:
        return "READY"

    map_table = get_mapping_rule_table()
    query = f"""
        SELECT STATUS FROM {map_table}
        WHERE MAP_ID = :1
    """

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (prior_map_id,))
            row = cursor.fetchone()

            if not row:
                logger.warning(f"[Repository] map_id={map_id} | prior map_id={prior_map_id} not found.")
                return "PENDING"

            status = (ensure_str(row[0]) or "").strip().upper()
            if status != "PASS":
                logger.warning(f"[Repository] map_id={map_id} | prior map_id={prior_map_id} status={status or 'NULL'}")
                return status if status else "PENDING"

            return "READY"
    except Exception as e:
        logger.error(f"[Repository] Dependency check failed: {e}")
        return "ERROR"


def check_target_priority_dependencies(map_id: int, to_table: str, priority: int) -> str:
    logger.debug(
        f"[Repository] map_id={map_id} | check same-target prior jobs "
        f"(to_table={to_table}, priority<{priority})"
    )

    map_table = get_mapping_rule_table()
    query = f"""
        SELECT STATUS FROM {map_table}
        WHERE DBMS_LOB.SUBSTR(TO_TABLE, 200, 1) = :1
          AND PRIORITY < :2
          AND MAP_ID != :3
        ORDER BY PRIORITY DESC
    """

    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (to_table, priority, map_id))
            rows = cursor.fetchall()

            if not rows:
                return "READY"

            for row in rows:
                status = (ensure_str(row[0]) or "").strip().upper()
                if status != "PASS":
                    logger.warning(
                        f"[Repository] map_id={map_id} | same-target prior job status={status or 'NULL'}"
                    )
                    return status if status else "PENDING"

            return "READY"
    except Exception as e:
        logger.error(f"[Repository] Same-target dependency check failed: {e}")
        return "ERROR"


def is_first_job_for_target(map_id: int, to_table: str, priority: int) -> bool:
    map_table = get_mapping_rule_table()
    query = f"""
        SELECT COUNT(*) FROM {map_table}
        WHERE DBMS_LOB.SUBSTR(TO_TABLE, 200, 1) = :1
          AND PRIORITY < :2
          AND MAP_ID != :3
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(query, (to_table, priority, map_id))
            count = cursor.fetchone()[0]
            return count == 0
    except Exception as e:
        logger.error(f"[Repository] Failed to check first target job: {e}")
        return True
