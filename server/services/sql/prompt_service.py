import json
import re
from pathlib import Path
from typing import Any


# config/prompts/ (project_root 기준)
# agents/sql_pipeline/services/ → services → sql_pipeline → agents → root
PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "prompts"


def load_prompt_template(filename: str) -> dict[str, Any]:
    prompt_path = PROMPTS_DIR / filename
    return json.loads(prompt_path.read_text(encoding="utf-8-sig"))


def render_prompt_template(filename: str, **kwargs) -> dict[str, Any]:
    template = load_prompt_template(filename)
    return _render_value(template, kwargs)


def build_prompt_messages(filename: str, **kwargs) -> list[dict[str, str]]:
    payload = render_prompt_template(filename, **kwargs)
    user_instruction = payload.pop(
        "user_instruction",
        "Generate one executable Oracle SQL statement only. Do not end the SQL with a semicolon.",
    )
    return [
        {"role": "system", "content": _render_message_content(payload)},
        {"role": "user", "content": str(user_instruction)},
    ]


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _render_value(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        return {key: _render_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_value(item, context) for item in value]
    if isinstance(value, str):
        return value.format_map(_SafeFormatDict(context))
    return value


def _render_message_content(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("name", "role", "objective"):
        value = payload.get(key)
        if value:
            lines.append(f"{key}: {value}")

    inputs = payload.get("inputs")
    if isinstance(inputs, dict) and inputs:
        lines.append("")
        lines.append("inputs:")
        for input_name, input_value in inputs.items():
            lines.extend(_render_input_block(input_name, input_value))

    rules = payload.get("rules")
    if isinstance(rules, list) and rules:
        lines.append("")
        lines.append("rules:")
        for rule in rules:
            lines.append(f"- {rule}")

    remaining = {
        key: value
        for key, value in payload.items()
        if key not in {"name", "role", "objective", "inputs", "rules"}
    }
    if remaining:
        lines.append("")
        lines.append("metadata:")
        lines.append(json.dumps(remaining, ensure_ascii=False, indent=2))

    return "\n".join(lines).strip()


def _render_input_block(name: str, value: Any) -> list[str]:
    text, block_type = _format_input_value(name, value)
    return [
        f"{name}:",
        f"```{block_type}",
        text,
        "```",
    ]


def _format_input_value(name: str, value: Any) -> tuple[str, str]:
    if _is_json_input_name(name):
        parsed = _parse_json_input(value)
        if parsed is not None:
            return _render_structured_text(parsed), "text"
        return _stringify_input_value(value), "text"

    text = _stringify_input_value(value)
    return text, _detect_block_type(name, text)


def _is_json_input_name(name: str) -> bool:
    lowered_name = name.lower()
    return lowered_name.endswith("_json") or lowered_name in {
        "universal_tuning_rules",
        "searched_tuning_rule_block_rag_json",
    }


def _parse_json_input(value: Any) -> Any | None:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return None


def _render_structured_text(value: Any, level: int = 0) -> str:
    lines = _render_structured_lines(value, level=level)
    return "\n".join(lines).strip()


def _render_structured_lines(value: Any, level: int = 0) -> list[str]:
    indent = "    " * level
    if isinstance(value, dict):
        if not value:
            return [f"{indent}{{}}"]
        lines: list[str] = []
        for key, item in value.items():
            if _is_simple_structured_value(item):
                lines.append(f"{indent}{key}: {_format_simple_structured_value(item)}")
            else:
                lines.append(f"{indent}{key}:")
                lines.extend(_render_structured_lines(item, level + 1))
        return lines

    if isinstance(value, list):
        if not value:
            return [f"{indent}[]"]
        lines = []
        for item in value:
            if _is_simple_structured_value(item):
                lines.append(f"{indent}- {_format_simple_structured_value(item)}")
            else:
                lines.append(f"{indent}-")
                lines.extend(_render_structured_lines(item, level + 1))
        return lines

    text = _normalize_block_text(str(value))
    if "\n" not in text:
        return [f"{indent}{text}"]
    return [f"{indent}{line}" if line else "" for line in text.splitlines()]


def _is_simple_structured_value(value: Any) -> bool:
    if isinstance(value, (dict, list)):
        return False
    if isinstance(value, str):
        return "\n" not in _normalize_block_text(value)
    return True


def _format_simple_structured_value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return _normalize_block_text(str(value))


def _stringify_input_value(value: Any) -> str:
    if isinstance(value, str):
        return _normalize_block_text(value)
    return _normalize_block_text(json.dumps(value, ensure_ascii=False, indent=2))


def _normalize_block_text(text: str) -> str:
    normalized = (
        text.replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\t", "    ")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", "    ")
    )
    lines = [line.rstrip() for line in normalized.split("\n")]

    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    compacted: list[str] = []
    previous_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and previous_blank:
            continue
        compacted.append(line)
        previous_blank = is_blank

    return "\n".join(compacted)


def _detect_block_type(name: str, value: str) -> str:
    lowered_name = name.lower()
    if _is_json_input_name(lowered_name):
        return "json"
    if "sql" in lowered_name or re.search(r"\bSELECT\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bMERGE\b", value, re.IGNORECASE):
        return "sql"
    return "text"
