"""도구(Tool) 실행에 필요한 공유 상태 및 콜백 저장소."""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from server.core.llm_fallback import reset_active_model
from server.repositories.supervisor.metrics_repository import (
    build_metric_row,
    insert_agent_run_metrics,
)

# ── 정지 / 일시정지 제어 ────────────────────────────────────────────────────────
_stop_event = threading.Event()
PAUSE_FLAG = Path(__file__).resolve().parent.parent.parent / "runtime" / "agent.pause"
WAKE_FLAG  = Path(__file__).resolve().parent.parent.parent / "runtime" / "agent.wake"


def is_stop_requested() -> bool:
    """SIGINT/SIGTERM 수신 여부를 반환합니다."""
    return _stop_event.is_set()


def request_stop() -> None:
    """외부에서 정지 신호를 보냅니다 (SIGINT 핸들러에서 호출)."""
    _stop_event.set()


# ── 공유 상태 (Supervisor의 poll_node가 매 사이클마다 갱신) ────────────────────
mig_registry: dict = {}
sql_registry: dict = {}
tuning_registry: dict = {}
formatting_registry: dict = {}

# ── 실행 콜백 (에이전트 초기화 시 주입) ─────────────────────────────────────────
callbacks: dict = {}

# ── 현재 Supervisor cycle의 agent별 처리량/소요시간 누적 ───────────────────────
cycle_metrics: dict = {}
batch_metrics: dict = {}

def init_callbacks(**kwargs):
    """에이전트 인스턴스와 로거 등의 콜백을 등록합니다."""
    for key, val in kwargs.items():
        callbacks[key] = val

def get_registries():
    """레지스트리 참조를 반환합니다."""
    return mig_registry, sql_registry, tuning_registry, formatting_registry


def refresh_jobs_after_tool() -> None:
    """Refresh job registries after a tool finishes one job."""
    refresh_jobs = callbacks.get("refresh_jobs")
    if refresh_jobs:
        refresh_jobs()


def start_batch_metrics(batch_no: int) -> None:
    """Supervisor 프로세스 실행 1회를 식별하는 batch 번호를 등록합니다."""
    batch_metrics.clear()
    batch_metrics["batch_no"] = batch_no


def start_cycle_metrics(cycle_no: int) -> None:
    """현재 cycle의 처리 시간 집계를 시작합니다."""
    reset_active_model()
    cycle_metrics.clear()
    cycle_metrics.update(
        {
            "cycle_no": cycle_no,
            "batch_no": batch_metrics.get("batch_no", 0),
            "started_at": datetime.now(),
            "start_counter": time.perf_counter(),
            "agents": {},
            "flushed": False,
        }
    )


def get_current_metric_context() -> dict[str, int | None]:
    """Return current supervisor batch/cycle numbers for detailed logs."""
    if not cycle_metrics:
        return {"batch_no": None, "cycle_no": None}
    return {
        "batch_no": int(cycle_metrics.get("batch_no") or 0),
        "cycle_no": int(cycle_metrics.get("cycle_no") or 0),
    }


def record_agent_run(agent_name: str, elapsed_seconds: float, status: str) -> None:
    """agent tool 실행 1건의 결과를 현재 cycle 집계에 누적합니다."""
    if not cycle_metrics:
        return
    agents = cycle_metrics.setdefault("agents", {})
    metric = agents.setdefault(
        agent_name,
        {
            "job_count": 0,
            "success_count": 0,
            "fail_count": 0,
            "skip_count": 0,
            "elapsed_seconds": 0.0,
        },
    )
    normalized_status = (status or "").strip().upper()
    metric["job_count"] += 1
    metric["elapsed_seconds"] += max(0.0, elapsed_seconds)
    if normalized_status in ("SKIP", "NA", "WAITING", "PENDING"):
        metric["skip_count"] += 1
    elif normalized_status in ("SUCCESS", "PASS", "CONVERSION-PASS", "TUNING-PASS", "TUNING_PASS", "PASS_NON_SELECT"):
        metric["success_count"] += 1
    else:
        metric["fail_count"] += 1


def finish_cycle_metrics(logger=None) -> None:
    """현재 cycle 집계를 AG_AGENT_RUN_METRICS에 저장합니다."""
    if not cycle_metrics or cycle_metrics.get("flushed"):
        return

    cycle_metrics["flushed"] = True
    agents = cycle_metrics.get("agents") or {}
    total_jobs = sum(metric["job_count"] for metric in agents.values())
    if total_jobs <= 0:
        cycle_metrics.clear()
        return

    finished_at = datetime.now()
    cycle_no = int(cycle_metrics["cycle_no"])
    batch_no = int(cycle_metrics.get("batch_no") or 0)
    started_at = cycle_metrics["started_at"]
    total_elapsed = time.perf_counter() - float(cycle_metrics["start_counter"])

    rows = [
        build_metric_row(
            batch_no=batch_no,
            cycle_no=cycle_no,
            agent_name="SUPERVISOR_CYCLE",
            job_count=total_jobs,
            success_count=sum(metric["success_count"] for metric in agents.values()),
            fail_count=sum(metric["fail_count"] for metric in agents.values()),
            skip_count=sum(metric["skip_count"] for metric in agents.values()),
            started_at=started_at,
            finished_at=finished_at,
            elapsed_seconds=total_elapsed,
        )
    ]
    for agent_name, metric in agents.items():
        rows.append(
            build_metric_row(
                batch_no=batch_no,
                cycle_no=cycle_no,
                agent_name=agent_name,
                job_count=metric["job_count"],
                success_count=metric["success_count"],
                fail_count=metric["fail_count"],
                skip_count=metric["skip_count"],
                started_at=started_at,
                finished_at=finished_at,
                elapsed_seconds=metric["elapsed_seconds"],
            )
        )

    insert_agent_run_metrics(rows)
    if logger:
        logger.info(
            f"[Metrics] Cycle {cycle_no} saved "
            f"(jobs={total_jobs}, elapsed={round(total_elapsed, 3)}s)"
        )
    cycle_metrics.clear()
