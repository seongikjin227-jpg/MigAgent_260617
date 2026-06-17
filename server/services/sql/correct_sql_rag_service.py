from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from server.core.logger import logger
from server.repositories.sql.result_repository import get_feedback_corpus_rows


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class CorrectSqlHintRagService:
    """Retrieve similar FR SQL rows with human-corrected SQL hints."""

    def __init__(self) -> None:
        self.top_k = max(1, int(os.getenv("CORRECT_SQL_HINT_TOP_K", "2")))
        self.corpus_limit = max(1, int(os.getenv("CORRECT_SQL_HINT_CORPUS_LIMIT", "2000")))
        self.embed_base_url = os.getenv("RAG_EMBED_BASE_URL", "").strip()
        self.embed_api_key = os.getenv("RAG_EMBED_API_KEY", "").strip()
        self.embed_model = os.getenv("RAG_EMBED_MODEL", "BAAI/bge-m3").strip()
        self.embed_timeout_sec = int(os.getenv("RAG_EMBED_TIMEOUT_SEC", "30"))

    def retrieve_correct_sql_hints(
        self,
        sql_text: str,
        correct_kind: str,
        current_row_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query_sql = (sql_text or "").strip()
        if not query_sql:
            return []

        kind = (correct_kind or "").strip().upper()
        try:
            rows = get_feedback_corpus_rows(kind, limit=self.corpus_limit)
        except Exception as exc:
            logger.warning(
                f"[CorrectSqlHintRagService] failed to load {kind} corpus "
                f"({type(exc).__name__}: {exc})"
            )
            return []

        candidates = [row for row in rows if self._is_search_candidate(row, current_row_id)]
        if not candidates:
            return []

        try:
            hints = self._retrieve_by_vector_search(query_sql, candidates)
        except Exception as exc:
            logger.warning(
                "[CorrectSqlHintRagService] vector search fallback to token search "
                f"(kind={kind}, reason={type(exc).__name__}: {exc})"
            )
            hints = self._retrieve_by_lexical_search(query_sql, candidates)

        logger.info(
            f"[CorrectSqlHintRagService] correct SQL hints retrieved "
            f"(kind={kind}, candidates={len(candidates)}, hints={len(hints)})"
        )
        return hints

    @staticmethod
    def _is_search_candidate(row: dict[str, str], current_row_id: str | None) -> bool:
        if not (row.get("correct_sql") or "").strip():
            return False
        return bool(CorrectSqlHintRagService._effective_fr_sql(row))

    @staticmethod
    def _effective_fr_sql(row: dict[str, str]) -> str:
        edit_sql = (row.get("edit_fr_sql") or "").strip()
        return edit_sql if edit_sql else (row.get("fr_sql_text") or "").strip()

    def _retrieve_by_vector_search(
        self,
        query_sql: str,
        candidates: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        if not self.embed_base_url:
            raise RuntimeError("RAG_EMBED_BASE_URL is not set")

        try:
            import faiss
            import numpy as np
        except Exception as exc:
            raise RuntimeError("faiss-cpu and numpy are required for vector search") from exc

        candidate_texts = [self._normalize_sql_shape(self._effective_fr_sql(row)) for row in candidates]
        query_text = self._normalize_sql_shape(query_sql)
        embeddings = self._embed_texts(candidate_texts + [query_text])
        if len(embeddings) != len(candidate_texts) + 1:
            raise RuntimeError("embedding response count does not match request count")

        candidate_vectors = np.asarray(embeddings[: len(candidate_texts)], dtype="float32")
        query_vector = np.asarray(embeddings[len(candidate_texts) :], dtype="float32")
        if candidate_vectors.ndim != 2 or query_vector.ndim != 2:
            raise RuntimeError("embedding vectors must be 2-dimensional")

        faiss.normalize_L2(candidate_vectors)
        faiss.normalize_L2(query_vector)
        index = faiss.IndexFlatIP(candidate_vectors.shape[1])
        index.add(candidate_vectors)

        safe_k = min(self.top_k, len(candidates))
        scores, indices = index.search(query_vector, safe_k)
        hints: list[dict[str, Any]] = []
        for score, candidate_idx in zip(scores[0], indices[0]):
            if candidate_idx < 0:
                continue
            hints.append(
                self._format_hint(
                    candidates[int(candidate_idx)],
                    score=float(score),
                    search_method="faiss_vector",
                    embedding_model=self.embed_model,
                )
            )
        return hints

    def _retrieve_by_lexical_search(
        self,
        query_sql: str,
        candidates: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        normalized_query = self._normalize_sql_shape(query_sql)
        scored = [
            (row, self._lexical_similarity(normalized_query, self._normalize_sql_shape(self._effective_fr_sql(row))))
            for row in candidates
        ]
        scored.sort(key=lambda item: item[1], reverse=True)
        return [
            self._format_hint(row, score=score, search_method="token_fallback", embedding_model="")
            for row, score in scored[: self.top_k]
        ]

    @staticmethod
    def _format_hint(
        row: dict[str, str],
        score: float,
        search_method: str,
        embedding_model: str,
    ) -> dict[str, Any]:
        return {
            "row_id": row.get("row_id", ""),
            "space_nm": row.get("space_nm", ""),
            "sql_id": row.get("sql_id", ""),
            "score": round(float(score), 6),
            "search_method": search_method,
            "embedding_model": embedding_model,
            "from_sql": CorrectSqlHintRagService._effective_fr_sql(row),
            "to_sql_text": row.get("to_sql_text", ""),
            "correct_sql": row.get("correct_sql", ""),
        }

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
                    if isinstance(item, dict) and isinstance(item.get("embedding", None), list):
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
    def _normalize_sql_shape(sql_text: str) -> str:
        text = re.sub(r"/\*.*?\*/", " ", sql_text or "", flags=re.DOTALL)
        text = re.sub(r"--[^\n]*", " ", text)
        text = re.sub(r"'(?:''|[^'])*'", " STR ", text)
        text = re.sub(r"\b\d+(?:\.\d+)?\b", " NUM ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text.upper()

    @staticmethod
    def _lexical_similarity(left: str, right: str) -> float:
        left_tokens = set(re.findall(r"[A-Z_][A-Z0-9_]*", left or ""))
        right_tokens = set(re.findall(r"[A-Z_][A-Z0-9_]*", right or ""))
        if not left_tokens or not right_tokens:
            return 0.0
        return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


correct_sql_hint_rag_service = CorrectSqlHintRagService()
