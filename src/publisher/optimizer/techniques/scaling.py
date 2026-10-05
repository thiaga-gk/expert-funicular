"""Cluster scaling: the max-cluster ceiling for observed peak demand and the min-cluster floor for busy starts."""
from __future__ import annotations

from typing import Dict, List, Optional, TYPE_CHECKING

from publisher.analyzer.blockers import RESOURCE_WINDOW_BLOCKERS, failure_blockers
from publisher.common.utils import safe_float, safe_int
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision
from publisher.contracts.source import WarehouseConfig, WarehouseEvidence

if TYPE_CHECKING:
    from publisher.optimizer.optimizer import Optimizer


def decide_max_clusters(self: "Optimizer", e: WarehouseEvidence, analyses: Dict[str, AnalysisResult], cfg: WarehouseConfig, young: bool,
                        billed_days: int, out: List[OptimizationDecision]) -> None:
    """Append this knob's decision to ``out`` (``self`` is the Optimizer: thresholds and shared helpers)."""
    # Max clusters
    a=analyses["max_clusters"]; f=a.facts
    window_known = f["at_max_pct"] is not None and f["max_observed"] is not None
    saturated = window_known and f["at_max_pct"] >= self.t.time_at_max_pct_saturated
    queue_known = f["queue_pct"] is not None
    if queue_known and f["queue_pct"] >= self.t.queue_query_pct_increase_max and f["wait_capacity"] is None and saturated:
        out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","INSUFFICIENT_EVIDENCE",
            {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["QUEUE_WAIT_EVIDENCE_UNAVAILABLE"],
            ["warehouse_size"],[],[],"Queueing and saturation cross the increase thresholds, but capacity-wait severity was not reported (older collector evidence); no change is recommended."))
    elif queue_known and f["queue_pct"] >= self.t.queue_query_pct_increase_max and f["wait_capacity"] is not None and f["wait_capacity"] >= self.t.queue_wait_p95_ms_increase_max and saturated:
        if cfg.max_clusters >= self.t.max_clusters_supported_limit:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","BLOCKED",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["MAX_CLUSTER_LIMIT_REACHED"],
                ["warehouse_size"],[],[],"Concurrency pressure exists but max clusters is at the policy/API limit."))
        elif f["queue_n"] < self.t.severity_min_affected_queries or young or f["queries_30"] < self.t.min_query_sample_size:
            # Like every other change: a mature configuration epoch, a query sample and enough queued queries.
            gates=[code for code,hit in (
                ("CURRENT_CONFIG_EPOCH_TOO_NEW",young),
                ("INSUFFICIENT_QUERY_SAMPLE",f["queries_30"] < self.t.min_query_sample_size),
                ("INSUFFICIENT_AFFECTED_QUERY_SAMPLE",f["queue_n"] < self.t.severity_min_affected_queries)) if hit]
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},gates,
                ["warehouse_size"],[],[],"Queueing crosses the increase thresholds, but the configuration epoch, query sample or queued-query count is too new/small to act on; no change is recommended."))
        elif cfg.warehouse_type!="SERVERLESS" and not f["concurrency_available"]:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["CONCURRENCY_EVIDENCE_UNAVAILABLE"],["warehouse_size"],[],[],
                "Queueing crosses the increase thresholds, but per-minute concurrency was not collected (older evidence), so the target cannot be derived."))
        elif cfg.warehouse_type!="SERVERLESS" and safe_float(f["busy_minutes_recent"]) < self.t.concurrency_min_busy_minutes:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["INSUFFICIENT_BUSY_MINUTE_SAMPLE"],["warehouse_size"],[],[],
                "Queueing crosses the increase thresholds, but too few busy minutes support a peak-concurrency target."))
        else:
            # Pro/Classic: one move to the demand-derived ceiling (peak running + queued queries per cluster, plus
            # headroom). Serverless scales differently, so it keeps one step at a time (at low confidence).
            demand = max(cfg.min_clusters, safe_int(f["required_clusters_recent"]) + self.t.max_cluster_target_headroom_clusters)
            target = min(self.t.max_clusters_supported_limit, demand) if cfg.warehouse_type!="SERVERLESS" else cfg.max_clusters+1
            capped = cfg.warehouse_type!="SERVERLESS" and demand > self.t.max_clusters_supported_limit
            if target <= cfg.max_clusters:
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["QUEUEING_WITH_DEMAND_WITHIN_CEILING"],["warehouse_size"],[],[],
                    f"Queries queue, but peak demand ({f['peak_concurrency_recent']} concurrent queries) fits the current ceiling: the queueing is not caused by the ceiling."))
            else:
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","ACTIONABLE",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":target},["PEAK_DEMAND_ABOVE_CLUSTER_LIMIT"] if capped else [],["warehouse_size"],["min_clusters"],["min_clusters"],
                    ("Sustained queueing at the current ceiling; the ceiling moves in one step to cover peak demand." if cfg.warehouse_type!="SERVERLESS" else
                     "Sustained queueing plus observed saturation at the current ceiling indicates more concurrency capacity.")
                    + " " + MAX_INCREASE_COST_BOUNDS
                    + (f" Peak demand ({f['peak_concurrency_recent']} concurrent queries) needs {demand} clusters with headroom, above the {self.t.max_clusters_supported_limit}-cluster limit: queueing remains at the peak, and a larger size or splitting the workload is a human decision." if capped else "")))
    elif not window_known:
        # Without recent resource time neither saturation nor the unused ceiling can be judged.
        missing = f["resource_window_state"] == "MISSING"
        out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
            {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},[RESOURCE_WINDOW_BLOCKERS[f["resource_window_state"]]],["warehouse_size"],[],[],
            "The recent resource window was not collected (or has no settled day yet), so the ceiling is not changed." if missing else
            "The warehouse did not run in the recent window, so the ceiling is not changed."))
    else:
        peak = safe_int(f["max_observed"])
        epoch_peak = f.get("epoch_max_observed")
        demand_blocker: Optional[str] = None
        if cfg.warehouse_type!="SERVERLESS":
            # Pro/Classic: the ceiling is the demand-derived target; the whole current epoch's peak demand keeps it
            # for a seasonal peak outside the recent window.
            def target_for(clusters: Optional[int]) -> int:
                return min(self.t.max_clusters_supported_limit, max(cfg.min_clusters, safe_int(clusters) + self.t.max_cluster_target_headroom_clusters))
            if not f["concurrency_available"]:
                demand_blocker = "CONCURRENCY_EVIDENCE_UNAVAILABLE"
            elif safe_float(f["busy_minutes_recent"]) < self.t.concurrency_min_busy_minutes:
                demand_blocker = "INSUFFICIENT_BUSY_MINUTE_SAMPLE"
            recent_max = target_for(f["required_clusters_recent"])
            reduced_max = (target_for(max(safe_int(f["required_clusters_recent"]), safe_int(f["required_clusters_epoch"])))
                           if f.get("epoch_peak_outside_recent") else recent_max)
            epoch_peak_label = f"{f['peak_concurrency_epoch']} concurrent queries"
        else:
            def ceiling_for(level: int) -> int:
                return self._ceiling_for(level, cfg.min_clusters)
            # Seasonality: the ceiling covers the highest level of the whole current epoch, not only the recent
            # window, so a quarter- or half-year-end peak outside the last pressure_window_days is not cut away.
            recent_max = ceiling_for(peak)
            reduced_max = ceiling_for(max(peak, safe_int(epoch_peak)) if epoch_peak is not None else peak)
            epoch_peak_label = f"{safe_int(epoch_peak)} cluster(s)"
        cut_shape = (
            window_known and cfg.max_clusters > cfg.min_clusters
            and f["queries_30"] >= self.t.min_query_sample_size
            and f["at_max_pct"] <= self.t.time_at_max_pct_safe_reduce and peak > 0
        )
        base_eligible = cut_shape and not young
        # A cut the recent window would allow, held only because the configuration is younger than the stability period.
        # Queueing above the safe level would hold the cut at any age, so it is not an age hold.
        young_hold = (cut_shape and not base_eligible
                      and (demand_blocker is not None or recent_max < cfg.max_clusters)
                      and not (queue_known and f["queue_pct"] > self.t.queue_query_pct_safe))
        eligible = base_eligible and demand_blocker is None and reduced_max < cfg.max_clusters
        held_by_epoch_peak = base_eligible and demand_blocker is None and not eligible and recent_max < cfg.max_clusters
        # Missing evidence is named only when it is what holds an otherwise eligible cut: queueing must be known to
        # be low, and when queries did queue (within the safe level) their capacity wait must be known too.
        # A cut held for the epoch's peak still names these, so the holds stay visible together.
        considered = eligible or held_by_epoch_peak or young_hold
        evidence_blockers: List[str] = []
        if considered and not queue_known:
            evidence_blockers = ["QUEUE_EVIDENCE_UNAVAILABLE"]
        elif considered and 0 < f["queue_pct"] <= self.t.queue_query_pct_safe and f["wait_capacity"] is None:
            evidence_blockers = ["QUEUE_WAIT_EVIDENCE_UNAVAILABLE"]
        if considered and queue_known and f["queue_pct"] <= self.t.queue_query_pct_safe:
            evidence_blockers = evidence_blockers + failure_blockers(f["failure_pct"], self.t)
        if considered and self._growing(f):
            evidence_blockers = evidence_blockers + ["QUERY_VOLUME_GROWING"]
        if considered and f.get("older_epoch_demand_higher"):
            evidence_blockers = evidence_blockers + ["OLDER_EPOCH_PEAK_DEMAND_HIGHER"]
        if base_eligible and demand_blocker is not None:
            evidence_blockers = evidence_blockers + [demand_blocker]
        reduce_ok = eligible and queue_known and f["queue_pct"] <= self.t.queue_query_pct_safe and not evidence_blockers
        if reduce_ok:
            target=reduced_max
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","COST_GUARDRAIL","ACTIONABLE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":target},[],["warehouse_size"],[],[],
                "The configured ceiling has not been used and queue pressure is low; tighten the guardrail without claiming direct savings."))
        elif held_by_epoch_peak:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},evidence_blockers+["PEAK_OUTSIDE_RECENT_WINDOW"],["warehouse_size"],[],[],
                f"The recent window would allow a lower ceiling, but the current configuration has peaked at {epoch_peak_label}; the ceiling is kept for that peak."))
        elif (not evidence_blockers and not saturated and queue_known and f["queue_pct"] >= self.t.queue_query_pct_increase_max
              and not (f["wait_capacity"] is not None and f["wait_capacity"] < self.t.queue_wait_p95_ms_increase_max)):
            # Queueing the ceiling rule does not resolve: the warehouse rarely reaches its ceiling, so the queue
            # forms while clusters are still being added (scale-up lag on short bursts).
            demand = ""
            if cfg.warehouse_type!="SERVERLESS" and f["concurrency_available"] and f.get("peak_concurrency_recent") is not None:
                demand = f" Peak demand is {f['peak_concurrency_recent']} concurrent queries ({f['required_clusters_recent']} clusters)"
                demand += (f", and busy periods start with {f['busy_start_concurrency']} ({f['required_clusters_busy_start']} clusters) on a floor of {cfg.min_clusters}."
                           if f.get("busy_start_concurrency") is not None else ".")
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["QUEUEING_BELOW_CEILING"],["warehouse_size"],[],[],
                f"{f['queue_pct']:.1f}% of queries queued, but the warehouse was at its ceiling of {cfg.max_clusters} clusters only {f['at_max_pct']:.1f}% of the time.{demand}"
                " Clusters are added minutes after a burst starts (scale-up lag), so short bursts queue before the ceiling is reached;"
                " a higher floor would absorb burst starts at the cost of running those clusters all the time, which is a human decision."))
        elif young_hold:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},
                ["CURRENT_CONFIG_EPOCH_TOO_NEW"]+evidence_blockers+([demand_blocker] if demand_blocker else [])
                +(["PEAK_OUTSIDE_RECENT_WINDOW"] if demand_blocker is None and reduced_max >= cfg.max_clusters else []),["warehouse_size"],[],[],
                f"The current configuration is not ready yet ({billed_days} of {self.t.readiness_min_billed_days} settled billed days) to cut the ceiling; it is re-evaluated once it is."))
        else:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},evidence_blockers,["warehouse_size"],[],[],
                "Queue, failure or growth evidence holds the ceiling cut." if evidence_blockers else
                "No sustained saturation requiring an increase and no strong evidence for a lower ceiling."))


def decide_min_clusters(self: "Optimizer", e: WarehouseEvidence, analyses: Dict[str, AnalysisResult], cfg: WarehouseConfig, young: bool,
                        billed_days: int, out: List[OptimizationDecision]) -> None:
    """Append this knob's decision to ``out`` (``self`` is the Optimizer: thresholds and shared helpers)."""
    # Min clusters
    a=analyses["min_clusters"]; f=a.facts
    if cfg.min_clusters <= 1:
        out.append(OptimizationDecision(e.warehouse_id,"min_clusters","COST","NO_CHANGE",
            {"min_clusters":cfg.min_clusters},{"min_clusters":cfg.min_clusters},[],["warehouse_size","max_clusters"],[],[],"Minimum is already 1."))
    else:
        blockers=[]
        if young: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
        if f["queries_30"] < self.t.min_query_sample_size: blockers.append("INSUFFICIENT_QUERY_SAMPLE")
        if f["queue_pct"] is None: blockers.append("QUEUE_EVIDENCE_UNAVAILABLE")
        elif f["queue_pct"] > self.t.queue_query_pct_safe or (f["wait_capacity"] is not None and f["wait_capacity"] > self.t.queue_wait_p95_ms_safe): blockers.append("QUEUE_PRESSURE_PRESENT")
        if f["wait_capacity"] is None: blockers.append("QUEUE_WAIT_EVIDENCE_UNAVAILABLE")
        if f["at_max_pct"] is None or f["at_min_pct"] is None: blockers.append(RESOURCE_WINDOW_BLOCKERS.get(f["resource_window_state"], "RECENT_RESOURCE_WINDOW_UNAVAILABLE"))
        else:
            # A pinned warehouse (min = max) always runs at its maximum: fixed capacity, so pressure is judged on
            # queueing only.
            if f["at_max_pct"] >= self.t.time_at_max_pct_block_min_reduce and cfg.min_clusters < cfg.max_clusters: blockers.append("SUSTAINED_SCALE_PRESSURE")
            if f["at_min_pct"] < self.t.time_at_min_pct_reduce: blockers.append("WAREHOUSE_RARELY_AT_MINIMUM")
        blockers.extend(failure_blockers(f["failure_pct"], self.t))
        if self._growing(f): blockers.append("QUERY_VOLUME_GROWING")
        if f.get("older_epoch_demand_higher"): blockers.append("OLDER_EPOCH_PEAK_DEMAND_HIGHER")
        # The floor carries busy-period starts until the autoscaler adds clusters (scale-up lag): the smallest
        # floor covering the P95 busy-start demand, in one canary.
        floor_target = None
        if not f["concurrency_available"]:
            blockers.append("CONCURRENCY_EVIDENCE_UNAVAILABLE")
        elif safe_float(f["busy_periods_recent"]) < self.t.min_cluster_min_busy_periods:
            blockers.append("INSUFFICIENT_BUSY_PERIOD_SAMPLE")
        else:
            floor_target = max(1, safe_int(f["required_clusters_busy_start"], 1))
        if not blockers and floor_target is not None and floor_target < cfg.min_clusters:
            out.append(OptimizationDecision(e.warehouse_id,"min_clusters","COST","TEST_REQUIRED",
                {"min_clusters":cfg.min_clusters},{"min_clusters":floor_target},
                ["A_B_PERFORMANCE_VALIDATION_REQUIRED","SPARE_FLOOR_CAPACITY_UNPROVEN"],["warehouse_size","max_clusters"],
                [],[],
                f"Busy periods start with at most {f['busy_start_concurrency']} concurrent queries (P{self.t.min_cluster_busy_start_percentile:g}), which {floor_target} cluster(s) carry until the autoscaler adds more; the floor moves there in one canary and its savings are an upper-bound range."))
        elif not blockers:
            out.append(OptimizationDecision(e.warehouse_id,"min_clusters","COST","NO_CHANGE",
                {"min_clusters":cfg.min_clusters},{"min_clusters":cfg.min_clusters},[],["warehouse_size","max_clusters"],[],[],
                f"Busy periods start with {f['busy_start_concurrency']} concurrent queries (P{self.t.min_cluster_busy_start_percentile:g}), which needs the current floor."))
        else:
            out.append(OptimizationDecision(e.warehouse_id,"min_clusters","COST","NO_CHANGE",
                {"min_clusters":cfg.min_clusters},{"min_clusters":cfg.min_clusters},blockers,["warehouse_size","max_clusters"],[],[],
                "Minimum-cluster reduction is not sufficiently supported by current-epoch evidence."))


# Why a ceiling increase's best case is zero added cost, stated with the candidate and its pricing method.
MAX_INCREASE_COST_BOUNDS = ("Cost: the best case adds no cost, because the same queries run on more clusters at once and the "
                            "total running time does not grow; the worst case runs every added cluster whenever the warehouse "
                            "sat at its current ceiling.")
