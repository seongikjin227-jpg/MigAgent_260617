"""SQL formatting helpers for readable DB storage.

The functions in this module are intentionally not wired into the persistence
path yet. Use them when debugging or when explicitly enabling formatted SQL
storage later.
"""

from __future__ import annotations

import re


_CLAUSE_PATTERNS = (
    r"UNION\s+ALL",
    r"UNION",
    r"SELECT",
    r"FROM",
    r"WHERE",
    r"GROUP\s+BY",
    r"HAVING",
    r"ORDER\s+BY",
    r"LEFT\s+OUTER\s+JOIN",
    r"RIGHT\s+OUTER\s+JOIN",
    r"FULL\s+OUTER\s+JOIN",
    r"LEFT\s+JOIN",
    r"RIGHT\s+JOIN",
    r"INNER\s+JOIN",
    r"OUTER\s+JOIN",
    r"JOIN",
    r"ON",
    r"AND",
    r"OR",
    r"CASE",
    r"WHEN",
    r"THEN",
    r"ELSE",
    r"END",
)


def format_sql_for_storage(sql_text: str | None, indent_size: int = 4) -> str:
    """Return a readable SQL string without changing execution intent."""
    text = _to_text(sql_text).strip()
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n").strip().rstrip(";").strip()
    text = _collapse_horizontal_whitespace(text)
    text = _break_before_clauses(text)
    text = _break_top_level_commas(text)
    return _indent_sql_lines(text, indent_size=indent_size)


def format_sql_fields_for_storage(
    *,
    tobe_sql: str | None = None,
    tuned_sql: str | None = None,
    bind_sql: str | None = None,
    test_sql: str | None = None,
) -> dict[str, str | None]:
    """Format SQL result fields using the shared storage guide."""
    return {
        "TO_SQL_TEXT": format_sql_for_storage(tobe_sql),
        "TUNED_SQL": format_sql_for_storage(tuned_sql) if tuned_sql is not None else None,
        "BIND_SQL": format_sql_for_storage(bind_sql),
        "TEST_SQL": format_sql_for_storage(test_sql),
    }


def _to_text(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "read"):
        value = value.read()
    if value is None:
        return ""
    return str(value)


def _collapse_horizontal_whitespace(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return " ".join(line for line in lines if line)


def _break_before_clauses(text: str) -> str:
    formatted = text
    for pattern in _CLAUSE_PATTERNS:
        formatted = re.sub(
            rf"\s+({pattern})\b",
            lambda match: "\n" + match.group(1).upper(),
            formatted,
            flags=re.IGNORECASE,
        )
    return formatted.strip()


def _break_top_level_commas(text: str) -> str:
    result: list[str] = []
    depth = 0
    in_single_quote = False
    in_xml_tag = False
    xml_attr_quote = ""
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if in_xml_tag:
            result.append(ch)
            if xml_attr_quote:
                if ch == xml_attr_quote:
                    xml_attr_quote = ""
                idx += 1
                continue
            if ch in ("'", '"'):
                xml_attr_quote = ch
            elif ch == ">":
                in_xml_tag = False
            idx += 1
            continue

        if in_single_quote:
            result.append(ch)
            if ch == "'":
                if idx + 1 < len(text) and text[idx + 1] == "'":
                    result.append(text[idx + 1])
                    idx += 2
                    continue
                in_single_quote = False
            idx += 1
            continue

        if ch == "'":
            in_single_quote = True
            result.append(ch)
        elif ch == "<" and re.match(r"</?\s*[A-Za-z][A-Za-z0-9:_-]*(?:\s|/?>)", text[idx:]):
            in_xml_tag = True
            result.append(ch)
        elif ch == "(":
            depth += 1
            result.append(ch)
        elif ch == ")":
            depth = max(0, depth - 1)
            result.append(ch)
        elif ch == "," and depth == 0:
            result.append(",\n")
            while idx + 1 < len(text) and text[idx + 1] == " ":
                idx += 1
        else:
            result.append(ch)
        idx += 1
    return "".join(result)


def _indent_sql_lines(text: str, indent_size: int) -> str:
    indent = " " * indent_size
    lines: list[str] = []
    depth = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        depth = max(0, depth - _leading_close_parens(line))
        prefix = indent * depth
        lines.append(prefix + line)
        depth += _paren_delta(line)
    return "\n".join(lines)


def _leading_close_parens(line: str) -> int:
    return len(line) - len(line.lstrip(")"))


def _paren_delta(line: str) -> int:
    delta = 0
    in_single_quote = False
    for idx, ch in enumerate(line):
        if in_single_quote:
            if ch == "'":
                if idx + 1 < len(line) and line[idx + 1] == "'":
                    continue
                in_single_quote = False
            continue
        if ch == "'":
            in_single_quote = True
        elif ch == "(":
            delta += 1
        elif ch == ")":
            delta -= 1
    return delta
