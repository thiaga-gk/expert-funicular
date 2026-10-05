"""Cross-technique conflicts: a fail-fast canary rollback restores the changed knob and holds the cuts that conflict with it."""
from __future__ import annotations

from typing import Dict, List, TYPE_CHECKING

from publisher.analyzer.findings import canary_rationale
from publisher.common.utils import safe_int
from publisher.config.defaults import SIZE_ORDER
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision
from publisher.contracts.source import WarehouseConfig, WarehouseEvidence

if TYPE_CHECKING:
    from publisher.optimizer.optimizer import Optimizer


def apply_canary_rollback(self: "Optimizer", e: WarehouseEvidence, analyses: Dict[str, AnalysisResult], cfg: WarehouseConfig, young: bool,
                          billed_days: int, out: List[OptimizationDecision]) -> None:
    """Rewrite ``out`` in place when a canary guard tripped (``self`` is the Optimizer)."""
    # Fail-fast canary rollback: a guard tripped on the new epoch's own evidence after a canary-type change restores
    # the previous value of the changed knob (the one decision an older epoch's configuration sets).
    canary = analyses["warehouse_size"].facts.get("canary") or {}
    if canary.get("guards"):
        why = canary_rationale(canary, self.t)
        changes = dict(canary["changes"])
        if changes.get("min_clusters", 0) > cfg.max_clusters:
            # The previous floor does not fit under the current ceiling: the ceiling the change lowered comes back too.
            changes["max_clusters"] = canary["previous"]["max_clusters"]
        for knob, previous in sorted(changes.items()):
            i = next(k for k, d in enumerate(out) if d.knob == knob)
            # A size rollback invalidates the capacity knobs it does not restore itself.
            invalidates = [k for k in ("max_clusters", "min_clusters") if k not in changes] if knob == "warehouse_size" else []
            out[i] = OptimizationDecision(e.warehouse_id, knob, "PERFORMANCE", "ACTIONABLE",
                {knob: out[i].current_config[knob]}, {knob: previous}, ["CANARY_GUARDRAIL_BREACHED"] + sorted(canary["guards"]),
                [], invalidates, invalidates,
                f"Roll back the {knob} change to {previous}: since the change, {why}. The previous value is restored; no saving is claimed.")
        # Changes that conflict with the rollback wait for it: after a size rollback the other size and cluster
        # cuts (their evidence was measured at the size being rolled back), after a floor rollback a lower ceiling
        # or a smaller size (the floor comes back). An increase for queueing or spill is never held; after a size
        # rollback it waits behind it (the size rollback invalidates it).
        def lower(d: OptimizationDecision) -> bool:
            cur, rec = d.current_config.get(d.knob), d.recommended_config.get(d.knob)
            if d.knob == "warehouse_size":
                return cur in SIZE_ORDER and rec in SIZE_ORDER and SIZE_ORDER.index(rec) < SIZE_ORDER.index(cur)
            return safe_int(rec) < safe_int(cur)
        def conflicts(d: OptimizationDecision) -> bool:
            if d.knob in changes or d.status not in ("ACTIONABLE", "TEST_REQUIRED", "REVIEW") or not lower(d):
                return False
            if "warehouse_size" in changes:
                return d.knob in ("max_clusters", "min_clusters")
            return "min_clusters" in changes and d.knob in ("max_clusters", "warehouse_size")
        for i, d in enumerate(out):
            if conflicts(d):
                out[i] = OptimizationDecision(e.warehouse_id, d.knob, d.objective, "NO_CHANGE", dict(d.current_config), dict(d.current_config),
                    ["CANARY_ROLLBACK_PENDING"], d.dependencies, [], [],
                    f"Held while the canary rollback is pending: this {d.knob} change conflicts with restoring the previous configuration.")
