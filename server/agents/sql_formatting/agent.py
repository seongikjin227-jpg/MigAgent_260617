"""SQL formatting agent."""

from server.core.logger import logger
from server.repositories.sql.result_repository import update_formatted_sql
from server.services.sql.llm_service import generate_formatted_sql


class SqlFormattingAgent:
    """Apply the indent formatting prompt to completed tuning rows."""

    name = "sql_formatting_agent"

    def process_job(self, job) -> str:
        job_key = f"{job.space_nm}.{job.sql_id}"
        source_sql = (job.tuned_sql or job.to_sql_text or "").strip()
        if not source_sql:
            logger.warning(f"[{self.name}] ({job_key}) stage=SKIP_FORMATTING completed (reason=no_sql)")
            return "SKIP"

        try:
            formatted_sql = generate_formatted_sql(job=job, input_sql=source_sql)
            update_formatted_sql(row_id=job.row_id, formatted_sql=formatted_sql)
            logger.info(
                f"[{self.name}] ({job_key}) stage=GENERATE_FORMATTED_SQL "
                f"completed (sql_length={len(formatted_sql)})"
            )
            return "PASS"
        except Exception as exc:
            logger.error(f"[{self.name}] ({job_key}) stage=GENERATE_FORMATTED_SQL failed: {exc}")
            return "FAIL"
