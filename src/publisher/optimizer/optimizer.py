"""Optimizer: apply the policy to analyzer facts, one technique per active knob, then resolve cross-technique conflicts."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence

from publisher.analyzer.epochs import ready_billed_days
from publisher.common.logging import LOGGER
from publisher.governor.cost_profiles import thresholds_for_profile
from publisher.governor.thresholds import Thresholds
from publisher.contracts.analysis import AnalysisResult, FleetAnalysis, WarehouseAnalysis
from publisher.contracts.candidate import OptimizationDecision, OptimizationResult, WarehouseCandidates
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.request import RunContext
from publisher.contracts.scope import FULL_SCOPE, RunScope
from publisher.contracts.source import WarehouseEvidence
from publisher.optimizer import conflicts
from publisher.optimizer.estimator.candidate import Estimator
from publisher.optimizer.techniques import autostop, migration, scaling, size


class Optimizer:
    """Apply deterministic policy thresholds to analyzer facts and propose config decisions.

    Exactly one decision per active domain (size, max clusters, min clusters, auto-stop, warehouse type), in that
    order, so the candidate gate's one-per-knob invariant holds by construction. Photon and the warehouse channel
    are observation-only and have no technique here. Thresholds come from the governor; nothing reads wall-clock
    time or randomness, so identical evidence and policy give identical decisions.
    """
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def _ceiling_for(self, level: int, min_clusters: int, headroom_pct: float) -> int:
        """Ceiling for an observed peak: the peak plus headroom (at least max_cluster_headroom_clusters, else
        ``headroom_pct`` of the peak), never below the minimum."""
        headroom = max(self.t.max_cluster_headroom_clusters, math.ceil(headroom_pct / 100.0 * level))
        return max(min_clusters, level + headroom)

    def _growing(self, facts: Mapping[str, Any]) -> bool:
        """True when weekly query volume growth reaches the hold ratio; unknown growth is not growth."""
        ratio = facts.get("growth_ratio")
        return ratio is not None and ratio >= self.t.growth_hold_weekly_ratio

    def optimize(self, e: WarehouseEvidence, analyses: Dict[str, AnalysisResult]) -> List[OptimizationDecision]:
        """All five decisions for one warehouse from its current-epoch analyses.

        Techniques run in a fixed order (size, max, min, auto-stop, migration) because the migration technique reads
        the auto-stop decision; the canary rollback then rewrites ``out`` in place when a fail-fast guard tripped.
        """
        cfg = e.current_epoch.config
        # Readiness: the current configuration has run for enough settled billed days (not a fixed calendar age).
        billed_days = ready_billed_days(e)
        young = billed_days < self.t.readiness_min_billed_days
        out: List[OptimizationDecision] = []

        size.decide(self, e, analyses, cfg, young, billed_days, out)
        scaling.decide_max_clusters(self, e, analyses, cfg, young, billed_days, out)
        scaling.decide_min_clusters(self, e, analyses, cfg, young, billed_days, out)
        autostop.decide(self, e, analyses, cfg, young, billed_days, out)
        migration.decide(self, e, analyses, cfg, young, billed_days, out)
        conflicts.apply_canary_rollback(self, e, analyses, cfg, young, billed_days, out)
        for d in out:
            LOGGER.debug(
                "optimizer.decision workspace_id=%s warehouse_id=%s knob=%s status=%s blockers=%s",
                e.workspace_id, e.warehouse_id, d.knob, d.status, ",".join(d.blockers) or "NONE",
            )
        return out


def in_scope(decisions: Sequence[OptimizationDecision], scope: RunScope) -> List[OptimizationDecision]:
    """The decisions a run publishes: the in-scope knobs, plus any canary rollback (a safety action, kept in every scope).

    Every knob is decided first, so cross-technique holds (a rollback holding a conflicting cut, migration reading the
    auto-stop decision) are the same in every scope; scope only removes decisions afterwards, in order.
    """
    return [d for d in decisions if d.knob in scope.knobs or "CANARY_GUARDRAIL_BREACHED" in d.blockers]


def optimize(w: WarehouseAnalysis, thresholds: Thresholds, scope: RunScope = FULL_SCOPE) -> WarehouseCandidates:
    """Decisions for one eligible warehouse under its cost profile and within the run scope, each priced as an isolated
    (non-additive) candidate."""
    e, analyses = w.evidence, w.analyses
    t = thresholds_for_profile(thresholds, w.cost_profile.name)
    decisions = in_scope(Optimizer(t).optimize(e, analyses), scope)
    candidates = Estimator(t).candidates(e, analyses, decisions)
    return WarehouseCandidates(w, decisions, candidates)


def optimize_eligible(eligible: Sequence[WarehouseAnalysis], thresholds: Thresholds, batch_size: int = 100,
                      scope: RunScope = FULL_SCOPE) -> OptimizationResult:
    """Optimize the eligible warehouses in batches, in order.

    Batching only bounds the log granularity; results and their order do not depend on ``batch_size``.
    """
    results: List[WarehouseCandidates] = []
    batch = max(1, int(batch_size))
    for offset in range(0, len(eligible), batch):
        chunk = eligible[offset:offset + batch]
        LOGGER.info("pipeline.batch.start batch_index=%d start=%d size=%d", offset // batch + 1, offset, len(chunk))
        for w in chunk:
            e = w.evidence
            LOGGER.info(
                "warehouse.start workspace_id=%s warehouse_id=%s epoch_id=%s",
                e.workspace_id, e.warehouse_id, e.current_epoch_id,
            )
            results.append(optimize(w, thresholds, scope))
        LOGGER.info("pipeline.batch.done batch_index=%d processed=%d", offset // batch + 1, len(chunk))
    return OptimizationResult(results)


def run(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult) -> OptimizationResult:
    """What should change: decisions and isolated candidate estimates for every eligible warehouse, within the run scope."""
    return optimize_eligible(governance.eligible, context.thresholds, context.request.batch_size, governance.scope)
