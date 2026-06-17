import re

def split_sql_script(script: str) -> list[str]:
    if not script:
        return []

    parts = re.split(r'^\s*/\s*$', script, flags=re.MULTILINE)

    statements = []
    for part in parts:
        clean_part = part.strip()
        if not clean_part:
            continue

        statements.append(clean_part)

    return statements

def clean_sql_statement(stmt: str) -> str:
    if not stmt:
        return ""

    cleaned = stmt.strip()
    cleaned = re.sub(r'[;/]\s*$', '', cleaned)
    return cleaned.strip()
