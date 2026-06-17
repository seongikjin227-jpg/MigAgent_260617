"""Shared NEXT_SQL_INFO status values and compatibility helpers."""

CONVERSION_PASS = "CONVERSION-PASS"
TUNING_PASS = "TUNING-PASS"
PASS_NON_SELECT = "PASS_NON_SELECT"

FAIL_TOBE = "FAIL-TOBE"
FAIL_TUNED = "FAIL-TUNED"
FAIL_BIND = "FAIL-BIND"
FAIL_TEST = "FAIL-TEST"
CONVERSION_FAIL_STATUSES = (FAIL_TOBE, FAIL_BIND, FAIL_TEST)
TUNING_FAIL_STATUSES = (FAIL_TUNED, FAIL_BIND, FAIL_TEST)
FAIL_STATUSES = (FAIL_TOBE, FAIL_TUNED, FAIL_BIND, FAIL_TEST)

LEGACY_PASS = "PASS"
LEGACY_FAIL = "FAIL"
TUNING_PASS_ALIASES = (TUNING_PASS, "TUNING_PASS")

CONVERSION_SUCCESS_STATUSES = (CONVERSION_PASS, LEGACY_PASS)
TUNING_SUCCESS_STATUSES = (TUNING_PASS, "TUNING_PASS", LEGACY_PASS, PASS_NON_SELECT)


def normalize_status(value: str | None) -> str:
    return str(value or "").strip().upper()


def is_conversion_pass(value: str | None) -> bool:
    return normalize_status(value) in {status.upper() for status in CONVERSION_SUCCESS_STATUSES}


def is_tuning_pass(value: str | None) -> bool:
    return normalize_status(value) in {status.upper() for status in TUNING_SUCCESS_STATUSES}


def is_fail(value: str | None) -> bool:
    normalized = normalize_status(value)
    return normalized == LEGACY_FAIL or normalized.startswith("FAIL")


def sql_in(values: tuple[str, ...] | list[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)
