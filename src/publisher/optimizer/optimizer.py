"""Optimizer: apply the policy to analyzer facts, one technique per active knob, then resolve cross-technique conflicts."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping

from publisher.analyzer.epochs import ready_billed_days
from publisher.config.thresholds import Thresholds
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision
from publisher.contracts.source import WarehouseEvidence
from publisher.optimizer import conflicts
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
