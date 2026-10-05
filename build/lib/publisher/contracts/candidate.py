"""Optimizer and candidate-estimator output: decisions, ranges and candidate recommendations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from publisher.contracts.analysis import Finding


@dataclass
class OptimizationDecision:
    """Optimizer decision contract before any cost counterfactual is attached."""
    warehouse_id: str
    knob: str
    objective: str
    status: str
    current_config: Dict[str, Any]
    recommended_config: Dict[str, Any]
    blockers: List[str]
    dependencies: List[str]
    invalidates: List[str]
    reanalysis_required: List[str]
    rationale: str


@dataclass(frozen=True)
class Range3:
    """Deterministic low/base/high range used for cost, savings, and scenario estimates."""
    low: float
    base: float
    high: float

    def rounded(self) -> "Range3":
        return Range3(round(self.low, 2), round(self.base, 2), round(self.high, 2))


@dataclass
class CandidateRecommendation:
    """Reviewed standalone per-knob counterfactual with evidence, confidence, and economics."""
    recommendation_id: str
    warehouse_id: str
    workspace_id: str
    warehouse_name: str
    knob: str
    objective: str
    status: str
    current_config: Dict[str, Any]
    recommended_config: Dict[str, Any]
    current_cost_365: Optional[float]
    baseline_forecast_cost_365: Optional[float]
    estimated_cost_365: Optional[float]
    estimated_cost_range_365: Optional[Range3]
    estimated_savings_365: Optional[float]
    estimated_savings_range_365: Optional[Range3]
    confidence: Dict[str, Any]
    findings: List[Finding]
    blockers: List[str]
    dependencies: List[str]
    invalidates: List[str]
    reanalysis_required: List[str]
    metadata: Dict[str, Any]
    dbu_365d_baseline_cost: Optional[float] = None
    aws_365d_baseline_cost: Optional[float] = None
    total_365d_baseline_cost: Optional[float] = None
    dbu_365d_savings_estimate: Optional[float] = None
    aws_365d_savings_estimate: Optional[float] = None
    total_365d_savings_estimate: Optional[float] = None
    dbu_365d_savings_range: Optional[Range3] = None
    aws_365d_savings_range: Optional[Range3] = None
    total_365d_savings_range: Optional[Range3] = None
    dbu_365d_estimated_cost_range: Optional[Range3] = None
    aws_365d_estimated_cost_range: Optional[Range3] = None
    total_365d_estimated_cost_range: Optional[Range3] = None
