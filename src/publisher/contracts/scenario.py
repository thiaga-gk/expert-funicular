"""Packager output: ordered plan steps and stateful scenario economics."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from publisher.contracts.candidate import Range3


@dataclass
class PlanStep:
    """One ordered scenario step referencing a canonical candidate and validation gate."""
    step: int
    recommendation_id: str
    knob: str
    current_config: Dict[str, Any]
    recommended_config: Dict[str, Any]
    status: str
    validation_gate: str
    remeasure_days: int
    invalidates: List[str]
    dependencies: List[str]
    execution_state: str = "READY"
    instruction: str = ""
    running_estimated_cost_range_365: Optional[Range3] = None
    running_estimated_savings_range_365: Optional[Range3] = None
    running_dbu_cost_range_365: Optional[Range3] = None
    running_aws_cost_range_365: Optional[Range3] = None
    running_dbu_savings_range_365: Optional[Range3] = None
    running_aws_savings_range_365: Optional[Range3] = None
    step_dbu_savings_range_365: Optional[Range3] = None
    step_aws_savings_range_365: Optional[Range3] = None
    confidence: Dict[str, Any] = field(default_factory=dict)
    pricing_basis: str = "AUTHORITATIVE"


@dataclass
class PortfolioEstimate:
    """Stateful non-overlapping forward DBU/AWS/total scenario economics."""
    estimate_status: str
    baseline_cost_365: float
    estimated_cost_range_365: Optional[Range3]
    estimated_savings_range_365: Optional[Range3]
    method: str
    non_additive: bool = True
    dbu_365d_baseline_cost: Optional[float] = None
    aws_365d_baseline_cost: Optional[float] = None
    total_365d_baseline_cost: Optional[float] = None
    dbu_365d_estimated_cost_range: Optional[Range3] = None
    aws_365d_estimated_cost_range: Optional[Range3] = None
    total_365d_estimated_cost_range: Optional[Range3] = None
    dbu_365d_savings_range: Optional[Range3] = None
    aws_365d_savings_range: Optional[Range3] = None
    total_365d_savings_range: Optional[Range3] = None
    # Stateful upside of steps marked PENDING_REEVALUATION; None when nothing is pending or it cannot be priced.
    contingent: Optional["PortfolioEstimate"] = None


@dataclass
class Scenario:
    """Mutually exclusive execution-plan branch with ordered steps and portfolio estimate.

    ``scenario_type`` is set for the migration branch (``WAREHOUSE_TYPE_MIGRATION``, with or without its step) and the
    no-change baseline (``NO_CHANGE_BASELINE``); when None the public type follows the steps.
    """
    scenario_id: str
    warehouse_id: str
    workspace_id: str
    name: str
    description: str
    steps: List[PlanStep]
    portfolio_estimate: PortfolioEstimate
    blockers: List[str]
    recommended: bool = False
    compatibility: Dict[str, Any] = field(default_factory=dict)
    confidence: Dict[str, Any] = field(default_factory=dict)
    scenario_type: Optional[str] = None


PRICING_AUTHORITATIVE = "AUTHORITATIVE"


PRICING_CONTINGENT = "CONTINGENT_PENDING_REEVALUATION"
