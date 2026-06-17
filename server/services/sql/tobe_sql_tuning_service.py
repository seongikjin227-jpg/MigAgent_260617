from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from server.core.logger import logger


_SERVICE_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SERVICE_DIR.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

SQL_CONVERSION = "SQL_CONVERSION"
SQL_TUNING = "SQL_TUNING"
RULE_GENERAL = "GENERAL"
RULE_SEARCH = "SEARCH"


class TobeSqlTuningService:
    """Shared SQL conversion/tuning RAG service backed by NEXT_MIG_RAG_INFO."""

    def __init__(self) -> None:
        self.top_k = max(1, int(os.getenv("TOBE_SQL_TUNING_TOP_K", "3")))
        self.embed_base_url = os.getenv("RAG_EMBED_BASE_URL", "").strip()
        self.embed_api_key = os.getenv("RAG_EMBED_API_KEY", "").strip()
        self.embed_model = os.getenv("RAG_EMBED_MODEL", "BAAI/bge-m3").strip()
        self.embed_timeout_sec = int(os.getenv("RAG_EMBED_TIMEOUT_SEC", "30"))
        self.table_name = os.getenv("RAG_INFO_TABLE", "NEXT_MIG_RAG_INFO").strip() or "NEXT_MIG_RAG_INFO"

    def retrieve_tuning_examples(
        self,
        sql_text: str,
        source_tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.retrieve_rag_examples(
            sql_text=sql_text,
            category=SQL_TUNING,
            source_tables=source_tables,
        )

    def retrieve_conversion_examples(
        self,
        sql_text: str,
        source_tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.retrieve_rag_examples(
            sql_text=sql_text,
            category=SQL_CONVERSION,
            source_tables=source_tables,
        )

    def retrieve_rag_examples(
        self,
        sql_text: str,
        category: str,
        source_tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        blocks = self._split_sql_into_blocks(sql_text)
        tables = self._normalize_table_set(source_tables)
        rules = self._load_search_rules(category=category, source_tables=tables)
        if not blocks or not rules:
            return []

        ordered_blocks = [block for block in blocks if block["block_type"] == "SUBQUERY"]
        ordered_blocks.extend(block for block in blocks if block["block_type"] != "SUBQUERY")

        try:
            payloads = self._retrieve_by_vector_search(ordered_blocks, rules)
        except Exception as exc:
            logger.warning(
                "[TobeSqlTuningService] vector search fallback to token search "
                f"(reason={type(exc).__name__}: {exc})"
            )
            payloads = [self._build_lexical_match_payload(block, rules) for block in ordered_blocks]
        return payloads

    def load_universal_tuning_rules(
        self,
        sql_text: str | None = None,
        source_tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.load_universal_rules(
            category=SQL_TUNING,
            source_tables=source_tables or set(),
        )

    def load_universal_conversion_rules(
        self,
        sql_text: str,
        source_tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self.load_universal_rules(
            category=SQL_CONVERSION,
            source_tables=self._normalize_table_set(source_tables),
        )

    def load_universal_rules(
        self,
        category: str,
        source_tables: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return self._load_general_rules(
                category=category,
                source_tables=self._normalize_table_set(source_tables),
            )
        except Exception as exc:
            logger.warning(
                f"[TobeSqlTuningService] DB GENERAL rule load failed; skip universal rules "
                f"({type(exc).__name__}: {exc})"
            )
            return []

    def parse_source_tables(self, value: str | None) -> set[str]:
        return self._parse_source_tables(value)

    def increment_rule_hit_counts_for_success(self, tuning_examples: list[dict[str, Any]]) -> None:
        unique_rule_ids = sorted(set(self._extract_prompt_rule_ids(tuning_examples)))
        self.increment_rule_hit_counts(unique_rule_ids, expected_rule_type=RULE_SEARCH)

    def increment_rule_hit_counts(
        self,
        rule_ids: list[str],
        expected_rule_type: str | None = None,
    ) -> None:
        clean_rule_ids = [str(rule_id).strip() for rule_id in rule_ids if str(rule_id or "").strip()]
        if not clean_rule_ids:
            return

        try:
            from server.services.sql.db_runtime import get_connection, qualify_table_name

            table = qualify_table_name(self.table_name)
            with get_connection() as conn:
                cur = conn.cursor()
                type_filter = ""
                if expected_rule_type:
                    type_filter = " AND UPPER(TRIM(RULE_TYPE)) = :rule_type"
                update_sql = f"""
                    UPDATE {table}
                    SET HIT_CNT = NVL(HIT_CNT, 0) + 1,
                        UPDATED_AT = SYSTIMESTAMP
                    WHERE TO_CHAR(RAG_ID) = :rule_id
                      AND UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                      {type_filter}
                """
                if expected_rule_type:
                    binds = [{"rule_id": rule_id, "rule_type": expected_rule_type} for rule_id in clean_rule_ids]
                else:
                    binds = [{"rule_id": rule_id} for rule_id in clean_rule_ids]
                cur.executemany(update_sql, binds)
                conn.commit()
            logger.info(
                f"[TobeSqlTuningService] RAG HIT_CNT incremented "
                f"(rule_type={expected_rule_type or 'ANY'}, hits={len(clean_rule_ids)})"
            )
        except Exception as exc:
            logger.warning(
                f"[TobeSqlTuningService] failed to increment RAG HIT_CNT "
                f"({type(exc).__name__}: {exc})"
            )

    def _retrieve_by_vector_search(
        self,
        blocks: list[dict[str, str]],
        rules: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not self.embed_base_url:
            raise RuntimeError("RAG_EMBED_BASE_URL is not set")

        try:
            import faiss
            import numpy as np
        except Exception as exc:
            raise RuntimeError("faiss-cpu and numpy are required for vector search") from exc

        rule_texts = [self._rule_embedding_text(rule) for rule in rules]
        block_texts = [block["normalized_sql"] for block in blocks]
        embeddings = self._embed_texts(rule_texts + block_texts)
        if len(embeddings) != len(rule_texts) + len(block_texts):
            raise RuntimeError("embedding response count does not match request count")

        rule_vectors = np.asarray(embeddings[: len(rule_texts)], dtype="float32")
        block_vectors = np.asarray(embeddings[len(rule_texts) :], dtype="float32")
        if rule_vectors.ndim != 2 or block_vectors.ndim != 2:
            raise RuntimeError("embedding vectors must be 2-dimensional")

        faiss.normalize_L2(rule_vectors)
        faiss.normalize_L2(block_vectors)
        index = faiss.IndexFlatIP(rule_vectors.shape[1])
        index.add(rule_vectors)

        safe_k = min(self.top_k, len(rules))
        scores, indices = index.search(block_vectors, safe_k)

        payloads: list[dict[str, Any]] = []
        for block_idx, block in enumerate(blocks):
            matches = []
            for score, rule_idx in zip(scores[block_idx], indices[block_idx]):
                if rule_idx < 0:
                    continue
                matches.append(self._format_rule_match(rules[int(rule_idx)], float(score)))
            payloads.append(
                {
                    "block_id": block["block_id"],
                    "block_type": block["block_type"],
                    "source_sql": block["sql"],
                    "search_method": "faiss_vector",
                    "embedding_model": self.embed_model,
                    "top_rule_matches": matches,
                }
            )
        return payloads

    def _build_lexical_match_payload(self, block: dict[str, str], rules: list[dict[str, Any]]) -> dict[str, Any]:
        scored: list[tuple[dict[str, Any], float]] = []
        for rule in rules:
            score = self._lexical_similarity(block["normalized_sql"], rule["normalized_source_sql"])
            scored.append((rule, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return {
            "block_id": block["block_id"],
            "block_type": block["block_type"],
            "source_sql": block["sql"],
            "search_method": "token_fallback",
            "top_rule_matches": [
                self._format_rule_match(rule, score)
                for rule, score in scored[: self.top_k]
            ],
        }

    @staticmethod
    def _format_rule_match(rule: dict[str, Any], score: float) -> dict[str, Any]:
        return {
            "rule_id": rule["rule_id"],
            "score": round(score, 6),
            "category": rule.get("category", ""),
            "rule_type": rule.get("rule_type", RULE_SEARCH),
            "source_tables": rule.get("source_tables", []),
            "guidance": rule["guidance"],
            "example_bad_sql": rule["example_bad_sql"],
            "example_tuned_sql": rule["example_tuned_sql"],
            "source_sql": rule["source_sql"],
            "target_sql": rule["target_sql"],
        }

    @staticmethod
    def _extract_prompt_rule_ids(payloads: list[dict[str, Any]]) -> list[str]:
        rule_ids: list[str] = []
        for payload in payloads:
            matches = payload.get("top_rule_matches", [])
            if not isinstance(matches, list):
                continue
            for match in matches:
                if not isinstance(match, dict):
                    continue
                rule_id = str(match.get("rule_id", "")).strip()
                if rule_id:
                    rule_ids.append(rule_id)
        return rule_ids

    def _load_search_rules(self, category: str, source_tables: set[str]) -> list[dict[str, Any]]:
        from server.services.sql.db_runtime import get_connection, qualify_table_name

        table = qualify_table_name(self.table_name)
        result: list[dict[str, Any]] = []
        with get_connection() as conn:
            cur = conn.cursor()
            q = f"""
                SELECT RAG_ID, CATEGORY, RULE_TYPE, SOURCE_TABLES,
                       GUIDANCE_TEXT, SOURCE_SQL, TARGET_SQL
                FROM {table}
                WHERE UPPER(TRIM(CATEGORY)) = :category
                  AND UPPER(TRIM(RULE_TYPE)) = :rule_type
                  AND UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                ORDER BY CREATED_AT ASC
            """
            cur.execute(q, {"category": self._normalize_category(category), "rule_type": RULE_SEARCH})
            for row in cur.fetchall():
                rule_id = str(row[0] or "").strip()
                rule_tables = self._parse_source_tables(self._to_text(row[3]))
                if not self._source_tables_match(rule_tables, source_tables):
                    continue
                source_sql = self._to_text(row[5]).strip()
                if not rule_id or not source_sql:
                    continue
                guidance = [
                    item.strip()
                    for item in self._to_text(row[4]).splitlines()
                    if item.strip()
                ]
                target_sql = self._to_text(row[6]).strip()
                result.append(
                    {
                        "rule_id": rule_id,
                        "category": self._normalize_category(row[1]),
                        "rule_type": self._normalize_rule_type(row[2]),
                        "source_tables": sorted(rule_tables),
                        "guidance": guidance,
                        "example_bad_sql": source_sql,
                        "example_tuned_sql": target_sql,
                        "source_sql": source_sql,
                        "target_sql": target_sql,
                        "normalized_source_sql": self._normalize_sql_shape(source_sql),
                    }
                )
        logger.info(
            f"[TobeSqlTuningService] RAG SEARCH rules loaded "
            f"(category={self._normalize_category(category)}, "
            f"source_tables={','.join(sorted(source_tables)) or 'ALL'}, count={len(result)})"
        )
        return result

    def _load_general_rules(self, category: str, source_tables: set[str]) -> list[dict[str, Any]]:
        from server.services.sql.db_runtime import get_connection, qualify_table_name

        table = qualify_table_name(self.table_name)
        result: list[dict[str, Any]] = []
        with get_connection() as conn:
            cur = conn.cursor()
            q = f"""
                SELECT RAG_ID, CATEGORY, SOURCE_TABLES, GUIDANCE_TEXT
                FROM {table}
                WHERE UPPER(TRIM(CATEGORY)) = :category
                  AND UPPER(TRIM(RULE_TYPE)) = :rule_type
                  AND UPPER(TRIM(NVL(USE_YN, 'N'))) = 'Y'
                ORDER BY CREATED_AT ASC
            """
            cur.execute(q, {"category": self._normalize_category(category), "rule_type": RULE_GENERAL})
            for row in cur.fetchall():
                rule_id = str(row[0] or "").strip()
                rule_tables = self._parse_source_tables(self._to_text(row[2]))
                if not self._source_tables_match(rule_tables, source_tables):
                    continue
                guidance_raw = self._to_text(row[3]).strip()
                if not rule_id or not guidance_raw:
                    continue
                guidance = [item.strip() for item in guidance_raw.splitlines() if item.strip()]
                result.append(
                    {
                        "rule_id": rule_id,
                        "rag_category": self._normalize_category(row[1]),
                        "source_tables": sorted(rule_tables),
                        "category": "general",
                        "priority": "mandatory",
                        "guidance": guidance,
                    }
                )
        logger.info(
            f"[TobeSqlTuningService] RAG GENERAL rules loaded "
            f"(category={self._normalize_category(category)}, "
            f"source_tables={','.join(sorted(source_tables)) or 'ALL'}, count={len(result)})"
        )
        return result

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        endpoint = self._embedding_endpoint(self.embed_base_url)
        headers = {"Content-Type": "application/json"}
        if self.embed_api_key:
            headers["Authorization"] = f"Bearer {self.embed_api_key}"
        payload = {"model": self.embed_model, "input": texts}

        response = requests.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=self.embed_timeout_sec,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"embedding HTTP {response.status_code}: {response.text[:300]}")
        vectors = self._extract_embedding_vectors(response.json())
        if not vectors:
            raise RuntimeError("embedding response did not contain vectors")
        return vectors

    @staticmethod
    def _extract_embedding_vectors(body: Any) -> list[list[float]]:
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                vectors = []
                for item in data:
                    if isinstance(item, dict) and isinstance(item.get("embedding"), list):
                        vectors.append([float(value) for value in item["embedding"]])
                if vectors:
                    return vectors

            embeddings = body.get("embeddings")
            if isinstance(embeddings, list):
                vectors = []
                for item in embeddings:
                    if isinstance(item, list):
                        vectors.append([float(value) for value in item])
                if vectors:
                    return vectors

            embedding = body.get("embedding")
            if isinstance(embedding, list):
                return [[float(value) for value in embedding]]
        return []

    @staticmethod
    def _embedding_endpoint(base_url: str) -> str:
        normalized = base_url.strip().rstrip("/")
        if normalized.endswith("/embeddings"):
            return normalized
        if normalized.endswith("/v1"):
            return f"{normalized}/embeddings"
        return f"{normalized}/v1/embeddings"

    @staticmethod
    def _rule_embedding_text(rule: dict[str, Any]) -> str:
        return "\n".join(
            [
                rule.get("normalized_source_sql", ""),
                rule.get("source_sql", ""),
            ]
        ).strip()

    def _split_sql_into_blocks(self, sql_text: str) -> list[dict[str, str]]:
        source = (sql_text or "").strip().rstrip(";").strip()
        if not source:
            return []

        replacements: list[tuple[int, int, str, str]] = []
        stack: list[int] = []
        in_quote = False
        idx = 0
        while idx < len(source):
            ch = source[idx]
            if ch == "'":
                if in_quote and idx + 1 < len(source) and source[idx + 1] == "'":
                    idx += 2
                    continue
                in_quote = not in_quote
                idx += 1
                continue
            if in_quote:
                idx += 1
                continue
            if ch == "(":
                stack.append(idx)
            elif ch == ")" and stack:
                start = stack.pop()
                inner = source[start + 1 : idx].strip()
                if re.match(r"^SELECT\b", inner, flags=re.IGNORECASE):
                    placeholder = f"SUBQUERY_{len(replacements) + 1}"
                    replacements.append((start, idx + 1, placeholder, inner))
            idx += 1

        main_sql = source
        for start, end, placeholder, _inner in sorted(replacements, key=lambda item: item[0], reverse=True):
            main_sql = main_sql[:start] + f"({placeholder})" + main_sql[end:]

        blocks = [
            {
                "block_id": "MAIN_SQL",
                "block_type": "MAIN",
                "sql": main_sql,
                "normalized_sql": self._normalize_sql_shape(main_sql),
            }
        ]
        for _start, _end, placeholder, inner in replacements:
            blocks.append(
                {
                    "block_id": placeholder,
                    "block_type": "SUBQUERY",
                    "sql": inner,
                    "normalized_sql": self._normalize_sql_shape(inner),
                }
            )
        return blocks

    def _normalize_sql_shape(self, sql_text: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", sql_text or "", flags=re.DOTALL)
        text = re.sub(r"--[^\n]*", " ", text)
        text = re.sub(r"'(?:''|[^'])*'", " STR ", text)
        text = re.sub(r"\b\d+(?:\.\d+)?\b", " NUM ", text)
        text = re.sub(r"\bSUBQUERY_\d+\b", "SUBQUERY", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text.upper()

    @staticmethod
    def _lexical_similarity(left: str, right: str) -> float:
        left_tokens = set(re.findall(r"[A-Z_]+|\d+", left.upper()))
        right_tokens = set(re.findall(r"[A-Z_]+|\d+", right.upper()))
        if not left_tokens or not right_tokens:
            return 0.0
        union = len(left_tokens.union(right_tokens))
        return len(left_tokens.intersection(right_tokens)) / union if union else 0.0

    @staticmethod
    def _to_text(value: Any, default: str = "") -> str:
        if value is None:
            return default
        if hasattr(value, "read"):
            value = value.read()
        if value is None:
            return default
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="ignore")
        return str(value)

    @staticmethod
    def _normalize_category(value: Any) -> str:
        return str(value or "").strip().upper()

    @staticmethod
    def _normalize_rule_type(value: Any) -> str:
        return str(value or RULE_SEARCH).strip().upper()

    @classmethod
    def _normalize_table_set(cls, values: set[str] | list[str] | tuple[str, ...] | None) -> set[str]:
        return {table for value in (values or []) if (table := cls._normalize_table_name(str(value)))}

    @staticmethod
    def _normalize_table_name(value: str | None) -> str:
        text = (value or "").strip().strip('"').strip("'")
        if not text:
            return ""
        if "." in text:
            text = text.split(".")[-1]
        return text.strip().strip('"').strip("'").upper()

    @classmethod
    def _parse_source_tables(cls, value: str | None) -> set[str]:
        text = (value or "").strip()
        if not text:
            return set()
        return {
            normalized
            for token in re.split(r"[,;\s]+", text)
            if (normalized := cls._normalize_table_name(token))
        }

    @staticmethod
    def _source_tables_match(rule_tables: set[str], job_tables: set[str]) -> bool:
        if not rule_tables:
            return True
        if not job_tables:
            return False
        return bool(rule_tables & job_tables)


tobe_sql_tuning_service = TobeSqlTuningService()
