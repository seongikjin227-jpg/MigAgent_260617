"""Runtime LLM model fallback state."""

from __future__ import annotations

import os
import threading

DEFAULT_LLM_FALLBACK_MODELS = "GLM-5.1,Qwen3.6-35B-A3B,Kimi-K2.5"

_lock = threading.Lock()
_active_model: str | None = None


def model_candidates(primary_model: str) -> list[str]:
    configured = os.getenv("LLM_FALLBACK_MODELS", DEFAULT_LLM_FALLBACK_MODELS)
    configured_models = [model.strip() for model in configured.split(",") if model.strip()]

    with _lock:
        active_model = _active_model

    candidates: list[str] = []
    if active_model:
        candidates.append(active_model)
    if primary_model.strip():
        candidates.append(primary_model.strip())
    candidates.extend(configured_models)

    deduped: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        key = model.lower()
        if key not in seen:
            deduped.append(model)
            seen.add(key)
    return deduped


def set_active_model(model: str) -> None:
    model = (model or "").strip()
    if not model:
        return
    with _lock:
        global _active_model
        _active_model = model


def get_active_model() -> str | None:
    with _lock:
        return _active_model


def reset_active_model() -> str | None:
    with _lock:
        global _active_model
        previous_model = _active_model
        _active_model = None
        return previous_model


def is_model_fallback_error(message: str) -> bool:
    text = (message or "").lower()
    api_key_rate_limit_patterns = (
        "rate limit exceed for api_key",
        "rate limit exceeded for api_key",
        "rate_limit_exceed_for_api_key",
        "rate_limit_exceeded_for_api_key",
    )
    if ("429" in text or "rate limit" in text) and any(
        pattern in text for pattern in api_key_rate_limit_patterns
    ):
        return True

    server_error_patterns = (
        "error code: 500",
        "error code : 500",
        "status code: 500",
        "status code : 500",
        "internal server error",
        "server error",
        "http 500",
        " 500 ",
    )
    if any(pattern in text for pattern in server_error_patterns):
        return True

    fatal_patterns = (
        "model not allow",
        "model_not_allow",
        "model not allowed",
        "model_not_allowed",
        "not allowed to access model",
        "team not allowed",
        "team_not_allowed",
        "model not found",
        "model_not_found",
        "model does not exist",
        "does not exist",
        "not supported",
        "unsupported model",
        "permission",
        "not authorized",
        "access denied",
        "forbidden",
    )
    transient_patterns = (
        "timed out",
        "timeout",
        "rate limit",
        "429",
        "gateway timeout",
        "504",
        "connection reset",
        "temporarily unavailable",
    )
    return any(pattern in text for pattern in fatal_patterns) and not any(
        pattern in text for pattern in transient_patterns
    )
