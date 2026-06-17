from server.core.logger import logger
from server.services.migration.domain_models import MappingRule, MappingDetail
from server.core.db_migration import (
    get_connection,
    get_mapping_rule_detail_table,
    get_mapping_rule_table,
)

def ensure_str(val):
    """LOB 객체인 경우 문자열로 읽어 반환합니다."""
    if val is not None and hasattr(val, 'read'):
        return val.read()
    return val

_STATUS_PRIORITY = {
    "URGENT": 0,
    "READY": 1,
    "FAIL": 2,
    "PENDING": 3,
    "": 4,
}

def _job_sort_key(job: MappingRule):
    status = (job.status or "").strip().upper()
    status_rank = _STATUS_PRIORITY.get(status, 9)
    priority = job.priority if job.priority is not None else 999999999
    return (priority, status_rank)

def get_pending_jobs() -> list[MappingRule]:
    """PRIOR_MAP_ID 선행 조건을 만족한 최우선 DB Migration 작업 1건을 가져옵니다."""
    logger.debug("[Repository] DB에서 작업 대상을 스캔합니다...")
    jobs = {}
    map_table = get_mapping_rule_table()
    detail_table = get_mapping_rule_detail_table()

    query = f"""
        WITH PICKED AS (
            SELECT M.MAP_ID
            FROM {map_table} M
            LEFT JOIN {map_table} P ON P.MAP_ID = M.PRIOR_MAP_ID
            WHERE UPPER(TRIM(NVL(M.USE_YN, 'N'))) = 'Y'
              AND UPPER(TRIM(NVL(M.TARGET_YN, 'N'))) = 'Y'
              AND UPPER(TRIM(NVL(M.STATUS, 'PENDING'))) NOT IN ('PASS', 'NA')
              AND (M.PRIOR_MAP_ID IS NULL OR M.PRIOR_MAP_ID <= 0 OR UPPER(TRIM(NVL(P.STATUS, ''))) = 'PASS')
            ORDER BY
                M.PRIORITY ASC,
                CASE UPPER(TRIM(NVL(M.STATUS, 'PENDING')))
                    WHEN 'URGENT' THEN 1
                    WHEN 'READY' THEN 2
                    WHEN 'FAIL' THEN 3
                    WHEN 'SKIP' THEN 4
                    WHEN 'PENDING' THEN 5
                    ELSE 6
                END ASC,
                M.MAP_ID ASC
            FETCH FIRST 1 ROW ONLY
        )
        SELECT
            R.MAP_ID, R.MAP_TYPE, R.FR_TABLE, R.TO_TABLE,
            R.USE_YN, R.TARGET_YN, R.PRIORITY,
            R.MIG_SQL, R.VERIFY_SQL, R.STATUS, R.CORRECT_SQL, R.USER_EDITED,
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
                    rule = MappingRule(
                        map_id=map_id,
                        map_type=ensure_str(row[1]),
                        fr_table=ensure_str(row[2]),
                        to_table=ensure_str(row[3]),
                        use_yn=ensure_str(row[4]),
                        target_yn=ensure_str(row[5]),
                        priority=row[6],
                        prior_map_id=row[18],
                        mig_sql=ensure_str(row[7]),
                        verify_sql=ensure_str(row[8]),
                        status=ensure_str(row[9]),
                        correct_sql=ensure_str(row[10]),
                        user_edited=ensure_str(row[11]),
                        batch_cnt=row[12] if row[12] is not None else 0,
                        elapsed_seconds=row[13] if row[13] is not None else 0,
                        retry_count=row[14] if row[14] is not None else 0,
                        created_at=row[15],
                        upd_ts=row[16],
                        condition=ensure_str(row[17]),
                        details=[]
                    )
                    jobs[map_id] = rule

                if row[19] is not None:
                    detail = MappingDetail(
                        map_dtl=row[19],
                        map_id=map_id,
                        fr_col=ensure_str(row[20]),
                        to_col=ensure_str(row[21])
                    )
                    jobs[map_id].details.append(detail)

    except Exception as e:
        logger.error(f"[Repository] 작업 대상을 조회하는 중 오류 발생: {e}")

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
        logger.error(f"[Repository] BATCH_COUNT 업데이트 중 오류: {e}")

def update_job_status(map_id: int, status: str, elapsed_seconds: int = 0, retry_count: int = 0) -> bool:
    logger.info(f"[Repository] map_id={map_id} | DB 상태를 {status} 로 업데이트 (Retry: {retry_count})")

    map_table = get_mapping_rule_table()
    query = f"""
        UPDATE {map_table}
        SET STATUS = :1,
            USE_YN = 'N',
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
                logger.debug(f"[Repository] map_id={map_id} | 업데이트 성공 (rowcount={rowcount})")
                return True
            else:
                logger.warning(f"[Repository] map_id={map_id} | 업데이트된 행이 없습니다.")
                return False
    except Exception as e:
        logger.error(f"[Repository] 작업 상태 업데이트 중 오류 발생 map_id={map_id}: {e}")
        return False

def check_dependencies(map_id: int, prior_map_id: int | None) -> str:
    logger.debug(f"[Repository] map_id={map_id} | PRIOR_MAP_ID={prior_map_id} 의존성 체크 시작")

    if prior_map_id is None:
        return "READY"

    try:
        prior_map_id = int(prior_map_id)
    except (TypeError, ValueError):
        logger.warning(f"[Repository] map_id={map_id} | PRIOR_MAP_ID 값이 유효하지 않습니다: {prior_map_id}")
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
                logger.warning(f"[Repository] map_id={map_id} | 선행 MAP_ID={prior_map_id}를 찾을 수 없습니다.")
                return "PENDING"

            status = (ensure_str(row[0]) or "").strip().upper()
            if status != "PASS":
                logger.warning(f"[Repository] map_id={map_id} | 선행 MAP_ID={prior_map_id} 상태가 {status or 'NULL'} 입니다.")
                return status if status else "PENDING"

            return "READY"
    except Exception as e:
        logger.error(f"[Repository] 의존성 체크 중 오류: {e}")
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
        logger.error(f"[Repository] same-target dependency check failed: {e}")
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
        logger.error(f"[Repository] 최초 작업 여부 확인 중 오류: {e}")
        return True
