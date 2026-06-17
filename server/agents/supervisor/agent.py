"""Supervisor Agent.

외부 while 루프가 사이클을 반복하고, 각 사이클마다 LangGraph 기반 ReAct 그래프를
한 번 invoke합니다. 수퍼바이저 LLM이 아래 도구를 순서에 따라 직접 선택·호출합니다.

  Tool 1: poll_jobs          — DB 폴링 및 레지스트리 갱신
  Tool 2: run_data_migration — 데이터 이관 (MigrationOrchestrator)
  Tool 3: run_sql_conversion — SQL 변환   (SqlConversionAgent)
  Tool 4: run_sql_tuning     — SQL 튜닝   (SqlTuningAgent)
  Tool 5: run_sql_formatting — SQL 포맷팅 (SqlFormattingAgent)
  Tool 6: flush_cycle_metrics — 사이클 메트릭 저장
  Tool 7: request_wait        — 다음 사이클 전 대기
"""

import json
import logging
import os
import signal
from datetime import datetime
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from server.agents.supervisor.graph import build_supervisor_graph
from server.agents.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT
from server.agents.supervisor.state import SupervisorState
from server.config.settings import SUPERVISOR_RECURSION_LIMIT
import server.tools as supervisor_tools
from server.tools.context import is_stop_requested, request_stop

_COMMAND_FILE = Path(__file__).resolve().parent.parent.parent.parent / "runtime" / "chat_command.json"


def _read_chat_command() -> tuple[str | None, bool]:
    """채팅에서 전달된 수퍼바이저 명령을 읽고 파일을 삭제한다 (1회성).
    Returns: (command, one_shot)
    """
    if not _COMMAND_FILE.exists():
        return None, False
    try:
        data = json.loads(_COMMAND_FILE.read_text(encoding="utf-8"))
        _COMMAND_FILE.unlink(missing_ok=True)
        return data.get("command"), data.get("one_shot", False)
    except Exception:
        return None, False

logger = logging.getLogger("migration_agent")


class SupervisorAgent:
    """멀티 에이전트 시스템의 최상위 오케스트레이터."""

    def __init__(self) -> None:
        from server.repositories.migration.repository import (
            get_pending_jobs as get_mig_jobs,
            increment_batch_count as mig_inc,
        )
        from server.agents.migration.orchestrator import MigrationOrchestrator
        from server.repositories.sql.result_repository import (
            get_pending_jobs as get_sql_jobs,
            get_tuning_jobs as get_tuning_jobs_func,
            get_formatting_jobs as get_formatting_jobs_func,
            increment_batch_count as sql_inc,
        )
        from server.agents.sql_conversion.agent import SqlConversionAgent
        from server.agents.sql_tuning.agent import SqlTuningAgent
        from server.agents.sql_formatting.agent import SqlFormattingAgent

        dm = MigrationOrchestrator()
        sql_conversion = SqlConversionAgent()
        sql_tuning = SqlTuningAgent()
        sql_formatting = SqlFormattingAgent()

        self._graph = build_supervisor_graph(
            get_migration_jobs=get_mig_jobs,
            get_sql_jobs=get_sql_jobs,
            get_tuning_jobs=get_tuning_jobs_func,
            get_formatting_jobs=get_formatting_jobs_func,
            mig_increment_batch=mig_inc,
            mig_process_job=dm.process_job,
            sql_increment_batch=sql_inc,
            sql_process_job=sql_conversion.process_job,
            tune_process_job=sql_tuning.process_job,
            format_process_job=sql_formatting.process_job,
            logger=logger,
        )

    def run(self) -> None:
        """SIGINT/SIGTERM 을 등록하고 Supervisor 사이클 루프를 실행한다."""
        logger.info("============================================================")
        logger.info(" Multi-Agent Supervisor 시작 (LLM ReAct Mode)")
        logger.info("  ├─ Tool 1: poll_jobs           — DB 폴링")
        logger.info("  ├─ Tool 2: run_data_migration  — 데이터 이관")
        logger.info("  ├─ Tool 3: run_sql_conversion  — SQL 변환")
        logger.info("  ├─ Tool 4: run_sql_tuning      — SQL 튜닝")
        logger.info("  ├─ Tool 5: run_sql_formatting  — SQL 포맷팅")
        logger.info("  ├─ Tool 6: flush_cycle_metrics — 메트릭 저장")
        logger.info("  └─ Tool 7: request_wait        — 사이클 대기")
        logger.info("============================================================")

        self._register_signal_handlers()
        batch_no = int(datetime.now().strftime("%Y%m%d%H%M%S"))
        supervisor_tools.start_batch_metrics(batch_no)
        logger.info(f"[Supervisor] Batch {batch_no} 시작")

        cycle = 0
        try:
            while not is_stop_requested():
                cycle += 1
                supervisor_tools.start_cycle_metrics(cycle)
                logger.info(f"\n{'=' * 50}")
                logger.info(f"[Supervisor] Cycle {cycle} 시작")

                human_content = (
                    f"사이클 {cycle}을 시작합니다. "
                    "poll_jobs()를 호출하여 현황을 파악하고 작업을 처리하세요."
                )
                chat_command, one_shot = _read_chat_command()
                if chat_command:
                    human_content += f"\n\n[사용자 요청] {chat_command}\n위 요청을 이번 사이클에 반영하세요."
                    if one_shot:
                        human_content += (
                            "\n이 요청은 one_shot=true 입니다. poll_jobs() 결과에서 요청 대상만 실행하고, "
                            "다른 작업 유형은 실행하지 마세요. 작업 후 flush_cycle_metrics()를 호출한 뒤 "
                            "request_wait() 없이 최종 응답하고 사이클을 끝내세요."
                        )
                    logger.info(f"[Supervisor] 채팅 명령 수신 (one_shot={one_shot}): {chat_command}")

                initial_state: SupervisorState = {
                    "messages": [
                        SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
                        HumanMessage(content=human_content),
                    ],
                    "cycle": cycle,
                    "stop_requested": False,
                }

                try:
                    self._graph.invoke(
                        initial_state,
                        config={"recursion_limit": SUPERVISOR_RECURSION_LIMIT},
                    )
                except (KeyboardInterrupt, SystemExit):
                    break
                except Exception:
                    logger.exception(
                        "[Supervisor] 예기치 못한 오류로 사이클이 중단되었습니다."
                    )
                    raise
                finally:
                    # LLM이 flush_cycle_metrics 호출을 누락한 경우를 대비한 안전망
                    supervisor_tools.finish_cycle_metrics(logger)

                if one_shot:
                    logger.info("[Supervisor] one_shot 사이클 완료. 자동 종료합니다.")
                    break

        finally:
            logger.info("[Supervisor] 모든 에이전트가 종료되었습니다.")

    @staticmethod
    def _register_signal_handlers() -> None:
        signal_count = {"n": 0}

        def _handle(_signum, _frame):
            signal_count["n"] += 1
            request_stop()
            if signal_count["n"] == 1:
                try:
                    msg = "[Supervisor] Stop signal received. Finishing current job...\n"
                    os.write(2, msg.encode("utf-8", errors="ignore"))
                except OSError:
                    pass
                # Raise KeyboardInterrupt so blocking LLM/tool calls are interrupted immediately.
                raise KeyboardInterrupt
            else:
                os._exit(130)

        signal.signal(signal.SIGINT, _handle)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _handle)
