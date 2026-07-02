import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from server.core.exceptions import LLMRateLimitError
from server.core.llm_fallback import (
    get_active_model,
    is_model_fallback_error,
    model_candidates,
    reset_active_model,
    set_active_model,
)
from server.core.logger import logger
from server.repositories.sql.log_repository import insert_sql_log
from server.services.sql.domain_models import MappingRuleItem, SqlInfoJob
from server.services.sql.prompt_service import build_prompt_messages
from server.services.sql.correct_sql_rag_service import correct_sql_hint_rag_service
from server.services.sql.tobe_sql_tuning_service import tobe_sql_tuning_service


# unified_agent/ 프로젝트 루트
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")
_BIND_TOKEN_PATTERN = re.compile(r"[#$]\{\s*([^}]+?)\s*\}")
_SQL_START_KEYWORDS = r"SELECT|INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|WITH"
_MYBATIS_WRAPPER_TAGS = r"script|select|insert|update|delete"
_MYBATIS_DYNAMIC_TAGS = r"if|choose|when|otherwise|where|trim|foreach"


def _env_or_value(value: str | None, env_name: str) -> str:
    resolved = value or os.getenv(env_name)
    if not resolved:
        raise ValueError(f"Required environment variable '{env_name}' is not set.")
    return resolved


def _normalize_anthropic_base_url(raw_base_url: str) -> str:
    normalized = raw_base_url.strip().rstrip("/")
    if normalized.endswith("/v1/message"):
        return normalized[: -len("/v1/message")]
    if normalized.endswith("/v1/messages"):
        return normalized[: -len("/v1/messages")]
    if normalized.endswith("/v1"):
        return normalized[: -len("/v1")]
    return normalized


def _normalize_openai_base_url(raw_base_url: str) -> str:
    normalized = raw_base_url.strip().rstrip("/")
    for suffix in ("/chat/completions", "/responses", "/completions", "/models"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def _resolve_llm_provider(provider: str | None, base_url: str, model: str) -> str:
    resolved = (provider or os.getenv("LLM_PROVIDER") or "").strip().lower()
    if resolved:
        if resolved not in {"anthropic", "openai"}:
            raise ValueError("LLM_PROVIDER must be either 'anthropic' or 'openai'.")
        return resolved

    lowered_base = base_url.lower()
    lowered_model = model.lower()
    if "anthropic" in lowered_base or lowered_model.startswith("claude"):
        return "anthropic"
    return "openai"


def _serialize_mapping_rules(mapping_rules: list[MappingRuleItem], section_name: str = "MAPPING_RULES") -> str:
    if not mapping_rules:
        return f"[{section_name}]\n- (empty)"

    rows: set[tuple[str, str, str, str]] = set()
    source_schema = _schema_env("ORACLE_SCHEMA_SRC")
    target_schema = _schema_env("ORACLE_SCHEMA_TGT")
    for rule in mapping_rules:
        fr_table = _qualify_mapping_table(rule.fr_table, source_schema)
        to_table = _qualify_mapping_table(rule.to_table, target_schema)
        fr_col = (rule.fr_col or "").strip()
        to_col = (rule.to_col or "").strip()
        if fr_table and to_table and fr_col and to_col:
            rows.add((fr_table, fr_col, to_table, to_col))

    lines = [f"[{section_name}]"]
    if not rows:
        lines.append("- (empty)")
        return "\n".join(lines)

    for fr_table, fr_col, to_table, to_col in sorted(rows):
        lines.append(
            f"- FR_TABLE={fr_table} | FR_COL={fr_col} | "
            f"TO_TABLE={to_table} | TO_COL={to_col}"
        )
    return "\n".join(lines)


def _serialize_sql_conversion_mapping_rules(
    migration_rules: list[MappingRuleItem],
    conversion_general_rules: list[dict[str, Any]],
    conversion_examples: list[dict[str, Any]],
) -> str:
    sections = [_serialize_mapping_rules(migration_rules, section_name="MIGRATION_MAPPING_RULES"), ""]
    sections.append("[UNMAPPED_NAME_POLICY]")
    sections.append(
        "- If a source table or column has no matching MIGRATION_MAPPING_RULES entry, keep the original table or column name unchanged."
    )
    sections.append(
        "- Treat this unchanged-name path as an explicit conversion decision, not as a reason to skip the job."
    )
    sections.append("")

    sections.append("[SQL_CONVERSION_GENERAL_RAG_GUIDANCE]")
    if not conversion_general_rules:
        sections.append("- (empty)")
    for rule in conversion_general_rules:
        source_tables = ",".join(rule.get("source_tables") or []) or "ALL"
        sections.append(f"- RAG_ID={rule.get('rule_id')} | SOURCE_TABLES={source_tables}")
        for guidance in rule.get("guidance") or []:
            sections.append(f"  - {guidance}")

    sections.append("")
    sections.append("[SQL_CONVERSION_SEARCH_RAG_TOP_K_BY_SQL_BLOCK]")
    serialized_examples = serialize_conversion_examples_for_prompt(conversion_examples)
    if serialized_examples == "[]":
        sections.append("- (empty)")
    else:
        sections.append(serialized_examples)
    sections.append("")
    return "\n".join(sections)


def _extract_rag_rule_ids(payloads: list[dict[str, Any]]) -> list[str]:
    rule_ids: list[str] = []
    for payload in payloads or []:
        for match in payload.get("top_rule_matches", []) if isinstance(payload, dict) else []:
            if not isinstance(match, dict):
                continue
            rule_id = str(match.get("rule_id") or "").strip()
            if rule_id:
                rule_ids.append(rule_id)
    return rule_ids


def _increment_prompt_rag_hits(payloads: list[dict[str, Any]]) -> None:
    rule_ids = sorted(set(_extract_rag_rule_ids(payloads)))
    if rule_ids:
        tobe_sql_tuning_service.increment_rule_hit_counts(rule_ids, expected_rule_type="SEARCH")


def _qualify_mapping_table(table_name: str, schema: str) -> str:
    table = (table_name or "").strip()
    if not table or "." in table or not schema:
        return table
    return f"{schema}.{table}"


def _normalize_table_token(token: str) -> str:
    value = (token or "").strip().strip("[]").strip().strip('"').strip("'").strip()
    if not value:
        return ""
    if "." in value:
        value = value.split(".")[-1]
    return value.strip("[]").strip().strip('"').strip("'").upper()


def _load_target_tables(job: SqlInfoJob) -> set[str]:
    raw = (job.target_table or "").strip()
    if not raw:
        return set()

    tokens: list[str] = []
    if raw.startswith("[") or raw.startswith("{"):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                tokens = [str(item) for item in parsed]
            elif isinstance(parsed, str):
                tokens = [parsed]
        except Exception:
            tokens = []

    if not tokens:
        tokens = re.split(r"[,\s;|]+", raw)

    return {normalized for token in tokens if (normalized := _normalize_table_token(token))}


def _extract_referenced_fr_tables_from_source_sql(source_sql: str, candidate_fr_tables: set[str]) -> set[str]:
    if not source_sql or not candidate_fr_tables:
        return set()

    text = re.sub(r"/\*.*?\*/", " ", source_sql, flags=re.DOTALL)
    text = re.sub(r"--[^\n]*", " ", text)
    text = re.sub(r"'(?:''|[^'])*'", " ", text)
    scan = text.upper()

    matched: set[str] = set()
    for table in candidate_fr_tables:
        pattern = rf"(?<![A-Z0-9_$#]){re.escape(table)}(?![A-Z0-9_$#])"
        if re.search(pattern, scan):
            matched.add(table)
    return matched


def _select_mapping_rules_for_job(job: SqlInfoJob, mapping_rules: list[MappingRuleItem]) -> list[MappingRuleItem]:
    if not mapping_rules:
        return []

    target_tables = _load_target_tables(job)
    if target_tables:
        return [
            rule
            for rule in mapping_rules
            if _fr_table_contains_any_target(rule.fr_table, target_tables)
        ]

    rules_by_fr: dict[str, list[MappingRuleItem]] = {}
    for rule in mapping_rules:
        fr_norm = _normalize_table_token(rule.fr_table)
        if fr_norm:
            rules_by_fr.setdefault(fr_norm, []).append(rule)

    selected_fr_tables = _extract_referenced_fr_tables_from_source_sql(job.source_sql, set(rules_by_fr.keys()))

    if not selected_fr_tables:
        return mapping_rules

    filtered: list[MappingRuleItem] = []
    for fr_table in sorted(selected_fr_tables):
        filtered.extend(rules_by_fr.get(fr_table, []))
    return filtered


def _select_mapping_rules_for_target_tables(
    mapping_rules: list[MappingRuleItem],
    target_tables: set[str],
) -> list[MappingRuleItem]:
    if not mapping_rules or not target_tables:
        return []
    return [
        rule
        for rule in mapping_rules
        if _fr_table_contains_any_target(rule.fr_table, target_tables)
    ]


def _fr_table_contains_any_target(fr_table: str, target_tables: set[str]) -> bool:
    fr_text = (fr_table or "").upper()
    if not fr_text or not target_tables:
        return False

    for table in target_tables:
        pattern = rf"(?<![A-Z0-9_$#]){re.escape(table)}(?![A-Z0-9_$#])"
        if re.search(pattern, fr_text):
            return True
    return False


def serialize_tuning_examples_for_log(tuning_examples: list[dict[str, Any]]) -> str:
    if not tuning_examples:
        return "[]"
    return json.dumps(tuning_examples, ensure_ascii=False, indent=2, default=str)


def serialize_tuning_examples_for_prompt(tuning_examples: list[dict[str, Any]]) -> str:
    if not tuning_examples:
        return "[]"

    compact_examples: list[dict[str, object]] = []
    for block in tuning_examples:
        if not isinstance(block, dict):
            continue

        source_sql = block.get("source_sql", block.get("from_sql", ""))
        for rule_match in block.get("top_rule_matches", []):
            if isinstance(rule_match, dict):
                compact_examples.append(
                    {
                        "source_sql": source_sql,
                        "guidance": rule_match.get("guidance", []),
                        "example_bad_sql": rule_match.get("example_bad_sql", ""),
                        "example_tuned_sql": rule_match.get("example_tuned_sql", ""),
                    }
                )

    return json.dumps(compact_examples, ensure_ascii=False, indent=2)


def serialize_conversion_examples_for_prompt(conversion_examples: list[dict[str, Any]]) -> str:
    if not conversion_examples:
        return "[]"

    compact_examples: list[dict[str, object]] = []
    for block in conversion_examples:
        if not isinstance(block, dict):
            continue

        block_source_sql = block.get("source_sql", block.get("from_sql", ""))
        for rule_match in block.get("top_rule_matches", []):
            if isinstance(rule_match, dict):
                compact_examples.append(
                    {
                        "matched_block_sql": block_source_sql,
                        "guidance": rule_match.get("guidance", []),
                        "source_sql": rule_match.get("source_sql", rule_match.get("example_bad_sql", "")),
                        "target_sql": rule_match.get("target_sql", rule_match.get("example_tuned_sql", "")),
                    }
                )

    return json.dumps(compact_examples, ensure_ascii=False, indent=2)


def serialize_correct_sql_hints_for_prompt(hints: list[dict[str, object]]) -> str:
    correct_sqls = [
        str(hint.get("correct_sql", "")).strip()
        for hint in hints
        if isinstance(hint, dict) and str(hint.get("correct_sql", "")).strip()
    ]
    return json.dumps(correct_sqls, ensure_ascii=False, indent=2)


def _build_sql_messages(template_name: str, **payload: str) -> list[dict[str, str]]:
    return build_prompt_messages(template_name, **payload)


def _call_llm_for_job(
    *,
    job: SqlInfoJob | None,
    sql_kind: str,
    prompt_name: str,
    messages: list[dict[str, str]],
    last_error: str | None = None,
    stage_name: str | None = None,
) -> str:
    started = time.perf_counter()
    try:
        sql_text = call_llm_api(
            api_key=None,
            model=None,
            base_url=None,
            messages=messages,
        )
        insert_sql_log(
            space_nm=job.space_nm if job else None,
            sql_id=job.sql_id if job else None,
            sql_info_rowid=job.row_id if job else None,
            sql_kind=sql_kind,
            sql_content=sql_text,
            status="SUCCESS",
            prompt_name=prompt_name,
            model_name=_model_name(),
            elapsed_seconds=time.perf_counter() - started,
            attempt_no=_attempt_no(last_error),
            stage_name=stage_name or f"GENERATE_{sql_kind}",
        )
        return sql_text
    except Exception as exc:
        insert_sql_log(
            space_nm=job.space_nm if job else None,
            sql_id=job.sql_id if job else None,
            sql_info_rowid=job.row_id if job else None,
            sql_kind=sql_kind,
            sql_content=None,
            status="FAIL",
            prompt_name=prompt_name,
            model_name=_model_name(),
            elapsed_seconds=time.perf_counter() - started,
            attempt_no=_attempt_no(last_error),
            stage_name=stage_name or f"GENERATE_{sql_kind}",
            error_message=str(exc),
        )
        raise


def _call_formatter_llm_for_job(
    *,
    job: SqlInfoJob | None,
    input_sql: str,
) -> str:
    prompt_name = "sql_indent_format_prompt.json"
    sql_kind = "FORMATTED_SQL"
    started = time.perf_counter()
    try:
        formatted_sql = call_llm_text_api(
            api_key=None,
            model=None,
            base_url=None,
            messages=_build_sql_messages(prompt_name, input_sql=input_sql),
        ).strip()
        if not formatted_sql:
            raise ValueError("LLM returned an empty formatted SQL.")
        insert_sql_log(
            space_nm=job.space_nm if job else None,
            sql_id=job.sql_id if job else None,
            sql_info_rowid=job.row_id if job else None,
            sql_kind=sql_kind,
            sql_content=formatted_sql,
            status="SUCCESS",
            prompt_name=prompt_name,
            model_name=_model_name(),
            elapsed_seconds=time.perf_counter() - started,
            stage_name="GENERATE_FORMATTED_SQL",
        )
        return formatted_sql
    except Exception as exc:
        insert_sql_log(
            space_nm=job.space_nm if job else None,
            sql_id=job.sql_id if job else None,
            sql_info_rowid=job.row_id if job else None,
            sql_kind=sql_kind,
            sql_content=None,
            status="FAIL",
            prompt_name=prompt_name,
            model_name=_model_name(),
            elapsed_seconds=time.perf_counter() - started,
            stage_name="GENERATE_FORMATTED_SQL",
            error_message=str(exc),
        )
        raise


def _call_tuning_llm_for_job(
    *,
    job: SqlInfoJob | None,
    prompt_name: str,
    messages: list[dict[str, str]],
    last_error: str | None = None,
) -> tuple[str, str]:
    started = time.perf_counter()
    try:
        response_text = call_llm_text_api(
            api_key=None,
            model=None,
            base_url=None,
            messages=messages,
        )
        tuned_sql, tuned_result = _extract_tuning_response(response_text)
        elapsed_seconds = time.perf_counter() - started
        insert_sql_log(
            space_nm=job.space_nm if job else None,
            sql_id=job.sql_id if job else None,
            sql_info_rowid=job.row_id if job else None,
            sql_kind="TUNED_SQL",
            sql_content=tuned_sql,
            status="SUCCESS",
            prompt_name=prompt_name,
            model_name=_model_name(),
            elapsed_seconds=elapsed_seconds,
            attempt_no=_attempt_no(last_error),
            stage_name="GENERATE_TUNED_SQL",
        )
        if tuned_result:
            insert_sql_log(
                space_nm=job.space_nm if job else None,
                sql_id=job.sql_id if job else None,
                sql_info_rowid=job.row_id if job else None,
                sql_kind="TUNED_RESULT",
                sql_content=tuned_result,
                status="SUCCESS",
                prompt_name=prompt_name,
                model_name=_model_name(),
                elapsed_seconds=elapsed_seconds,
                attempt_no=_attempt_no(last_error),
                stage_name="GENERATE_TUNED_RESULT",
            )
        return tuned_sql, tuned_result
    except Exception as exc:
        insert_sql_log(
            space_nm=job.space_nm if job else None,
            sql_id=job.sql_id if job else None,
            sql_info_rowid=job.row_id if job else None,
            sql_kind="TUNED_SQL",
            sql_content=None,
            status="FAIL",
            prompt_name=prompt_name,
            model_name=_model_name(),
            elapsed_seconds=time.perf_counter() - started,
            attempt_no=_attempt_no(last_error),
            stage_name="GENERATE_TUNED_SQL",
            error_message=str(exc),
        )
        raise


def _extract_tuning_response(response_text: str) -> tuple[str, str]:
    text = response_text.strip()
    if not text:
        raise ValueError("LLM returned an empty tuning response.")

    code_block_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    json_text = code_block_match.group(1).strip() if code_block_match else text
    parsed: object | None = None
    try:
        parsed = json.loads(json_text)
    except Exception:
        object_match = re.search(r"\{.*\}", json_text, flags=re.DOTALL)
        if object_match:
            try:
                parsed = json.loads(object_match.group(0))
            except Exception:
                parsed = None

    if isinstance(parsed, dict):
        tuned_sql_raw = str(parsed.get("tuned_sql") or parsed.get("TUNED_SQL") or "").strip()
        tuned_result = str(parsed.get("tuned_result") or parsed.get("TUNED_RESULT") or "").strip()
        if not tuned_sql_raw:
            raise ValueError("LLM tuning response did not include tuned_sql.")
        return _extract_sql_text(tuned_sql_raw), tuned_result

    labeled_match = re.search(
        r"tuned_sql\s*:\s*(?P<tuned_sql>.*?)(?:\n\s*tuned_result\s*:|$)(?P<tuned_result>.*)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if labeled_match:
        tuned_sql_raw = labeled_match.group("tuned_sql").strip()
        tuned_result = labeled_match.group("tuned_result").strip()
        if not tuned_sql_raw:
            raise ValueError("LLM tuning response did not include tuned_sql.")
        return _extract_sql_text(tuned_sql_raw), tuned_result

    tuned_sql = _extract_sql_text(text)
    return tuned_sql, ""


def _extract_sql_text(response_text: str) -> str:
    text = response_text.strip()
    code_block_match = re.search(r"```(?:sql)?\s*(.*?)```", text, re.IGNORECASE | re.DOTALL)
    if code_block_match:
        text = code_block_match.group(1).strip()
    if not text:
        raise ValueError("LLM returned an empty response.")

    text = _strip_leading_non_sql_text(text)
    text = _strip_mybatis_wrapper_tags(text)

    if not _starts_with_sql_or_dynamic_tag(text):
        raise ValueError("LLM response does not start with executable SQL.")
    return _normalize_oracle_sql(text)


def _strip_leading_non_sql_text(text: str) -> str:
    start_patterns = [
        rf"\b(?:{_SQL_START_KEYWORDS})\b",
        rf"<\s*(?:{_MYBATIS_WRAPPER_TAGS})\b",
        rf"<\s*(?:{_MYBATIS_DYNAMIC_TAGS})\b",
    ]
    matches = [match for pattern in start_patterns if (match := re.search(pattern, text, re.IGNORECASE))]
    if not matches:
        return text.strip()
    first = min(matches, key=lambda match: match.start())
    return text[first.start() :].strip()


def _strip_mybatis_wrapper_tags(text: str) -> str:
    stripped = text.strip()
    while True:
        open_match = re.match(
            rf"^<\s*({_MYBATIS_WRAPPER_TAGS})\b[^>]*>",
            stripped,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not open_match:
            return stripped
        tag_name = open_match.group(1)
        stripped = stripped[open_match.end() :].strip()
        stripped = re.sub(
            rf"</\s*{re.escape(tag_name)}\s*>\s*$",
            "",
            stripped,
            flags=re.IGNORECASE,
        ).strip()


def _starts_with_sql_or_dynamic_tag(text: str) -> bool:
    return bool(
        re.match(rf"^(?:{_SQL_START_KEYWORDS})\b", text, re.IGNORECASE)
        or re.match(rf"^<\s*(?:{_MYBATIS_DYNAMIC_TAGS})\b", text, re.IGNORECASE)
    )


def _normalize_bind_name(token: str) -> str:
    cleaned = (token or "").strip()
    if not cleaned:
        return ""
    return cleaned.split(".")[-1].strip()


def _sql_literal(value) -> str:
    if value is not None and hasattr(value, "read"):
        value = value.read()
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, datetime):
        return f"TO_DATE('{value.strftime('%Y-%m-%d')}', 'YYYY-MM-DD')"
    if isinstance(value, date):
        return f"TO_DATE('{value.isoformat()}', 'YYYY-MM-DD')"

    text = str(value)
    iso_match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T\s].*)?$", text)
    if iso_match:
        return f"TO_DATE('{iso_match.group(1)}', 'YYYY-MM-DD')"
    return "'" + text.replace("'", "''") + "'"


def _strip_sqlplus_terminator_lines(lines: Iterable[str]) -> list[str]:
    return [line for line in lines if line.strip() != "/"]


def _replace_limit_with_fetch_first(text: str) -> str:
    return re.sub(r"\s+LIMIT\s+(\d+)\s*$", r" FETCH FIRST \1 ROWS ONLY", text, flags=re.IGNORECASE)


def _normalize_oracle_sql(sql_text: str) -> str:
    text = sql_text.replace("﻿", "").replace("​", "").replace(" ", " ")
    text = "\n".join(_strip_sqlplus_terminator_lines(text.splitlines())).strip()
    text = _replace_limit_with_fetch_first(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\s+\n", "\n", text)
    text = text.strip().rstrip(";").strip()

    if not text:
        raise ValueError("LLM returned an empty SQL statement after normalization.")
    return text


def _to_langchain_messages(messages: list[dict[str, str]]):
    converted = []
    for message in messages:
        if message.get("role") == "system":
            converted.append(SystemMessage(content=message.get("content", "")))
        else:
            converted.append(HumanMessage(content=message.get("content", "")))
    return converted


def _ensure_anthropic_message_requirements(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    safe = list(messages or [])
    has_user_or_assistant = any((message.get("role") or "").lower() in {"user", "assistant"} for message in safe)
    if not has_user_or_assistant:
        safe.append(
            {
                "role": "user",
                "content": "Generate one executable Oracle SQL statement only. Do not end the SQL with a semicolon.",
            }
        )
    return safe


def call_llm_api(
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    messages: list[dict[str, str]],
    provider: str | None = None,
) -> str:
    return _extract_sql_text(
        call_llm_text_api(
            api_key=api_key,
            model=model,
            base_url=base_url,
            messages=messages,
            provider=provider,
        )
    )


def call_llm_text_api(
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    messages: list[dict[str, str]],
    provider: str | None = None,
) -> str:
    resolved_api_key = api_key or os.getenv("OPEN_API_KEY") or os.getenv("LLM_API_KEY")
    if not resolved_api_key:
        raise ValueError("Required environment variable 'OPEN_API_KEY' or 'LLM_API_KEY' is not set.")
    resolved_model = _env_or_value(model, "LLM_MODEL")
    raw_base_url = _env_or_value(base_url, "LLM_BASE_URL")
    candidates = model_candidates(resolved_model)
    last_exc: Exception | None = None

    for idx, candidate_model in enumerate(candidates):
        resolved_provider = _resolve_llm_provider(provider=provider, base_url=raw_base_url, model=candidate_model)
        try:
            if resolved_provider == "anthropic":
                llm = ChatAnthropic(
                    api_key=resolved_api_key,
                    model_name=candidate_model,
                    anthropic_api_url=_normalize_anthropic_base_url(raw_base_url),
                    max_tokens_to_sample=int(os.getenv("LLM_MAX_TOKENS", "4096")),
                    temperature=0,
                )
                safe_messages = _ensure_anthropic_message_requirements(messages)
            else:
                llm = ChatOpenAI(
                    api_key=resolved_api_key,
                    model=candidate_model,
                    base_url=_normalize_openai_base_url(raw_base_url),
                    temperature=0,
                )
                safe_messages = list(messages or [])

            response = llm.invoke(_to_langchain_messages(safe_messages))
            content = getattr(response, "content", response)
            if isinstance(content, list):
                text = "".join(item.get("text", "") if isinstance(item, dict) else str(item) for item in content)
            else:
                text = str(content)
            if idx == len(candidates) - 1:
                reset_active_model()
            else:
                set_active_model(candidate_model)
            return text
        except Exception as exc:
            message = str(exc)
            lowered = message.lower()
            if idx < len(candidates) - 1 and is_model_fallback_error(message):
                logger.warning(
                    f"[LLM] model fallback: {candidate_model} failed ({message}); "
                    f"trying {candidates[idx + 1]}"
                )
                last_exc = exc
                continue
            if "429" in message or "rate limit" in lowered or "504" in message or "gateway timeout" in lowered or "timed out" in lowered:
                raise LLMRateLimitError(message) from exc
            raise

    if last_exc:
        raise last_exc
    raise ValueError("No LLM model candidates are configured.")


def generate_tobe_sql(
    job: SqlInfoJob,
    mapping_rules: list[MappingRuleItem],
    last_error: str | None = None,
) -> str:
    template_name = "tobe_sql_prompt.json"
    scoped_rules = _select_mapping_rules_for_job(job=job, mapping_rules=mapping_rules)
    source_tables = _load_target_tables(job)
    conversion_general_rules = tobe_sql_tuning_service.load_universal_conversion_rules(
        sql_text=job.source_sql,
        source_tables=source_tables,
    )
    conversion_examples = tobe_sql_tuning_service.retrieve_conversion_examples(
        sql_text=job.source_sql,
        source_tables=source_tables,
    )
    mapping_schema_text = _serialize_sql_conversion_mapping_rules(
        migration_rules=scoped_rules,
        conversion_general_rules=conversion_general_rules,
        conversion_examples=conversion_examples,
    )
    _increment_prompt_rag_hits(conversion_examples)

    correct_sql_hint_json = "[]"
    correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(
        sql_text=job.source_sql,
        correct_kind="TOBE",
        current_row_id=job.row_id,
    )
    correct_sql_hint_json = serialize_correct_sql_hints_for_prompt(correct_sql_hints)
    return _call_llm_for_job(
        job=job,
        sql_kind="TOBE_SQL",
        prompt_name=template_name,
        last_error=last_error,
        messages=_build_sql_messages(
            template_name,
            from_sql=job.source_sql,
            mapping_schema_text=mapping_schema_text,
            target_schema=_schema_env("ORACLE_SCHEMA_TGT") or "UNKNOWN",
            correct_sql_hint_json=correct_sql_hint_json,
            last_error=last_error or "None",
        ),
    )



def generate_bind_tuned_sql(
    job: SqlInfoJob,
    last_error: str | None = None,
) -> str:
    template_name = "bind_tuned_sql_prompt.json"
    source_tables = _load_target_tables(job)
    tuning_examples = tobe_sql_tuning_service.retrieve_tuning_examples(
        job.source_sql,
        source_tables=source_tables,
    )
    _increment_prompt_rag_hits(tuning_examples)
    return _call_llm_for_job(
        job=job,
        sql_kind="BIND_TUNED_SQL",
        prompt_name=template_name,
        last_error=last_error,
        messages=_build_sql_messages(
            template_name,
            current_from_sql=job.source_sql,
            universal_tuning_rules=json.dumps(
                tobe_sql_tuning_service.load_universal_tuning_rules(
                    job.source_sql,
                    source_tables=source_tables,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            tuning_examples_json=serialize_tuning_examples_for_prompt(tuning_examples),
            last_error=last_error or "None",
        ),
    )

def generate_bind_sql(
    job: SqlInfoJob,
    last_error: str | None = None,
    bind_source_sql: str | None = None,
) -> str:
    template_name = "bind_sql_final_retry_prompt.json" if _is_final_retry_mode(last_error) else "bind_sql_prompt.json"
    source_sql = bind_source_sql or job.source_sql
    correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(
        sql_text=job.source_sql,
        correct_kind="BIND",
        current_row_id=job.row_id,
    )
    return _call_llm_for_job(
        job=job,
        sql_kind="BIND_SQL",
        prompt_name=template_name,
        last_error=last_error,
        messages=_build_sql_messages(
            template_name,
            from_sql=source_sql,
            from_schema=_schema_env("ORACLE_SCHEMA_SRC") or "UNKNOWN",
            correct_sql_hint_json=serialize_correct_sql_hints_for_prompt(correct_sql_hints),
            last_error=last_error or "None",
        ),
    )


def tune_tobe_sql(
    current_tobe_sql: str,
    tuning_examples: list[dict[str, Any]] | None = None,
    last_error: str | None = None,
    job: SqlInfoJob | None = None,
) -> tuple[str, str]:
    template_name = "tobe_sql_tuning_prompt.json"
    source_tables = _load_target_tables(job) if job else set()
    _increment_prompt_rag_hits(tuning_examples or [])
    return _call_tuning_llm_for_job(
        job=job,
        prompt_name=template_name,
        last_error=last_error,
        messages=_build_sql_messages(
            template_name,
            current_tobe_sql=current_tobe_sql,
            universal_tuning_rules=json.dumps(
                tobe_sql_tuning_service.load_universal_tuning_rules(
                    current_tobe_sql,
                    source_tables=source_tables,
                ),
                ensure_ascii=False,
                indent=2,
            ),
            tuning_examples_json=serialize_tuning_examples_for_prompt(tuning_examples or []),
            last_error=last_error or "None",
        ),
    )


def _load_bind_sets_json(bind_set_json: str | None) -> str:
    try:
        bind_sets = json.loads(bind_set_json or "[]")
    except Exception:
        bind_sets = []
    if not isinstance(bind_sets, list):
        bind_sets = []
    if not bind_sets:
        bind_sets = [{}]
    return json.dumps(bind_sets, ensure_ascii=False, default=str)


def _schema_env(name: str) -> str:
    return (os.getenv(name) or "").strip().upper()


def _model_name() -> str:
    return (get_active_model() or os.getenv("LLM_MODEL") or "").strip()


def _attempt_no(last_error: str | None) -> int | None:
    match = re.search(r"\battempt\s*=\s*(\d+)\s*/\s*\d+", last_error or "", re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except Exception:
        return None


def _is_final_retry_mode(last_error: str | None) -> bool:
    text = (last_error or "").upper()
    return "FINAL_RETRY_MODE=ON" in text or "ATTEMPT=3/3" in text


def _generate_validation_test_sql(
    from_sql: str,
    tobe_sql: str,
    bind_set_json: str | None,
    from_schema: str,
    tobe_schema: str,
    last_error: str | None = None,
    final_retry_mode: bool = False,
    correct_sql_hint_json: str = "[]",
    job: SqlInfoJob | None = None,
    sql_kind: str = "TEST_SQL",
) -> str:
    template_name = "test_sql_final_retry_prompt.json" if final_retry_mode else "test_sql_prompt.json"
    return _call_llm_for_job(
        job=job,
        sql_kind=sql_kind,
        prompt_name=template_name,
        last_error=last_error,
        messages=_build_sql_messages(
            template_name,
            from_sql=from_sql,
            tobe_sql=tobe_sql,
            from_schema=from_schema or "UNKNOWN",
            tobe_schema=tobe_schema or "UNKNOWN",
            bind_set_json=_load_bind_sets_json(bind_set_json),
            correct_sql_hint_json=correct_sql_hint_json,
            last_error=last_error or "None",
        ),
    )


def generate_test_sql(
    job: SqlInfoJob,
    tobe_sql: str,
    bind_set_json: str,
    last_error: str | None = None,
) -> str:
    correct_sql_hints = correct_sql_hint_rag_service.retrieve_correct_sql_hints(
        sql_text=job.source_sql,
        correct_kind="TEST",
        current_row_id=job.row_id,
    )
    return _generate_validation_test_sql(
        from_sql=job.source_sql,
        tobe_sql=tobe_sql,
        bind_set_json=bind_set_json,
        from_schema=_schema_env("ORACLE_SCHEMA_SRC"),
        tobe_schema=_schema_env("ORACLE_SCHEMA_TGT"),
        last_error=last_error,
        final_retry_mode=_is_final_retry_mode(last_error),
        correct_sql_hint_json=serialize_correct_sql_hints_for_prompt(correct_sql_hints),
        job=job,
        sql_kind="TEST_SQL",
    )


def generate_sql_comparison_test_sql(
    baseline_sql: str,
    candidate_sql: str,
    bind_set_json: str | None = None,
    last_error: str | None = None,
    job: SqlInfoJob | None = None,
) -> str:
    template_name = "tuned_test_sql_prompt.json"
    return _call_llm_for_job(
        job=job,
        sql_kind="TUNED_TEST_SQL",
        prompt_name=template_name,
        last_error=last_error,
        messages=_build_sql_messages(
            template_name,
            baseline_tobe_sql=baseline_sql,
            tuned_sql=candidate_sql,
            tobe_schema=_schema_env("ORACLE_SCHEMA_TGT") or "UNKNOWN",
            bind_set_json=_load_bind_sets_json(bind_set_json),
            last_error=last_error or "None",
        ),
    )


def generate_formatted_sql(
    job: SqlInfoJob,
    input_sql: str,
) -> str:
    return _call_formatter_llm_for_job(job=job, input_sql=input_sql)
