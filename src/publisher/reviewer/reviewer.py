"""Reviewer: one capability with a candidate gate and a scenario/package gate."""
from __future__ import annotations

from publisher.config.thresholds import Thresholds
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.candidate import OptimizationResult
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.package import PackagingResult
from publisher.contracts.request import RunContext
from publisher.contracts.review import CandidateReviewResult, PackageReviewResult
from publisher.reviewer.candidate import CandidateGate
from publisher.reviewer.package import review_contract
from publisher.reviewer.scenario import ScenarioGate


class Reviewer(CandidateGate, ScenarioGate):
    """Fail-fast invariant gate for identity, evidence epoch, config, cost, and confidence contracts."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds


def review_candidates(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult,
                      optimization: OptimizationResult) -> CandidateReviewResult:
    """Candidate gate for every optimized warehouse, in order; raises ValueError on the first violated invariant."""
    for w in optimization.warehouses:
        Reviewer(context.thresholds).review(w.evidence, w.candidates)
    return CandidateReviewResult(list(optimization.warehouses))


def review_package(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult,
                   package: PackagingResult) -> PackageReviewResult:
    """Package gate for every packaged warehouse, in order: the scenario gate, then report-contract consistency.

    Raises on the first violated invariant (ValueError, or a schema ValidationError).
    """
    for w, p in zip(package.warehouses, package.packages, strict=True):
        Reviewer(context.thresholds).review_scenarios(w.evidence, w.candidates, w.scenarios, w.recommended_scenario_id)
        review_contract(p)
    return PackageReviewResult(list(package.packages))
