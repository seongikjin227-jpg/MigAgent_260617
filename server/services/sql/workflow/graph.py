"""단일 SQL conversion 재시도 시도에 대한 LangGraph 정의.

START
  -> tobe_generation.generate
      -> non-SELECT: END
      -> SELECT:     tobe_generation.validate -> END
"""

from typing import Literal

from langgraph.graph import END, START, StateGraph

from server.services.sql.workflow.state import MigrationGraphState


def build_migration_workflow(generation_agent, tuning_agent=None):
    def tobe_generation_generate_node(state: MigrationGraphState) -> MigrationGraphState:
        execution = state["execution"]
        generation_agent.generate(execution)
        return {"execution": execution, "terminal_action": None}

    def tobe_generation_validate_node(state: MigrationGraphState) -> MigrationGraphState:
        execution = state["execution"]
        generation_agent.validate(execution)
        return {"execution": execution, "terminal_action": None}

    graph = StateGraph(MigrationGraphState)
    graph.add_node("tobe_generation.generate", tobe_generation_generate_node)
    graph.add_node("tobe_generation.validate", tobe_generation_validate_node)

    graph.add_edge(START, "tobe_generation.generate")
    graph.add_conditional_edges(
        "tobe_generation.generate",
        route_after_generation,
        {
            "validate_generation": "tobe_generation.validate",
            "end": END,
        },
    )
    graph.add_edge("tobe_generation.validate", END)
    return graph.compile()


def route_after_generation(state: MigrationGraphState) -> Literal["validate_generation", "end"]:
    execution = state["execution"]
    tag_kind = (execution.job.tag_kind or "").strip().upper()
    if tag_kind != "SELECT":
        return "end"
    return "validate_generation"
