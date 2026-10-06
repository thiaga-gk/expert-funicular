"""Warehouse size: the size decision from capacity, latency, spill and failure evidence."""
from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING

from publisher.analyzer.blockers import failure_blockers
from publisher.common.utils import safe_int
from publisher.config.defaults import SIZE_ORDER
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision
from publisher.contracts.source import WarehouseConfig, WarehouseEvidence

if TYPE_CHECKING:
    from publisher.optimizer.optimizer import Optimizer


def decide(self: "Optimizer", e: WarehouseEvidence, analyses: Dict[str, AnalysisResult], cfg: WarehouseConfig, young: bool,
           billed_days: int, out: List[OptimizationDecision]) -> None:
    """Append the warehouse_size decision to ``out`` (``self`` is the Optimizer: thresholds and shared helpers).

    Branch order: unknown size -> BLOCKED; spill frequency without severity -> INSUFFICIENT_EVIDENCE; spill over
    both upsize thresholds -> one size up as a TEST_REQUIRED canary (held as NO_CHANGE when the spill is
    concentrated in a few statements, INSUFFICIENT_EVIDENCE when the sample is thin or the epoch young, BLOCKED
    at the largest size); otherwise one size down as a TEST_REQUIRED canary only when every downsize safety check
    is clear (blockers accumulate and force NO_CHANGE). A size move invalidates the capacity knobs. Speed-up and
    cost of a resize are modeled, never observed, so a resize is never ACTIONABLE.
    """
    # Size
    a = analyses["warehouse_size"]; f=a.facts
    blockers: List[str] = []
    if cfg.warehouse_size not in SIZE_ORDER:
        out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","COST_AND_PERFORMANCE","BLOCKED",
            {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["UNKNOWN_WAREHOUSE_SIZE"],
            [],[],[],"Warehouse size is outside the known deterministic ordering."))
    elif f["spill_pct"] is not None and f["spill_pct"] >= self.t.spill_query_pct_upsize and f["spill_p95"] is None:
        out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","INSUFFICIENT_EVIDENCE",
            {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["SPILL_SEVERITY_EVIDENCE_UNAVAILABLE"],[],[],[],
            "Spill frequency crosses the upsize threshold, but spill severity was not reported (older collector evidence); no resize is recommended."))
    elif f["spill_pct"] is not None and f["spill_pct"] >= self.t.spill_query_pct_upsize and f["spill_p95"] >= self.t.spill_p95_bytes_upsize:
        target = next_size(cfg.warehouse_size)
        # Spill frequency/severity are only trustworthy with a minimum sample from a mature config epoch.
        if young: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
        # Spill and latency are measured on executed queries (not result-cache hits); so is the sample.
        if f["decision_queries"] is None: blockers.append("DECISION_QUERY_EVIDENCE_UNAVAILABLE")
        elif f["decision_queries"] < self.t.min_query_sample_size: blockers.append("INSUFFICIENT_QUERY_SAMPLE")
        if f["spill_n"] < self.t.severity_min_affected_queries: blockers.append("INSUFFICIENT_AFFECTED_QUERY_SAMPLE")
        concentrated = f.get("top_spill_share") is not None and f["top_spill_share"] >= self.t.spill_concentration_hold_pct
        if not blockers and f.get("top_spill_share") is None:
            blockers.append("SPILL_CONCENTRATION_EVIDENCE_UNAVAILABLE")
        if target and not blockers and concentrated:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","NO_CHANGE",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["SPILL_CONCENTRATED_IN_FEW_STATEMENTS"],[],[],[],
                f"{f['top_spill_share']:g}% of spilled bytes come from {self.t.spill_concentration_top_sources} or fewer statement sources ({f['spill_sources']} spilled in all): rewrite those statements; a larger size for every query is the expensive fix."))
        elif target and blockers:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},blockers,[],[],[],
                "Spill crosses the upsize thresholds, but the query sample, spilled-query count or config epoch is too small/new to act on; no resize is recommended."))
        elif target:
            # The speed-up and its cost are modeled, not observed: one size up is a latency/cost canary.
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","TEST_REQUIRED",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":target},["A_B_PERFORMANCE_VALIDATION_REQUIRED"],[],
                ["max_clusters","min_clusters"],["max_clusters","min_clusters"],
                "Spill frequency and severity both cross the deterministic upsize thresholds; one size up as a canary validates the speed-up and its cost."))
        else:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","BLOCKED",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["ALREADY_AT_MAX_KNOWN_SIZE"],
                [],[],[],"Spill pressure exists but the warehouse is already at the largest known size."))
    else:
        target = previous_size(cfg.warehouse_size)
        batch = f.get("batch_profile")
        if target is None: blockers.append("ALREADY_AT_SMALLEST_SIZE")
        if young: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
        if batch:
            # Batch: one run a day is enough evidence; the canary measures job run time instead of interactive latency.
            if f["active_days_30"] < self.t.batch_min_active_days: blockers.append("INSUFFICIENT_BATCH_RUN_DAYS")
        elif f["decision_queries"] is None: blockers.append("DECISION_QUERY_EVIDENCE_UNAVAILABLE")
        elif f["decision_queries"] < self.t.strong_query_sample_size: blockers.append("INSUFFICIENT_STRONG_QUERY_SAMPLE")
        if f["spill_pct"] is None: blockers.append("SPILL_EVIDENCE_UNAVAILABLE")
        elif f["spill_pct"] > self.t.spill_query_pct_downsize_safe: blockers.append("SPILL_PRESENT")
        if f["queue_pct"] is None: blockers.append("QUEUE_EVIDENCE_UNAVAILABLE")
        elif f["queue_pct"] > self.t.queue_query_pct_safe: blockers.append("QUEUE_PRESSURE_PRESENT")
        blockers.extend(failure_blockers(f["failure_pct"], self.t))
        if not batch:
            if f["p95_exec"] is None or f["p99_exec"] is None: blockers.append("LATENCY_EVIDENCE_UNAVAILABLE")
            if f["p95_exec"] is not None and f["p95_exec"] > self.t.p95_execution_ms_downsize_safe: blockers.append("P95_LATENCY_TOO_HIGH")
            if f["p99_exec"] is not None and f["p99_exec"] > self.t.p99_execution_ms_downsize_safe: blockers.append("P99_LATENCY_TOO_HIGH")
        # Seasonality: the canary's 30-day evidence cannot see a peak the epoch reached beyond the recent ceiling.
        mf = analyses["max_clusters"].facts
        if mf.get("max_observed") is not None and mf.get("epoch_max_observed") is not None \
                and safe_int(mf["epoch_max_observed"]) > self._ceiling_for(safe_int(mf["max_observed"]), cfg.min_clusters, self.t.max_cluster_headroom_pct):
            blockers.append("PEAK_OUTSIDE_RECENT_WINDOW")
        if self._growing(f): blockers.append("QUERY_VOLUME_GROWING")
        if f.get("older_epoch_demand_higher"): blockers.append("OLDER_EPOCH_PEAK_DEMAND_HIGHER")
        if target and not blockers:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","COST","TEST_REQUIRED",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":target},["A_B_PERFORMANCE_VALIDATION_REQUIRED"],
                [],["max_clusters","min_clusters"],
                ["max_clusters","min_clusters"],
                ("A batch warehouse (mostly job queries) with no spill or queueing: one size down is a canary that measures job run times; absence of pressure is not proof the jobs still finish in their windows." if batch else
                 "Low spill, queueing, and latency make one-size down a controlled canary candidate; absence of pressure is not proof of SLA safety.")))
        else:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","COST_AND_PERFORMANCE","NO_CHANGE",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},blockers,[],[],[],
                f"Already at the smallest size ({cfg.warehouse_size}); nothing calls for a larger one." if blockers==["ALREADY_AT_SMALLEST_SIZE"] else
                "No deterministic upsize signal and downsize safety criteria are not all satisfied."))


def previous_size(size: str) -> Optional[str]:
    """One size down in SIZE_ORDER; None for an unknown size or the smallest one."""
    if size not in SIZE_ORDER:
        return None
    i = SIZE_ORDER.index(size)
    return SIZE_ORDER[i - 1] if i > 0 else None


def next_size(size: str) -> Optional[str]:
    """One size up in SIZE_ORDER; None for an unknown size or the largest known one."""
    if size not in SIZE_ORDER:
        return None
    i = SIZE_ORDER.index(size)
    return SIZE_ORDER[i + 1] if i < len(SIZE_ORDER) - 1 else None
