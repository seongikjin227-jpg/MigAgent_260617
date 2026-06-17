"""Bind parameter extraction and bind-set construction helpers."""

import json
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any


_BIND_TOKEN_PATTERN = re.compile(r"[#$]\{\s*([^}]+?)\s*\}")
_FOREACH_BLOCK_PATTERN = re.compile(
    r"""<foreach\b([^>]*)>.*?</\s*foreach\s*>""",
    re.IGNORECASE | re.DOTALL,
)
_FOREACH_COLLECTION_PATTERN = re.compile(
    r"""\bcollection\s*=\s*['"]([^'"]+)['"]""",
    re.IGNORECASE | re.DOTALL,
)
_DYNAMIC_TEST_PATTERN = re.compile(
    r"""<(?:if|when)\b[^>]*\btest\s*=\s*['"]([^'"]+)['"][^>]*>""",
    re.IGNORECASE | re.DOTALL,
)
_IDENTIFIER_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_\.]*)\b")
_RESERVED_WORDS = {
    "and",
    "or",
    "not",
    "null",
    "true",
    "false",
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "empty",
    "instanceof",
    "new",
    "in",
}


def _normalize_param_name(token: str) -> str:
    """Normalize MyBatis expressions like #{dto.id} to the final bind name id."""
    cleaned = (token or "").strip()
    if not cleaned:
        return ""
    for splitter in [",", " ", "?", ":", "=", "!", ">", "<", "+", "-", "*", "/", ")", "(", "["]:
        if splitter in cleaned:
            cleaned = cleaned.split(splitter)[0]
    return cleaned.strip().split(".")[-1]


def _append_unique(names: list[str], seen: set[str], token: str) -> None:
    normalized = _normalize_param_name(token)
    if normalized and normalized not in seen:
        names.append(normalized)
        seen.add(normalized)


def _extract_test_param_names(test_expr: str) -> list[str]:
    """Extract parameter-like names from MyBatis test expressions."""
    condition = re.sub(r"'[^']*'|\"[^\"]*\"", " ", test_expr or "")
    names: list[str] = []
    seen: set[str] = set()
    for ident in _IDENTIFIER_PATTERN.findall(condition):
        lowered = ident.lower()
        if lowered in _RESERVED_WORDS or ident.isdigit():
            continue
        _append_unique(names, seen, ident)
    return names


def extract_bind_param_names(sql_text: str) -> list[str]:
    """Extract bind parameters from placeholders and MyBatis dynamic tags.

    This is only used to decide whether the bind SQL stage is needed. Bind-set
    keys still come from the executed bind SQL aliases.
    """
    if not sql_text:
        return []

    names: list[str] = []
    seen: set[str] = set()

    sql_without_foreach = sql_text
    for match in _FOREACH_BLOCK_PATTERN.finditer(sql_text):
        attrs = match.group(1) or ""
        collection_match = _FOREACH_COLLECTION_PATTERN.search(attrs)
        if collection_match:
            _append_unique(names, seen, collection_match.group(1))
        sql_without_foreach = sql_without_foreach.replace(match.group(0), " ")

    for match in _BIND_TOKEN_PATTERN.finditer(sql_without_foreach):
        _append_unique(names, seen, match.group(1))

    for match in _DYNAMIC_TEST_PATTERN.finditer(sql_without_foreach):
        for name in _extract_test_param_names(match.group(1)):
            _append_unique(names, seen, name)

    return names


def _normalize_row_key(key: Any) -> str:
    """Normalize DB result column names for bind-set JSON keys."""
    return str(key).strip().strip('"')


def _build_bind_case(row: dict[str, Any]) -> dict[str, Any]:
    """Use bind SQL result aliases as bind-set keys."""
    return {
        normalized_key: value
        for key, value in row.items()
        if (normalized_key := _normalize_row_key(key))
    }


def _value_signature(bind_case: dict[str, Any]) -> tuple:
    """Compute a stable value signature for duplicate removal."""
    return tuple((key, bind_case.get(key)) for key in sorted(bind_case.keys()))


def _is_no_bind_marker(bind_case: dict[str, Any]) -> bool:
    """Detect the explicit no-bind marker produced by the bind SQL prompt."""
    return set(bind_case.keys()) == {"NO_BIND"}


def build_bind_sets(
    bind_query_rows: list[dict[str, Any]],
    max_cases: int = 3,
) -> list[dict[str, Any]]:
    """Build up to three bind cases from bind SQL result rows.

    Parameter values and key names come from the executed bind SQL aliases.
    """
    safe_max = max(1, min(max_cases, 3))
    selected: list[dict[str, Any]] = []
    seen_signatures = set()

    for row in bind_query_rows:
        bind_case = _build_bind_case(row)
        if not bind_case:
            continue
        if _is_no_bind_marker(bind_case):
            return [{}]
        signature = _value_signature(bind_case)
        if signature in seen_signatures:
            continue
        selected.append(bind_case)
        seen_signatures.add(signature)
        if len(selected) >= safe_max:
            break

    return selected or [{}]


def bind_sets_to_json(bind_sets: list[dict[str, Any]]) -> str:
    """Serialize bind sets for prompts and DB storage."""
    return json.dumps(bind_sets, ensure_ascii=False, default=_json_default)


def _json_default(value: Any):
    """Convert non-JSON values to stable scalar representations."""
    if value is not None and hasattr(value, "read"):
        value = value.read()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)
