"""SQL 튜닝 에이전트.

RAG(FAISS) 기반 튜닝 룰 검색 후 TO-BE SQL을 개선합니다.
Supervisor의 tool로 사용될 수 있는 독립적인 에이전트입니다.
내부 튜닝 로직(sql_pipeline)은 그대로 유지됩니다.
"""

from server.services.sql.agents import SqlTuningAgent as _SqlTuningAgent
from server.core.logger import logger
from server.repositories.sql.mapper_repository import get_all_mapping_rules
from server.repositories.sql.result_repository import update_cycle_result, update_tuning_error
from server.services.sql.statuses import FAIL_TEST, is_fail
from server.services.sql.workflow.state import JobExecutionState


class SqlTuningAgent:
    """SQL 튜닝 에이전트 — Supervisor tool로 사용됩니다.

    변환이 완료된(STATUS=PASS) SQL에 RAG 기반 튜닝 룰을 적용합니다.
    """

    def __init__(self) -> None:
        self._agent = _SqlTuningAgent()

    def process_job(self, job) -> str:
        """SQL 튜닝 작업 1건을 처리합니다."""
        job_key = f"{job.space_nm}.{job.sql_id}"
        state = None
        try:
            state = JobExecutionState(
                job=job,
                job_key=job_key,
                mapping_rules=get_all_mapping_rules(),
                last_error=None,
            )
            state.tobe_sql = job.to_sql_text
            state.bind_set_for_db = job.bind_set

            self._agent.run(state)

            final_status = state.tuned_test if state.tuned_test else (state.failure_status or FAIL_TEST)
            final_log = self._build_final_log(
                state=state,
                final_status=final_status,
                job_key=job_key,
            )
            update_cycle_result(
                row_id=job.row_id,
                tobe_sql=state.tobe_sql,
                tuned_sql=state.tuned_sql if state.tuned_sql else None,
                tuned_result=state.tuned_result if state.tuned_result else None,
                tuned_test=final_status,
                bind_sql=job.bind_sql,
                bind_set=job.bind_set,
                test_sql=job.test_sql,
                status=job.status,
                final_log=final_log,
                formatted_sql=state.formatted_sql if state.formatted_sql else None,
            )
            logger.info(f"[SqlTuningAgent] {job_key} 튜닝 완료 (Status: {final_status})")
            return final_status

        except Exception as exc:
            logger.error(f"[SqlTuningAgent] {job_key} 처리 오류: {exc}")
            update_tuning_error(
                job.row_id,
                str(exc),
                tuned_sql=state.tuned_sql if state and state.tuned_sql else None,
            )
            return state.failure_status if state and state.failure_status else FAIL_TEST

    @staticmethod
    def _get_case_insensitive_value(row: dict, key: str):
        lowered = key.lower()
        for existing_key, value in row.items():
            if str(existing_key).lower() == lowered:
                return value
        return None

    @classmethod
    def _summarize_tuned_test_rows(cls, rows: list[dict]) -> str:
        if not rows:
            return "no_tuned_test_rows"

        samples: list[str] = []
        for row in rows[:5]:
            case_no = cls._get_case_insensitive_value(row, "case_no")
            baseline_count = cls._get_case_insensitive_value(row, "from_count")
            tuned_count = cls._get_case_insensitive_value(row, "to_count")
            samples.append(
                f"CASE_NO={case_no},BASELINE_COUNT={baseline_count},TUNED_COUNT={tuned_count}"
            )
        return " ; ".join(samples)

    @classmethod
    def _build_final_log(
        cls,
        state: JobExecutionState,
        final_status: str,
        job_key: str,
    ) -> str:
        base_log = (
            f"TUNING COMPLETED status={final_status} "
            f"job={job_key} changed={bool(state.tuned_sql)}"
        )
        if is_fail(final_status):
            reason = state.last_error or "TUNED_TEST_VALIDATION_FAIL"
            details = cls._summarize_tuned_test_rows(state.tuned_test_rows)
            return f"{base_log} reason={reason} details={details}"
        return base_log
