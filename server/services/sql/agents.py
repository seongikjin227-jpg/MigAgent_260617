"""SQL conversion and tuning agent coordinator."""

import os
import random
import re
import time

from server.config import settings
from server.core.exceptions import LLMRateLimitError
from server.core.logger import logger
from server.repositories.sql.mapper_repository import (
    get_all_mapping_rules,
    get_sql_map_type,
    get_unready_target_tables,
)
from server.repositories.sql.result_repository import (
    classify_sql_length,
    reset_tuning_state,
    update_block_rag_content,
    update_job_classification,
    update_cycle_result,
    update_fr_bindtuned_sql,
    update_job_skip,
)
from server.repositories.sql.log_repository import insert_sql_log
from server.services.sql.binding_service import bind_sets_to_json, build_bind_sets, extract_bind_param_names
from server.services.sql.llm_service import (
    generate_bind_sql,
    generate_bind_tuned_sql,
    generate_formatted_sql,
    generate_sql_comparison_test_sql,
    generate_test_sql,
    generate_tobe_sql,
    serialize_tuning_examples_for_log,
    tune_tobe_sql,
)
from server.services.sql.tobe_sql_tuning_service import tobe_sql_tuning_service
from server.services.sql.validation_service import (
    evaluate_status_from_test_rows,
    execute_binding_query,
    execute_test_query,
)
from server.services.sql.statuses import (
    CONVERSION_PASS,
    FAIL_BIND,
    FAIL_TEST,
    FAIL_TUNED,
    FAIL_TOBE,
    PASS_NON_SELECT,
    TUNING_PASS,
    is_fail,
)
from server.services.sql.workflow.graph import build_migration_workflow
from server.services.sql.workflow.state import JobExecutionState


def _attempt_no(last_error: str | None) -> int | None:
    match = re.search(r"\battempt\s*=\s*(\d+)\s*/\s*\d+", last_error or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


class MappingRuleProvider:
    """Loads mapping rules shared by SQL conversion jobs."""

    def get_rules(self) -> list:
        return get_all_mapping_rules()


class TobeSqlGenerationAgent:
    """Generates baseline TO-BE SQL and validates it.

    Responsibilities:
    - Generate TO-BE SQL from the original SQL, mapping rules, and retry error.
    - Build bind sets when bind parameters exist.
    - Generate and execute validation Test SQL.
    """

    name = "tobe_sql_generation_agent"

    def run(self, state: JobExecutionState) -> None:
        self.generate(state)
        self.validate(state)

    def generate(self, state: JobExecutionState) -> None:
        state.failure_status = FAIL_TOBE
        correct_sql = self._correct_sql(getattr(state.job, "tobe_correct_sql", None))
        if correct_sql:
            state.tobe_sql = correct_sql
            self._log_sql_execution(
                state=state,
                sql_kind="TOBE_SQL",
                sql_content=state.tobe_sql,
                status="SUCCESS",
                stage_name="USE_TOBE_CORRECT_SQL",
                elapsed_seconds=0,
            )
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=USE_TOBE_CORRECT_SQL "
                f"completed (sql_length={len(state.tobe_sql)})"
            )
            return

        state.tobe_sql = generate_tobe_sql(
            job=state.job,
            mapping_rules=state.mapping_rules,
            last_error=state.last_error,
        )
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=GENERATE_TOBE_SQL "
            f"completed (sql_length={len(state.tobe_sql)})"
        )

    def validate(self, state: JobExecutionState) -> None:
        state.failure_status = FAIL_BIND
        correct_bind_sql = self._correct_sql(getattr(state.job, "bind_correct_sql", None))
        bind_param_names = (
            extract_bind_param_names(state.job.source_sql)
            or extract_bind_param_names(state.tobe_sql)
            or extract_bind_param_names(correct_bind_sql)
        )
        state.bind_param_names = bind_param_names
        if not bind_param_names and not correct_bind_sql:
            state.bind_sql = ""
            state.bind_set_for_db = None
            state.bind_set_json_for_test = "[{}]"
            logger.info(f"[{self.name}] ({state.job_key}) stage=SKIP_BIND completed (reason=no_bind_params)")
        else:
            bind_final_retry_mode = bool(state.last_error and "FINAL_RETRY_MODE=ON" in state.last_error.upper())
            if bind_final_retry_mode:
                logger.warning(
                    f"[{self.name}] ({state.job_key}) stage=FINAL_RETRY_MODE "
                    "enabled (template=bind_sql_final_retry_prompt.json)"
                )

            bind_source_sql = self._prepare_bind_source_sql(state)
            if correct_bind_sql:
                state.bind_sql = correct_bind_sql
                self._log_sql_execution(
                    state=state,
                    sql_kind="BIND_SQL",
                    sql_content=state.bind_sql,
                    status="SUCCESS",
                    stage_name="USE_BIND_CORRECT_SQL",
                    elapsed_seconds=0,
                )
                logger.info(
                    f"[{self.name}] ({state.job_key}) stage=USE_BIND_CORRECT_SQL "
                    f"completed (sql_length={len(state.bind_sql)})"
                )
            else:
                state.bind_sql = generate_bind_sql(
                    job=state.job,
                    last_error=state.last_error,
                    bind_source_sql=bind_source_sql,
                )
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=GENERATE_BIND_SQL "
                f"completed (sql_length={len(state.bind_sql)}, final_retry_mode={'ON' if bind_final_retry_mode else 'OFF'})"
            )

            started = time.perf_counter()
            try:
                bind_query_rows = execute_binding_query(state.bind_sql, max_rows=50)
                self._log_sql_execution(
                    state=state,
                    sql_kind="BIND_SQL",
                    sql_content=state.bind_sql,
                    status="SUCCESS",
                    stage_name="EXECUTE_BIND_SQL",
                    elapsed_seconds=time.perf_counter() - started,
                )
            except Exception as exc:
                self._log_sql_execution(
                    state=state,
                    sql_kind="BIND_SQL",
                    sql_content=state.bind_sql,
                    status="FAIL",
                    stage_name="EXECUTE_BIND_SQL",
                    elapsed_seconds=time.perf_counter() - started,
                    error_message=str(exc),
                )
                raise
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=EXECUTE_BIND_SQL "
                f"completed (rows={len(bind_query_rows)})"
            )

            bind_sets = build_bind_sets(
                bind_query_rows=bind_query_rows,
                max_cases=3,
            )
            state.bind_set_json_for_test = bind_sets_to_json(bind_sets)
            state.bind_set_for_db = state.bind_set_json_for_test
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=BUILD_BIND_SET "
                f"completed (cases={len(bind_sets)})"
            )

        final_retry_mode = bool(state.last_error and "FINAL_RETRY_MODE=ON" in state.last_error.upper())
        state.failure_status = FAIL_TEST
        if final_retry_mode:
            logger.warning(
                f"[{self.name}] ({state.job_key}) stage=FINAL_RETRY_MODE "
                "enabled (template=test_sql_final_retry_prompt.json)"
            )

        correct_test_sql = self._correct_sql(getattr(state.job, "test_correct_sql", None))
        if correct_test_sql:
            state.test_sql = correct_test_sql
            self._log_sql_execution(
                state=state,
                sql_kind="TEST_SQL",
                sql_content=state.test_sql,
                status="SUCCESS",
                stage_name="USE_TEST_CORRECT_SQL",
                elapsed_seconds=0,
            )
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=USE_TEST_CORRECT_SQL "
                f"completed (sql_length={len(state.test_sql)})"
            )
        else:
            state.test_sql = generate_test_sql(
                job=state.job,
                tobe_sql=state.tobe_sql,
                bind_set_json=state.bind_set_json_for_test,
                last_error=state.last_error,
            )
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=GENERATE_TEST_SQL "
            f"completed (sql_length={len(state.test_sql)}, final_retry_mode={'ON' if final_retry_mode else 'OFF'})"
        )

        started = time.perf_counter()
        try:
            state.test_rows = execute_test_query(state.test_sql)
        except Exception as exc:
            self._log_sql_execution(
                state=state,
                sql_kind="TEST_SQL",
                sql_content=state.test_sql,
                status="FAIL",
                stage_name="EXECUTE_TEST_SQL",
                elapsed_seconds=time.perf_counter() - started,
                error_message=str(exc),
            )
            raise
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=EXECUTE_TEST_SQL "
            f"completed (rows={len(state.test_rows)})"
        )

        state.status = evaluate_status_from_test_rows(state.test_rows)
        self._log_sql_execution(
            state=state,
            sql_kind="TEST_SQL",
            sql_content=state.test_sql,
            status=state.status,
            stage_name="EXECUTE_TEST_SQL",
            elapsed_seconds=time.perf_counter() - started,
        )
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=EVALUATE_STATUS "
            f"completed (status={state.status})"
        )


    @staticmethod
    def _correct_sql(value: str | None) -> str:
        return (value or "").strip()


    def _prepare_bind_source_sql(self, state: JobExecutionState) -> str:
        original_sql = state.job.source_sql or ""
        sql_length = (getattr(state.job, "sql_length", "") or "").strip().upper()
        min_length = max(0, settings.BIND_SQL_PRETUNING_MIN_LENGTH)
        should_pretune = sql_length == "LONG" or len(original_sql) >= min_length
        if not settings.BIND_SQL_PRETUNING_ENABLED or not should_pretune:
            return original_sql

        tuned_sql = generate_bind_tuned_sql(
            job=state.job,
            last_error=state.last_error,
        )
        update_fr_bindtuned_sql(row_id=state.job.row_id, fr_bindtuned_sql=tuned_sql)
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=BIND_TUNING applied "
            f"(sql_length={sql_length or 'UNKNOWN'}, original_len={len(original_sql)}, "
            f"min_length={min_length}, tuned_len={len(tuned_sql)})"
        )
        return tuned_sql

    @staticmethod
    def _log_sql_execution(
        *,
        state: JobExecutionState,
        sql_kind: str,
        sql_content: str | None,
        status: str,
        stage_name: str,
        elapsed_seconds: float,
        error_message: str | None = None,
    ) -> None:
        insert_sql_log(
            space_nm=state.job.space_nm,
            sql_id=state.job.sql_id,
            sql_info_rowid=state.job.row_id,
            sql_kind=sql_kind,
            sql_content=sql_content,
            status=status,
            model_name=os.getenv("LLM_MODEL", "").strip(),
            elapsed_seconds=elapsed_seconds,
            attempt_no=_attempt_no(state.last_error),
            stage_name=stage_name,
            error_message=error_message,
        )


class SqlTuningAgent:
    """Applies tuning rules to TO-BE SQL after baseline validation.

    Responsibilities:
    - Retrieve top tuning examples with RAG/FAISS.
    - Skip tuning when TOBE_SQL_TUNING_MAX_ITERATIONS is 0.
    """

    name = "sql_tuning_agent"

    def __init__(self, max_iterations: int | None = None) -> None:
        raw_max = max_iterations if max_iterations is not None else int(os.getenv("TOBE_SQL_TUNING_MAX_ITERATIONS", "1"))
        self.max_iterations = max(0, raw_max)

    def run(self, state: JobExecutionState) -> None:
        state.tuned_sql = ""
        state.tuned_test = None
        if self.max_iterations <= 0:
            return
        tag_kind = (state.job.tag_kind or "").strip().upper()
        max_tuning_attempts = 2 if tag_kind == "SELECT" else 1

        for tuning_attempt in range(1, max_tuning_attempts + 1):
            self._apply_tuning_rules(state)

            if self._is_no_tuning_result(state.tuned_result):
                state.tuned_test = TUNING_PASS
                logger.info(
                    f"[{self.name}] ({state.job_key}) stage=PASS_TUNED_TEST_FOR_NO_TUNING "
                    "completed (reason=no_tuning)"
                )
                break

            if tag_kind != "SELECT":
                state.tuned_test = PASS_NON_SELECT
                logger.info(
                    f"[{self.name}] ({state.job_key}) stage=PASS_TUNED_TEST_FOR_NON_SELECT "
                    f"completed (tag_kind={tag_kind or 'UNKNOWN'})"
                )
                break

            try:
                self._run_tuned_sql_validation(state)
            except Exception as exc:
                if tuning_attempt >= max_tuning_attempts:
                    raise
                state.last_error = f"TUNED_TEST_SQL_ERROR: {exc}"
                logger.warning(
                    f"[{self.name}] ({state.job_key}) stage=TUNING_RETRY_CONTEXT "
                    f"attempt={tuning_attempt + 1}/{max_tuning_attempts} last_error={state.last_error}"
                )
                continue

            if state.tuned_test == TUNING_PASS or tuning_attempt >= max_tuning_attempts:
                break

            state.last_error = "TUNED_TEST_VALIDATION_FAIL: " + self._summarize_test_rows_for_retry(state.tuned_test_rows)
            logger.warning(
                f"[{self.name}] ({state.job_key}) stage=TUNING_RETRY_CONTEXT "
                f"attempt={tuning_attempt + 1}/{max_tuning_attempts} last_error={state.last_error}"
            )

        if state.tuned_test in (TUNING_PASS, PASS_NON_SELECT):
            state.formatted_sql = generate_formatted_sql(
                job=state.job,
                input_sql=state.tuned_sql or state.tobe_sql,
            )
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=GENERATE_FORMATTED_SQL "
                f"completed (sql_length={len(state.formatted_sql)})"
            )

    def _apply_tuning_rules(self, state: JobExecutionState) -> None:
        state.failure_status = FAIL_TUNED
        current_sql = state.tobe_sql or ""
        state.tuned_sql = current_sql
        state.tuned_result = ""
        state.tuned_test = None
        state.tuned_test_rows = []
        state.tuning_examples = []
        source_tables = tobe_sql_tuning_service.parse_source_tables(getattr(state.job, "target_table", None))

        for iteration in range(1, self.max_iterations + 1):
            tuning_examples = tobe_sql_tuning_service.retrieve_tuning_examples(
                current_sql,
                source_tables=source_tables,
            )
            state.tuning_examples = tuning_examples
            update_block_rag_content(
                row_id=state.job.row_id,
                block_rag_content=serialize_tuning_examples_for_log(tuning_examples),
            )
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=LOAD_TUNING_RULES "
                f"completed (iteration={iteration}, rule_blocks={len(tuning_examples)})"
            )
            if not tuning_examples:
                state.tuned_result = "NO TUNING"
                break

            tuned_sql, tuned_result = tune_tobe_sql(
                current_tobe_sql=current_sql,
                tuning_examples=tuning_examples,
                last_error=state.last_error,
                job=state.job,
            )
            state.tuned_result = "NO TUNING" if tuned_sql.strip() == current_sql.strip() else tuned_result
            logger.info(
                f"[{self.name}] ({state.job_key}) stage=APPLY_TUNING_RULES "
                f"completed (iteration={iteration}, sql_length={len(tuned_sql)})"
            )
            if tuned_sql.strip() == current_sql.strip():
                break
            current_sql = tuned_sql

        state.tuned_sql = current_sql

    @staticmethod
    def _is_no_tuning_result(tuned_result: str | None) -> bool:
        return "NO TUNING" in (tuned_result or "").upper()

    def _run_tuned_sql_validation(self, state: JobExecutionState) -> None:
        state.failure_status = FAIL_TEST
        comparison_test_sql = generate_sql_comparison_test_sql(
            baseline_sql=state.tobe_sql,
            candidate_sql=state.tuned_sql,
            bind_set_json=state.bind_set_for_db,
            last_error=state.last_error,
            job=state.job,
        )
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=GENERATE_TUNED_TEST_SQL "
            f"completed (sql_length={len(comparison_test_sql)})"
        )

        started = time.perf_counter()
        try:
            comparison_rows = execute_test_query(comparison_test_sql)
        except Exception as exc:
            insert_sql_log(
                space_nm=state.job.space_nm,
                sql_id=state.job.sql_id,
                sql_info_rowid=state.job.row_id,
                sql_kind="TUNED_TEST_SQL",
                sql_content=comparison_test_sql,
                status="FAIL",
                model_name=os.getenv("LLM_MODEL", "").strip(),
                elapsed_seconds=time.perf_counter() - started,
                attempt_no=_attempt_no(state.last_error),
                stage_name="EXECUTE_TUNED_TEST_SQL",
                error_message=str(exc),
            )
            raise
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=EXECUTE_TUNED_TEST_SQL "
            f"completed (rows={len(comparison_rows)})"
        )

        state.tuned_test_rows = comparison_rows
        evaluated_status = evaluate_status_from_test_rows(comparison_rows)
        state.tuned_test = TUNING_PASS if evaluated_status == "PASS" else FAIL_TEST
        insert_sql_log(
            space_nm=state.job.space_nm,
            sql_id=state.job.sql_id,
            sql_info_rowid=state.job.row_id,
            sql_kind="TUNED_TEST_SQL",
            sql_content=comparison_test_sql,
            status=state.tuned_test,
            model_name=os.getenv("LLM_MODEL", "").strip(),
            elapsed_seconds=time.perf_counter() - started,
            attempt_no=_attempt_no(state.last_error),
            stage_name="EXECUTE_TUNED_TEST_SQL",
        )
        logger.info(
            f"[{self.name}] ({state.job_key}) stage=EVALUATE_TUNED_TEST "
            f"completed (status={state.tuned_test})"
        )

    @staticmethod
    def _get_case_insensitive_value(row: dict, key: str):
        lowered = key.lower()
        for existing_key, value in row.items():
            if str(existing_key).lower() == lowered:
                return value
        return None

    @classmethod
    def _summarize_test_rows_for_retry(cls, rows: list[dict]) -> str:
        if not rows:
            return "no_rows_returned"

        samples: list[str] = []
        for row in rows[:5]:
            case_no = cls._get_case_insensitive_value(row, "case_no")
            from_count = cls._get_case_insensitive_value(row, "from_count")
            to_count = cls._get_case_insensitive_value(row, "to_count")
            samples.append(f"CASE_NO={case_no},BASELINE_COUNT={from_count},TUNED_COUNT={to_count}")
        return " ; ".join(samples)


class TobeMultiAgentCoordinator:
    """Runs the SQL conversion workflow and persists job results."""

    def __init__(
        self,
        mapping_rule_provider: MappingRuleProvider | None = None,
        generation_agent: TobeSqlGenerationAgent | None = None,
        tuning_agent: SqlTuningAgent | None = None,
    ) -> None:
        self.mapping_rule_provider = mapping_rule_provider or MappingRuleProvider()
        self.generation_agent = generation_agent or TobeSqlGenerationAgent()
        self.graph = build_migration_workflow(
            generation_agent=self.generation_agent,
        )

    def process_job(self, job) -> str:
        logger.info("\n==========================================")
        logger.info(f"[TobeMultiAgentCoordinator] Starting job ({job.space_nm}.{job.sql_id})")
        job_key = f"{job.space_nm}.{job.sql_id}"

        retry_count = 0
        max_retries = 3
        stage = "INIT"
        state = self._build_state(job=job, last_error=None)
        sql_length = classify_sql_length(job.fr_sql_text, job.edit_fr_sql)
        map_type = get_sql_map_type(job.target_table)
        update_job_classification(row_id=job.row_id, sql_length=sql_length, map_type=map_type)
        job.sql_length = sql_length
        job.map_type = map_type
        logger.info(
            f"[TobeMultiAgentCoordinator] ({job_key}) classification "
            f"sql_length={sql_length}, map_type={map_type or 'UNKNOWN'}"
        )

        if is_fail(job.status):
            reset_tuning_state(job.row_id)
            job.tuned_sql = None
            job.tuned_test = None

        unready_target_tables = get_unready_target_tables(job.target_table)
        if unready_target_tables:
            reason = "TARGET_MAPPING_NOT_READY: " + ",".join(unready_target_tables)
            update_job_skip(row_id=job.row_id, reason=reason)
            insert_sql_log(
                space_nm=job.space_nm,
                sql_id=job.sql_id,
                sql_info_rowid=job.row_id,
                sql_kind="ERROR",
                sql_content=None,
                status="SKIP",
                model_name=os.getenv("LLM_MODEL", "").strip(),
                attempt_no=_attempt_no(state.last_error),
                stage_name="CHECK_TARGET_MAPPING",
                error_message=reason,
            )
            logger.warning(f"[TobeMultiAgentCoordinator] ({job_key}) excluded: {reason}")
            return "SKIP"

        while retry_count < max_retries:
            raw_last_error = state.last_error
            state = self._build_state(job=job, last_error=raw_last_error)
            attempt = retry_count + 1
            state.last_error = self._build_retry_prompt_context(
                last_error=raw_last_error,
                attempt=attempt,
                max_retries=max_retries,
            )
            if state.last_error:
                final_retry_mode = attempt >= max_retries
                logger.warning(
                    f"[TobeMultiAgentCoordinator] ({job_key}) stage=RETRY_PROMPT_CONTEXT "
                    f"attempt={attempt}/{max_retries} final_retry_mode={'ON' if final_retry_mode else 'OFF'}"
                )
            try:
                graph_result = self.graph.invoke({"execution": state, "terminal_action": None})
                state = graph_result["execution"]
                terminal_action = graph_result.get("terminal_action")
                stage = terminal_action or stage

                tag_kind = (job.tag_kind or "").strip().upper()
                if tag_kind != "SELECT":
                    self._complete_non_select_job(state, tag_kind)
                    return CONVERSION_PASS

                if state.status != "PASS":
                    state.failure_status = FAIL_TEST
                    retry_count += 1
                    state.last_error = "TEST_VALIDATION_FAIL: " + self._summarize_test_rows_for_retry(state.test_rows)
                    logger.warning(
                        f"[TobeMultiAgentCoordinator] ({job_key}) stage={stage} status=FAIL "
                        f"(retry={retry_count}/{max_retries}): {state.last_error}"
                    )
                    if retry_count < max_retries:
                        self._sleep_with_backoff(retry_count)
                        continue
                    break

                self._persist_success(state)
                return CONVERSION_PASS

            except LLMRateLimitError as exc:
                retry_count += 1
                stage = "LLM_CALL"
                state.last_error = str(exc)
                logger.warning(
                    f"[TobeMultiAgentCoordinator] ({job_key}) stage={stage} LLM rate limit "
                    f"(retry={retry_count}/{max_retries}): {state.last_error}"
                )
                if retry_count >= max_retries:
                    break
                self._sleep_with_backoff(retry_count)

            except Exception as exc:
                retry_count += 1
                state.last_error = str(exc)
                logger.error(
                    f"[TobeMultiAgentCoordinator] ({job_key}) stage={stage} error "
                    f"(retry={retry_count}/{max_retries}): {state.last_error}"
                )
                if retry_count >= max_retries:
                    break
                self._sleep_with_backoff(retry_count)

        self._persist_failure(state=state, stage=stage, retry_count=retry_count)
        return state.failure_status or FAIL_TOBE

    def _build_state(self, job, last_error: str | None) -> JobExecutionState:
        return JobExecutionState(
            job=job,
            job_key=f"{job.space_nm}.{job.sql_id}",
            mapping_rules=self.mapping_rule_provider.get_rules(),
            last_error=last_error,
        )

    @staticmethod
    def _build_retry_prompt_context(
        last_error: str | None,
        attempt: int,
        max_retries: int,
    ) -> str | None:
        if not last_error:
            return None
        final_retry_mode = attempt >= max_retries
        mode = "ON" if final_retry_mode else "OFF"
        return (
            f"RETRY_CONTEXT: attempt={attempt}/{max_retries}; "
            f"FINAL_RETRY_MODE={mode}; "
            f"last_error={last_error}"
        )

    @staticmethod
    def _persist_success(state: JobExecutionState) -> None:
        final_log = f"FINAL SUCCESS stage=COMPLETED status={state.status} job={state.job_key}"
        update_cycle_result(
            row_id=state.job.row_id,
            tobe_sql=state.tobe_sql,
            tuned_sql=state.tuned_sql or None,
            tuned_result=state.tuned_result or None,
            tuned_test=state.tuned_test or "READY",
            bind_sql=state.bind_sql,
            bind_set=state.bind_set_for_db,
            test_sql=state.test_sql,
            status=CONVERSION_PASS if state.status == "PASS" else (state.failure_status or FAIL_TEST),
            final_log=final_log,
            formatted_sql=state.formatted_sql or None,
        )
        logger.info(f"[TobeMultiAgentCoordinator] ({state.job_key}) completed successfully.")

    @staticmethod
    def _persist_failure(state: JobExecutionState, stage: str, retry_count: int) -> None:
        final_log = (
            f"FINAL FAIL stage={stage} retry_count={retry_count} "
            f"job={state.job_key} error={state.last_error or 'UNKNOWN'}"
        )
        update_cycle_result(
            row_id=state.job.row_id,
            tobe_sql=state.tobe_sql,
            tuned_sql=state.tuned_sql or None,
            tuned_result=state.tuned_result or None,
            tuned_test=state.tuned_test,
            bind_sql=state.bind_sql,
            bind_set=state.bind_set_for_db,
            test_sql=state.test_sql,
            status=state.failure_status or FAIL_TOBE,
            final_log=final_log,
        )
        logger.error(f"[TobeMultiAgentCoordinator] ({state.job_key}) failed after retries: {state.last_error}")

    @staticmethod
    def _complete_non_select_job(state: JobExecutionState, tag_kind: str) -> None:
        final_log = (
            f"FINAL SUCCESS stage=COMPLETED status={CONVERSION_PASS} "
            f"job={state.job_key} reason=TAG_KIND:{tag_kind or 'UNKNOWN'}"
        )
        update_cycle_result(
            row_id=state.job.row_id,
            tobe_sql=state.tobe_sql,
            tuned_sql=state.tuned_sql or None,
            tuned_result=state.tuned_result or None,
            tuned_test=state.tuned_test or "READY",
            bind_sql="",
            bind_set=None,
            test_sql="",
            status=CONVERSION_PASS,
            final_log=final_log,
            formatted_sql=state.formatted_sql or None,
        )
        logger.info(
            f"[TobeMultiAgentCoordinator] ({state.job_key}) stage=SKIP_TEST_FOR_NON_SELECT "
            f"completed (tag_kind={tag_kind or 'UNKNOWN'})"
        )

    @staticmethod
    def _sleep_with_backoff(retry_count: int) -> None:
        base = min(8, 2 ** max(0, retry_count - 1))
        jitter = random.uniform(0.0, 0.7)
        time.sleep(base + jitter)

    @staticmethod
    def _get_case_insensitive_value(row: dict, key: str):
        lowered = key.lower()
        for existing_key, value in row.items():
            if str(existing_key).lower() == lowered:
                return value
        return None

    @classmethod
    def _summarize_test_rows_for_retry(cls, rows: list[dict]) -> str:
        if not rows:
            return "no_rows_returned"

        samples: list[str] = []
        for row in rows[:5]:
            case_no = cls._get_case_insensitive_value(row, "case_no")
            from_count = cls._get_case_insensitive_value(row, "from_count")
            to_count = cls._get_case_insensitive_value(row, "to_count")
            samples.append(f"CASE_NO={case_no},FROM_COUNT={from_count},TO_COUNT={to_count}")
        return " ; ".join(samples)

