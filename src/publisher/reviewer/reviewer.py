"""Reviewer: one capability with a candidate gate and a scenario/package gate."""
from __future__ import annotations

from typing import Optional, Sequence

from publisher.common.logging import LOGGER
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.candidate import OptimizationResult
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.package import PackagingResult
from publisher.contracts.request import RunContext
from publisher.contracts.review import CandidateReviewResult, PackageReviewResult, WarehouseOptimization
from publisher.governor.thresholds import Thresholds
from publisher.reviewer.candidate import CandidateGate
from publisher.reviewer.package import review_contract
from publisher.reviewer.scenario import ScenarioGate


class Reviewer(CandidateGate, ScenarioGate):
    """Fail-fast invariant gate for identity, evidence epoch, config, cost, and confidence contracts.

    A gate verifies and never repairs: it raises on the first violated invariant and changes no decision, ID or
    economics. Candidate checks live in ``CandidateGate``, scenario checks in ``ScenarioGate``.
    """
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds


def _violation(exc: BaseException) -> str:
    """Short violation label for the log: the schema keyword for a JSON Schema error, otherwise the message up to its first colon.

    Gate messages put the violated rule before the colon and identifiers after it, and a schema error's own text
    embeds the offending value, so neither is logged whole.
    """
    validator: Optional[str] = getattr(exc, "validator", None)
    if validator:
        return "schema." + str(validator)
    text = str(exc).splitlines()[0] if str(exc) else ""
    return (text.split(":", 1)[0] or type(exc).__name__)[:120]


def _reject(gate: str, workspace_id: str, warehouse_id: str, exc: BaseException) -> None:
    """Log a gate rejection (rule label only, no values) before the caller re-raises the unchanged exception."""
    LOGGER.warning(
        "reviewer.gate.reject workspace_id=%s warehouse_id=%s gate=%s violation=%s",
        workspace_id, warehouse_id, gate, _violation(exc),
    )


def review_candidates(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult,
                      optimization: OptimizationResult) -> CandidateReviewResult:
    """Candidate gate for every optimized warehouse, in order; raises ValueError on the first violated invariant."""
    for w in optimization.warehouses:
        try:
            Reviewer(context.thresholds).review(w.evidence, w.candidates)
        except Exception as exc:
            _reject("candidate", w.evidence.workspace_id, w.evidence.warehouse_id, exc)
            raise
        LOGGER.debug("reviewer.gate.pass workspace_id=%s warehouse_id=%s gate=candidate candidates=%d",
                     w.evidence.workspace_id, w.evidence.warehouse_id, len(w.candidates))
    return CandidateReviewResult(list(optimization.warehouses))


def review_package(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult,
                   package: PackagingResult) -> PackageReviewResult:
    """Package gate for every packaged warehouse, in order: the scenario gate, then report-contract consistency.

    Raises on the first violated invariant (ValueError, or a schema ValidationError).
    """
    for w, p in zip(package.warehouses, package.packages, strict=True):
        review_plans(context, [w])
        try:
            review_contract(p)
        except Exception as exc:
            _reject("package", w.evidence.workspace_id, w.evidence.warehouse_id, exc)
            raise
        LOGGER.debug("reviewer.gate.pass workspace_id=%s warehouse_id=%s gate=package package_id=%s",
                     w.evidence.workspace_id, w.evidence.warehouse_id, p.recommendation_package_id)
    return PackageReviewResult(list(package.packages))


def review_plans(context: RunContext, warehouses: Sequence[WarehouseOptimization]) -> None:
    """Scenario gate for each planned warehouse, in order; raises ValueError on the first violated invariant."""
    for w in warehouses:
        try:
            Reviewer(context.thresholds).review_scenarios(w.evidence, w.candidates, w.scenarios, w.recommended_scenario_id)
        except Exception as exc:
            _reject("scenario", w.evidence.workspace_id, w.evidence.warehouse_id, exc)
            raise
        LOGGER.debug("reviewer.gate.pass workspace_id=%s warehouse_id=%s gate=scenario scenarios=%d",
                     w.evidence.workspace_id, w.evidence.warehouse_id, len(w.scenarios))
