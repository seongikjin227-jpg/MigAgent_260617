"""SQL 변환 에이전트.

TobeMultiAgentCoordinator를 래핑하여 Supervisor의 tool로 사용될 수 있는
독립적인 에이전트로 노출합니다.
내부 변환/검증 로직(sql_pipeline)은 그대로 유지됩니다.
"""

from server.services.sql.agents import (
    MappingRuleProvider,
    TobeSqlGenerationAgent,
    SqlTuningAgent,
    TobeMultiAgentCoordinator,
)


class SqlConversionAgent:
    """SQL 변환 에이전트 — Supervisor tool로 사용됩니다.

    레거시 SQL → TO-BE SQL 변환 및 Bind 파라미터 추출, 기본 검증까지 처리합니다.
    튜닝은 SqlTuningAgent가 담당합니다(max_iterations=0으로 비활성화).
    """

    def __init__(self) -> None:
        self._coordinator = TobeMultiAgentCoordinator(
            mapping_rule_provider=MappingRuleProvider(),
            generation_agent=TobeSqlGenerationAgent(),
            tuning_agent=SqlTuningAgent(max_iterations=0),
        )

    def process_job(self, job) -> str:
        """SQL 변환 작업 1건을 처리합니다."""
        return self._coordinator.process_job(job)
