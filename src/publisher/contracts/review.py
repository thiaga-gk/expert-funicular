"""Reviewed per-warehouse decision-layer result."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from publisher.contracts import reporter_contract as rc
from publisher.contracts.analysis import CostProfileChoice
from publisher.contracts.candidate import CandidateRecommendation, WarehouseCandidates
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
    cost_profile: CostProfileChoice

    @property
    def recommended_scenario(self) -> Optional[Scenario]:
        """The scenario whose id equals recommended_scenario_id, or None."""
        return next((s for s in self.scenarios if s.scenario_id == self.recommended_scenario_id), None)

    @property
    def recommended_execution_plan(self) -> List[PlanStep]:
        """Steps of the recommended scenario, or an empty list when none is recommended."""
        scenario = self.recommended_scenario
        return [] if scenario is None else scenario.steps

    @property
    def query_source_profile(self) -> Dict[str, Any]:
        """Query-source profile as plain data: the attributed profile when present, else derived from the current epoch's carried query view."""
        from publisher.analyzer.epochs import carried_query_view
        from publisher.analyzer.normalizer import derive_query_source_profile
        from publisher.common.utils import as_serializable

        attribution = self.evidence.attribution
        profile = attribution.query_source if attribution else derive_query_source_profile(carried_query_view(self.evidence.current_epoch.query)[0])
        return as_serializable(profile)

    @property
    def aws_365d_baseline_cost(self) -> Optional[float]:
        """Forward AWS baseline from the evidence; None when unknown."""
        return self.evidence.aws_baseline_cost_365

    @property
    def total_365d_baseline_cost(self) -> Optional[float]:
        """DBU plus AWS forward baseline from the evidence; None unless both are known."""
        return self.evidence.total_365d_baseline_cost


@dataclass
class CandidateReviewResult:
    """Candidates that passed the Reviewer's candidate gate (the gate fails fast, so every warehouse is accepted or the run stops)."""
    accepted: List[WarehouseCandidates]


@dataclass
class PackageReviewResult:
    """Packages that passed the Reviewer's package gate, ready for the Reporter."""
    packages: List[rc.RecommendationPackage]
