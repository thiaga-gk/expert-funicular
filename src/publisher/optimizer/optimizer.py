"""Optimizer: apply the policy to analyzer facts, one technique per active knob, then resolve cross-technique conflicts."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence

from publisher.analyzer.epochs import ready_billed_days
from publisher.common.logging import LOGGER
from publisher.governor.thresholds import Thresholds
from publisher.contracts.analysis import AnalysisResult, FleetAnalysis, WarehouseAnalysis
from publisher.contracts.candidate import OptimizationDecision, OptimizationResult, WarehouseCandidates
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.request import RunContext
from publisher.contracts.source import WarehouseEvidence
from publisher.optimizer import conflicts
from publisher.optimizer.estimator.candidate import Estimator
from publisher.optimizer.techniques import autostop, migration, scaling, size


class Optimizer:
    """Apply deterministic policy thresholds to analyzer facts and propose config decisions."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def _ceiling_for(self, level: int, min_clusters: int) -> int:
        """Ceiling for an observed peak: the peak plus headroom, never below the minimum."""
        headroom = max(self.t.max_cluster_headroom_clusters, math.ceil(self.t.max_cluster_headroom_pct / 100.0 * level))
        return max(min_clusters, level + headroom)

    def _growing(self, facts: Mapping[str, Any]) -> bool:
        ratio = facts.get("growth_ratio")
        return ratio is not None and ratio >= self.t.growth_hold_weekly_ratio

    def optimize(self, e: WarehouseEvidence, analyses: Dict[str, AnalysisResult]) -> List[OptimizationDecision]:
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
        return out


def optimize(w: WarehouseAnalysis, thresholds: Thresholds) -> WarehouseCandidates:
    """Decisions for one eligible warehouse, each priced as an isolated candidate."""
    e, analyses = w.evidence, w.analyses
    decisions = Optimizer(thresholds).optimize(e, analyses)
    candidates = Estimator(thresholds).candidates(e, analyses, decisions)
    return WarehouseCandidates(w, decisions, candidates)


def optimize_eligible(eligible: Sequence[WarehouseAnalysis], thresholds: Thresholds, batch_size: int = 100) -> OptimizationResult:
    """Optimize the eligible warehouses in batches, in order."""
    results: List[WarehouseCandidates] = []
    batch = max(1, int(batch_size))
    for offset in range(0, len(eligible), batch):
        chunk = eligible[offset:offset + batch]
        LOGGER.info("pipeline.batch.start batch_index=%d start=%d size=%d", offset // batch + 1, offset, len(chunk))
        for w in chunk:
            e = w.evidence
            LOGGER.info(
                "warehouse.start workspace_id=%s warehouse_id=%s name=%s epoch_id=%s",
                e.workspace_id, e.warehouse_id, e.warehouse_name, e.current_epoch_id,
            )
            results.append(optimize(w, thresholds))
        LOGGER.info("pipeline.batch.done batch_index=%d processed=%d", offset // batch + 1, len(chunk))
    return OptimizationResult(results)


def run(context: RunContext, analysis: FleetAnalysis, governance: GovernanceResult) -> OptimizationResult:
    """What should change: decisions and isolated candidate estimates for every eligible warehouse."""
    return optimize_eligible(governance.eligible, context.thresholds, context.request.batch_size)
