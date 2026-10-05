"""Reviewed per-warehouse decision-layer result."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from publisher.contracts.candidate import CandidateRecommendation
from publisher.contracts.scenario import PlanStep, Scenario
from publisher.contracts.source import WarehouseEvidence


@dataclass
class WarehouseOptimization:
    """Reviewed decision-layer result for one warehouse; not a serialization contract."""
    warehouse_id: str
    workspace_id: str
    evidence: WarehouseEvidence
    candidates: List[CandidateRecommendation]
    scenarios: List[Scenario]
    recommended_scenario_id: Optional[str]

    @property
    def recommended_scenario(self) -> Optional[Scenario]:
        return next((s for s in self.scenarios if s.scenario_id == self.recommended_scenario_id), None)

    @property
    def recommended_execution_plan(self) -> List[PlanStep]:
        scenario = self.recommended_scenario
        return [] if scenario is None else scenario.steps

    @property
    def query_source_profile(self) -> Dict[str, Any]:
        from publisher.publisher import as_serializable, carried_query_view, derive_query_source_profile

        attribution = self.evidence.attribution
        profile = attribution.query_source if attribution else derive_query_source_profile(carried_query_view(self.evidence.current_epoch.query)[0])
        return as_serializable(profile)

    @property
    def aws_365d_baseline_cost(self) -> Optional[float]:
        return self.evidence.aws_baseline_cost_365

    @property
    def total_365d_baseline_cost(self) -> Optional[float]:
        return self.evidence.total_365d_baseline_cost
