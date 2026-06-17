"""flush_cycle_metrics, request_wait 도구: 사이클 완료 처리 및 대기."""

from __future__ import annotations

import time
import os

from langchain_core.tools import tool

from server.tools.context import (
    PAUSE_FLAG,
    WAKE_FLAG,
    _stop_event,
    callbacks,
    finish_cycle_metrics,
)

_WAIT_STEP = 0.2
_PAUSE_STEP = 0.5


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


_MAX_WAIT_SECONDS = _env_int("SUPERVISOR_MAX_WAIT_SECONDS", 300)


@tool
def flush_cycle_metrics() -> str:
    """현재 사이클의 실행 결과를 DB(AG_AGENT_RUN_METRICS)에 저장합니다.
    작업 실행 여부와 관계없이 사이클 완료 시 반드시 호출해야 합니다."""
    logger = callbacks.get("logger")
    finish_cycle_metrics(logger=logger)
    return "사이클 메트릭이 저장되었습니다."


@tool
def request_wait(seconds: int) -> str:
    """다음 사이클 시작 전 지정한 시간(초) 동안 대기합니다.
    작업이 있었을 때는 5, 없었을 때는 30, 오류가 많을 때는 10을 권장합니다.
    반드시 사이클의 마지막 도구로 호출하세요."""
    logger = callbacks.get("logger")
    seconds = max(1, min(int(seconds), _MAX_WAIT_SECONDS))

    # pause 파일 감지 — 파일이 있는 동안 대기
    paused_logged = False
    while PAUSE_FLAG.exists():
        if _stop_event.is_set():
            return "정지 신호 수신. 일시정지 대기 중단."
        if not paused_logged:
            if logger:
                logger.info("[request_wait] 일시정지 중... (runtime/agent.pause 감지)")
            paused_logged = True
        time.sleep(_PAUSE_STEP)
    if paused_logged and logger:
        logger.info("[request_wait] 일시정지 해제, 재개합니다.")

    elapsed = 0.0
    while elapsed < seconds:
        if _stop_event.is_set():
            return f"정지 신호 수신. {elapsed:.1f}초 대기 후 중단."
        if PAUSE_FLAG.exists():
            break
        if WAKE_FLAG.exists():
            WAKE_FLAG.unlink(missing_ok=True)
            if logger:
                logger.info("[request_wait] 즉시 실행 신호 수신. 대기 중단.")
            return "즉시 실행 신호 수신. 다음 사이클을 바로 시작합니다."
        time.sleep(_WAIT_STEP)
        elapsed += _WAIT_STEP

    return f"{seconds}초 대기 완료."
