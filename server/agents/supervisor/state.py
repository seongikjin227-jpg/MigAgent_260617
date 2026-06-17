"""Supervisor 전역 상태 정의."""

from typing import Annotated
from typing_extensions import TypedDict
from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class SupervisorState(TypedDict):
    # LLM 대화 메시지 (ReAct 루프에서 누적, 사이클 전환 시 초기화)
    messages: Annotated[list[BaseMessage], add_messages]

    # 완료된 폴링 사이클 수 (로깅용)
    cycle: int

    # SIGINT/SIGTERM 수신 시 True → 루프 종료
    stop_requested: bool
