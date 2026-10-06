"""Per-knob analysis: facts and findings only, never a recommendation."""
from __future__ import annotations

import datetime as dt
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

from publisher.analyzer.collector.queries.query_history import CANARY_GUARD_FIELDS, CONCURRENCY_SCOPES
from publisher.analyzer.epochs import (
    DEMAND_CARRY_FINDINGS,
    QUEUE_CARRY_FINDINGS,
    billed_days_recent,
    carried_query_view,
    epoch_age_days,
)
from publisher.analyzer.metrics import (
    auto_stop_candidates,
    conditional_severity,
    histogram_percentile,
    histogram_top_level,
    query_volume_growth_ratio,
    recent_capacity_facts,
    recent_peak_at_rate,
    resource_window,
    running_idle_rates,
    running_idle_window_days,
    size_busy_share,
    window_days,
)
from publisher.analyzer.normalizer import hive_reference_count
from publisher.common.ids import finding_id
from publisher.common.time import parse_datetime
from publisher.config.defaults import QUERIES_PER_CLUSTER_PRO_CLASSIC
from publisher.governor.cost_profiles import nonprod_tag_match
from publisher.common.utils import as_string_list, clamp, optional_float, round_or_none, safe_float, safe_int
from publisher.config.defaults import SIZE_ORDER
from publisher.governor.thresholds import Thresholds
from publisher.contracts.analysis import AnalysisResult, Finding
from publisher.contracts.source import WarehouseEvidence


def canary_observation(e: "WarehouseEvidence", t: Thresholds) -> Dict[str, Any]:
    """Fail-fast guard after a canary-type change (one size down or a lower minimum), within 30 days of it.

    Compares the current epoch with the immediately preceding one on the canary guard collector's rows. A guard trips when
    the current value is above its safe level AND more than canary_p95_regression_pct above the previous epoch's, with a
    minimum sample; the p95 guard compares execution time on statement sources seen in both epochs. Returns
    {"changes": {knob: previous value}, "guards": [codes], "details": {...}, "previous": {previous config}}; no change or
    no rows: no guards. Uses the immediately preceding epoch only, and only for the rollback decision.
    """
    cur = e.current_epoch
    prev = next((x for x in e.epochs if not x.is_current and x.epoch_end is not None
                 and parse_datetime(x.epoch_end) == parse_datetime(cur.epoch_start)), None)
    out: Dict[str, Any] = {"changes": {}, "guards": [], "details": {}, "previous": {}}
    # The guard covers the canary window: a change at most 30 days old.
    if prev is None or epoch_age_days(cur, e.analysis_time) > 30:
        return out
    pc, cc = prev.config, cur.config
    out["previous"] = {"warehouse_size": pc.warehouse_size, "min_clusters": pc.min_clusters, "max_clusters": pc.max_clusters,
                       "auto_stop_minutes": pc.auto_stop_minutes}
    if pc.warehouse_type == cc.warehouse_type:
        if pc.warehouse_size in SIZE_ORDER and cc.warehouse_size in SIZE_ORDER and SIZE_ORDER.index(cc.warehouse_size) < SIZE_ORDER.index(pc.warehouse_size):
            out["changes"]["warehouse_size"] = pc.warehouse_size
        if cc.min_clusters < pc.min_clusters:
            out["changes"]["min_clusters"] = pc.min_clusters
        # A higher auto-stop is not a canary here: it changes running time and cold starts only, which no guard reads.
    guard = cur.query.get("canary_guard") if isinstance(cur.query.get("canary_guard"), Mapping) else {}
    now, before = guard.get("CURRENT") or {}, guard.get("PREVIOUS") or {}
    if not out["changes"] or not now or not before:
        return out
    margin = 1 + t.canary_p95_regression_pct / 100.0
    def worse(field: str, safe: float, count_field: str, min_count: float) -> bool:
        """True when `field` is known in both epochs, above `safe`, clearly above the previous value, with at least `min_count` samples."""
        # Above the safe level AND clearly worse than before (the same relative margin as the p95 guard), so window
        # noise on a warehouse that already queued or failed does not roll a change back.
        a, b = optional_float(now.get(field)), optional_float(before.get(field))
        return a is not None and b is not None and a > safe and a > b * margin and safe_float(now.get(count_field)) >= min_count
    if worse("failure_cancel_pct", t.failure_cancel_pct_downsize_safe, "queries", t.min_query_sample_size):
        out["guards"].append("CANARY_FAILURES_ABOVE_SAFE")
    if worse("queued_query_pct", t.queue_query_pct_safe, "queued_queries", t.severity_min_affected_queries):
        out["guards"].append("CANARY_QUEUE_ABOVE_SAFE")
    if "warehouse_size" in out["changes"] and worse("spill_query_pct", t.spill_query_pct_downsize_safe, "spill_queries", t.severity_min_affected_queries):
        out["guards"].append("CANARY_SPILL_AFTER_DOWNSIZE")
    p_now, p_before = optional_float(now.get("p95_execution_ms_shared_sources")), optional_float(before.get("p95_execution_ms_shared_sources"))
    if (p_now is not None and p_before and safe_float(now.get("shared_source_queries")) >= t.canary_p95_min_queries
            and p_now > p_before * (1 + t.canary_p95_regression_pct / 100.0)):
        out["guards"].append("CANARY_P95_REGRESSION")
    counts = {"queries", "queued_queries", "decision_queries", "spill_queries", "shared_source_queries"}
    num = lambda k, v: (None if optional_float(v) is None else int(optional_float(v))) if k in counts else optional_float(v)
    out["details"] = {"current": {k: num(k, now.get(k)) for k in CANARY_GUARD_FIELDS},
                      "previous": {k: num(k, before.get(k)) for k in CANARY_GUARD_FIELDS}}
    return out


def canary_rationale(obs: Mapping[str, Any], t: Thresholds) -> str:
    """Human-readable "; "-joined reason for each tripped guard in a `canary_observation` result (empty when none tripped)."""
    now, before = obs["details"]["current"], obs["details"]["previous"]
    parts = []
    fmt = lambda v: "unknown" if v is None else f"{safe_float(v):.1f}"
    if "CANARY_FAILURES_ABOVE_SAFE" in obs["guards"]:
        parts.append(f"failed or cancelled queries {fmt(now.get('failure_cancel_pct'))}% (before {fmt(before.get('failure_cancel_pct'))}%, safe {t.failure_cancel_pct_downsize_safe:g}%)")
    if "CANARY_QUEUE_ABOVE_SAFE" in obs["guards"]:
        parts.append(f"queued queries {fmt(now.get('queued_query_pct'))}% (before {fmt(before.get('queued_query_pct'))}%, safe {t.queue_query_pct_safe:g}%)")
    if "CANARY_SPILL_AFTER_DOWNSIZE" in obs["guards"]:
        parts.append(f"spilling queries {fmt(now.get('spill_query_pct'))}% (before {fmt(before.get('spill_query_pct'))}%, safe {t.spill_query_pct_downsize_safe:g}%)")
    if "CANARY_P95_REGRESSION" in obs["guards"]:
        parts.append(f"p95 execution on the same statement sources {safe_float(now.get('p95_execution_ms_shared_sources')):,.0f} ms vs "
                     f"{safe_float(before.get('p95_execution_ms_shared_sources')):,.0f} ms before (more than +{t.canary_p95_regression_pct:g}%, "
                     f"{safe_int(now.get('shared_source_queries')):,} queries)")
    return "; ".join(parts)


class Analyzer:
    """Convert normalized warehouse evidence into per-knob facts and findings only."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def _f(self, e: WarehouseEvidence, knob: str, metric: str, value: Any, *, unit: str = "", threshold: Any = None, comparator: str = "", table: str, field: str, window: str, interpretation: str) -> Finding:
        """Build one Finding for the current epoch with its deterministic ID.

        Metrics read from carried (cross-change) evidence get their window rewritten and `epoch_scope`
        CARRIED_ACROSS_CHANGE, so a reader can tell current-epoch facts from carried ones. Reads `self._carry`, set by `analyze`.
        """
        ep = e.current_epoch
        scope = "CURRENT"
        periods = getattr(self, "_carry", None) or {}
        start = periods.get("demand") if metric in DEMAND_CARRY_FINDINGS else periods.get("capacity") if metric in QUEUE_CARRY_FINDINGS else None
        if start and window.startswith("last "):
            window = f"{window.replace(' within current epoch', '')} since {start[:10]} (carried across a configuration change that does not affect it)"
            scope = "CARRIED_ACROSS_CHANGE"
        return Finding(
            finding_id=finding_id(e.workspace_id, e.warehouse_id, knob, ep.epoch_id, metric),
            metric=metric, value=value, unit=unit, threshold=threshold, comparator=comparator,
            source_table=table, source_field=field, epoch_id=ep.epoch_id, window=window,
            interpretation=interpretation, epoch_scope=scope,
        )

    @staticmethod
    def _auto_stop_options(ri: Optional[Mapping[str, Any]], current_auto: int, values: Sequence[int], restart_cost: float,
                           budget: Optional[float]) -> List[Dict[str, Any]]:
        """Score auto-stop values on the running-idle rates (cluster-minutes per day on the forecast basis; lowering: idle
        removed minus restarts added; raising: restarts saved minus idle added, at the cluster count it stopped at), with
        each value's added cold-start share against the budget (None: no budget) on the blend and on the recent window."""
        if ri is None:
            return []
        queries, recent = ri["queries_per_day"], ri["recent"]
        options: List[Dict[str, Any]] = []
        for x in values:
            if x == current_auto:
                continue
            row = ri["by_target"].get(str(x)) or {}  # collected, no row: nothing removed, added or avoided at this value
            lower = current_auto == 0 or x < current_auto
            if lower:
                gain = row.get("removable_running_cluster_minutes", 0.0); starts = row.get("additional_starts", 0.0)
                loss = starts * restart_cost
            else:
                starts = -row.get("avoided_starts", 0.0)
                gain = -starts * restart_cost; loss = row.get("added_idle_cluster_minutes", 0.0)
            share = None if queries <= 0 else round(100.0 * starts / queries, 4)
            # The recent window alone (its own totals): a value must also fit the budget there, so a recent change in
            # demand cannot push it over (no recent check on the whole-period fallback).
            recent_starts = safe_int((recent["by_target"].get(str(x)) or {}).get("additional_starts")) if (lower and recent) else 0
            recent_share = None if not recent or recent["queries"] <= 0 else round(100.0 * recent_starts / recent["queries"], 4)
            within = budget is None or starts <= 0 or (share is not None and share <= budget and (recent_starts <= 0 or (recent_share is not None and recent_share <= budget)))
            options.append({"target": x, "direction": "LOWER" if lower else "RAISE", "gain_cluster_minutes_per_day": round(gain, 6),
                            "loss_cluster_minutes_per_day": round(loss, 6), "net_cluster_minutes_per_day": round(gain - loss, 6),
                            "added_starts_per_day": round(starts, 6), "removable_wall_minutes_per_day": round(row.get("removable_running_wall_minutes", 0.0), 6) if lower else 0.0,
                            "added_cold_start_share_pct": share, "recent_added_cold_start_share_pct": recent_share if lower else None,
                            "within_cold_start_budget": within})
        return options

    def analyze(self, e: WarehouseEvidence) -> Dict[str, AnalysisResult]:
        """Facts and findings per knob (warehouse_size, max_clusters, min_clusters, auto_stop_minutes, warehouse_type) for one warehouse.

        Decides nothing: the Optimizer reads these facts. Missing evidence stays None (unknown), never zero, so
        capacity cuts fail closed on it. Current-epoch evidence controls; the exceptions are carried demand/queue
        evidence, the older-epoch peak-concurrency hold and the canary guard's previous epoch. Not re-entrant: it
        sets `self._carry` for the duration of the call.
        """
        ep = e.current_epoch
        ev, gaps, cfg = ep.events, ep.gaps, ep.config
        q, self._carry = carried_query_view(ep.query)
        # The recent query window starts at the demand period when evidence is carried, else at the epoch start.
        recent_from = self._carry["demand"] or ep.epoch_start
        q30 = safe_float(q.get("queries_30"))
        active_days = max(1.0, safe_float(q.get("active_days_30")))
        per_day = q30 / active_days
        starts_per_day = safe_float(ev.get("starts_30")) / active_days
        age = epoch_age_days(ep, e.analysis_time)
        common = {
            "queries_30": q30,
            "active_days_30": active_days,
            "queries_per_active_day_30": per_day,
            "epoch_age_days": age,
            "epoch_queries": safe_float(q.get("epoch_queries")),
            "epoch_cost_days": safe_float(ep.cost.get("cost_days")),
            "cost_history_days": e.cost_history_days,
            "cost_cv_84": e.cost_cv_84,
        }
        out: Dict[str, AnalysisResult] = {}

        # Size
        # Task-time utilization: query task-minutes per running cluster-minute over the whole current epoch (both terms
        # over the same period). Evidence only: no decision uses it until it is calibrated on real data.
        task_ms = optional_float(q.get("epoch_task_duration_ms")); epoch_cluster_minutes = safe_float(ev.get("cluster_minutes"))
        utilization = None if task_ms is None or epoch_cluster_minutes <= 0 else round(task_ms / 60000.0 / epoch_cluster_minutes, 6)

        def utilization_finding(knob: str) -> Finding:
            return self._f(e,knob,"task_minutes_per_cluster_minute",utilization,unit="ratio",table="system.query.history",field="total_task_duration_ms",
                window="current epoch",interpretation="Query task time per running cluster-minute (evidence only; not used in decisions until calibrated on real data).")

        # Missing pressure evidence stays None (unknown), never 0 ("no pressure"): capacity cuts fail closed on it.
        spill_pct = optional_float(q.get("spill_query_pct_30")); spill_p95, spill_n = conditional_severity(q, "spill_queries_30", "p95_spill_bytes_when_spilled_30")
        failure_pct = optional_float(q.get("failure_cancel_pct_30"))
        common["failure_pct"] = failure_pct  # reliability guard shared by every capacity cut
        common["canary"] = canary_observation(e, self.t)  # fail-fast guard after a canary-type change
        growth_ratio = query_volume_growth_ratio([q.get(f"queries_week_{k}") for k in range(1, 9)])
        common["growth_ratio"] = growth_ratio  # growth guard shared by every capacity cut
        # Older-epoch hold (the one use of older epochs in decisions): peak demand of each older epoch of this
        # warehouse within the last 365 days (level reached in peak_demand_min_minutes busy minutes) against the recent
        # window's peak demand, both in clusters at the documented queries per cluster. Holds capacity cuts only.
        cur_hist = q.get("concurrency_histogram") if isinstance(q.get("concurrency_histogram"), Mapping) else {}
        # Like with like: each older epoch's sustained peak (level reached in peak_demand_min_minutes busy minutes)
        # against the recent window's peak read at the same rate for that epoch's length; quiet now never hides a
        # busier past (a quiet recent window counts at its highest level, or 0).
        horizon = parse_datetime(e.analysis_time) - dt.timedelta(days=365)
        qpc_all = QUERIES_PER_CLUSTER_PRO_CLASSIC
        older_peak: Optional[int] = None; recent_peak: Optional[int] = None; held = False
        cur_recent_days = min(30.0, window_days(recent_from, None, e.analysis_time))
        # With carried evidence the recent window reaches back across the change: an older epoch wholly inside it is
        # part of the recent window, not an older peak.
        recent_start = max(parse_datetime(recent_from), parse_datetime(e.analysis_time) - dt.timedelta(days=30)) if self._carry["demand"] else None
        for x in e.epochs:
            if x.is_current or not isinstance(x.query.get("concurrency_histogram"), Mapping) or (x.epoch_end is not None and parse_datetime(x.epoch_end) <= horizon):
                continue
            if recent_start is not None and parse_datetime(x.epoch_start) >= recent_start:
                continue
            peak = histogram_top_level(x.query["concurrency_histogram"].get("EPOCH"), self.t.peak_demand_min_minutes)
            recent_at = recent_peak_at_rate(cur_hist.get("RECENT"), window_days(x.epoch_start, x.epoch_end, e.analysis_time), self.t, cur_recent_days)
            if peak is None or recent_at is None:
                continue
            holds = math.ceil(peak / qpc_all) > math.ceil(recent_at / qpc_all)
            # Report the epoch that holds (the largest such peak), else the largest older peak.
            if (holds and (not held or peak > safe_int(older_peak))) or (not held and not holds and (older_peak is None or peak > older_peak)):
                older_peak, recent_peak = peak, recent_at
            held = held or holds
        common["older_epoch_peak_concurrency"] = older_peak
        common["older_epoch_demand_higher"] = held

        def older_epoch_finding(knob: str) -> Finding:
            return self._f(e,knob,"older_epoch_peak_concurrency",older_peak,unit="queries",threshold=recent_peak,comparator="more clusters than the recent peak -> capacity cuts held",table="system.query.history",field="start_time/end_time",
                window="older epochs within the last 365 days",interpretation="Peak running + queued queries of an earlier configuration of this warehouse (level reached in the peak-demand minutes); when it needs more clusters than the recent window's peak, capacity cuts wait. Older epochs never choose a change, its target or its economics.")

        def growth_finding(knob: str) -> Finding:
            return self._f(e,knob,"query_volume_growth_ratio",growth_ratio,unit="ratio",threshold=self.t.growth_hold_weekly_ratio,comparator=">= capacity-cut hold",table="system.query.history",field="start_time",
                window="last 8 complete weeks within current epoch",interpretation="Smallest of the last 4 weekly query counts over the median of the 4 weeks before; at or above the threshold, capacity cuts wait (unknown when the epoch has fewer than 8 complete weeks).")
        queue_pct = optional_float(q.get("queued_query_pct_30")); p95 = optional_float(q.get("p95_execution_ms_30")); p99 = optional_float(q.get("p99_execution_ms_30"))
        fs = [
            self._f(e,"warehouse_size","warehouse_size",cfg.warehouse_size,table="system.compute.warehouses",field="warehouse_size",window="current epoch",interpretation="Current warehouse size."),
            self._f(e,"warehouse_size","spill_query_pct_30",round_or_none(spill_pct,4),unit="%",threshold=self.t.spill_query_pct_upsize,comparator=">= upsize signal",table="system.query.history",field="spilled_local_bytes",window="last 30 days within current epoch",interpretation="Frequency of queries spilling to local storage."),
            self._f(e,"warehouse_size","spill_queries_30",spill_n,unit="queries",threshold=self.t.severity_min_affected_queries,comparator=">= evidence minimum for upsize",table="system.query.history",field="spilled_local_bytes",window="last 30 days within current epoch",interpretation="Queries that spilled; the spill severity percentile is computed over these queries only."),
            self._f(e,"warehouse_size","p95_spill_bytes_when_spilled_30",round_or_none(spill_p95),unit="bytes",threshold=self.t.spill_p95_bytes_upsize,comparator=">= upsize signal",table="system.query.history",field="spilled_local_bytes",window="last 30 days within current epoch",interpretation="Severity of spill at p95 among queries that spilled."),
            self._f(e,"warehouse_size","queued_query_pct_30",round_or_none(queue_pct,4),unit="%",threshold=self.t.queue_query_pct_safe,comparator="<= downsize-safe",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Concurrency pressure during the decision window."),
            self._f(e,"warehouse_size","failure_cancel_pct_30",round_or_none(failure_pct,4),unit="%",threshold=self.t.failure_cancel_pct_downsize_safe,comparator="<= downsize-safe",table="system.query.history",field="execution_status",window="last 30 days within current epoch",interpretation="Failed or cancelled query share; a high rate undermines the no-pressure evidence a downsize relies on."),
            self._f(e,"warehouse_size","p95_execution_ms_30",round_or_none(p95),unit="ms",threshold=self.t.p95_execution_ms_downsize_safe,comparator="<= downsize-safe",table="system.query.history",field="execution_duration_ms",window="last 30 days within current epoch",interpretation="Tail execution latency guardrail."),
            self._f(e,"warehouse_size","p99_execution_ms_30",round_or_none(p99),unit="ms",threshold=self.t.p99_execution_ms_downsize_safe,comparator="<= downsize-safe",table="system.query.history",field="execution_duration_ms",window="last 30 days within current epoch",interpretation="Extreme tail execution latency guardrail."),
            self._f(e,"warehouse_size","avg_read_io_cache_percent_30",round_or_none(optional_float(q.get("avg_read_io_cache_percent_30")),4),unit="%",table="system.query.history",field="read_io_cache_percent",window="last 30 days within current epoch",interpretation="Share of bytes read served from the I/O cache (weighted by bytes read); HITL context only, not a size decision threshold."),
        ]
        decision_queries = optional_float(q.get("decision_queries_30"))
        fs.extend([
            self._f(e,"warehouse_size","decision_queries_30",round_or_none(decision_queries),unit="queries",threshold=self.t.strong_query_sample_size,comparator=">= downsize sample",table="system.query.history",field="from_result_cache",window="last 30 days within current epoch",interpretation="Queries not served from the result cache: the population latency and spill are measured on."),
            self._f(e,"warehouse_size","p95_compilation_ms_30",round_or_none(optional_float(q.get("p95_compilation_ms_30"))),unit="ms",table="system.query.history",field="compilation_duration_ms",window="last 30 days within current epoch",interpretation="Metadata loading and optimization time; a size change does not shorten it (evidence only)."),
            self._f(e,"warehouse_size","p95_result_fetch_ms_30",round_or_none(optional_float(q.get("p95_result_fetch_ms_30"))),unit="ms",table="system.query.history",field="result_fetch_duration_ms",window="last 30 days within current epoch",interpretation="Time clients spend fetching results after execution; a size change does not shorten it (evidence only)."),
        ])
        fs.append(utilization_finding("warehouse_size")); fs.append(growth_finding("warehouse_size")); fs.append(older_epoch_finding("warehouse_size"))
        # Busy share of running cluster time (whole epoch, same period as the event totals): a size change speeds up or
        # slows down query execution, not the idle running time around it.
        busy_share = size_busy_share(gaps.get("busy_minutes"), ev.get("uptime_minutes"), ev.get("cluster_minutes"), cfg.min_clusters)
        fs.append(self._f(e,"warehouse_size","busy_cluster_time_share",busy_share,unit="ratio",table="system.query.history + system.compute.warehouse_events",field="start_time/end_time, cluster_count",window="current epoch",interpretation="Share of running cluster-minutes with queries executing; the size runtime multiplier applies to this share only (unknown: all running time is treated as busy)."))
        top_spill_share = optional_float(q.get("top_spill_source_share_pct_30")); spill_sources = optional_float(q.get("spill_sources_30"))
        fs.append(self._f(e,"warehouse_size","top_spill_source_share_pct_30",round_or_none(top_spill_share,3),unit="%",threshold=self.t.spill_concentration_hold_pct,comparator=">= rewrite, not upsize",table="system.query.history",field="spilled_local_bytes by statement source",window="last 30 days within current epoch",
            interpretation=f"Share of spilled bytes from the top {self.t.spill_concentration_top_sources} statement sources (saved query, dashboard, job, notebook or statement hash); the statement text is not exported."))
        job_q = optional_float(q.get("job_queries_30"))
        job_share = None if job_q is None or q30 <= 0 else round(100.0 * job_q / q30, 3)
        batch_profile = job_share is not None and job_share >= self.t.batch_job_query_share_pct
        fs.append(self._f(e,"warehouse_size","job_query_share_30",job_share,unit="%",threshold=self.t.batch_job_query_share_pct,comparator=">= batch profile",table="system.query.history",field="query_source.job_info.job_id",window="last 30 days within current epoch",
            interpretation="Share of queries from jobs; a batch warehouse's downsize canary is judged on run days and job run time, not interactive latency."))
        out["warehouse_size"] = AnalysisResult(e.warehouse_id,"warehouse_size",ep.epoch_id,fs,{**common,"batch_profile":batch_profile,"job_share":job_share,"top_spill_share":top_spill_share,"spill_sources":None if spill_sources is None else int(spill_sources),"decision_queries":decision_queries,"spill_pct":spill_pct,"spill_p95":spill_p95,"spill_n":spill_n,"queue_pct":queue_pct,"failure_pct":failure_pct,"p95_exec":p95,"p99_exec":p99,"busy_share":busy_share})

        # Concurrency demand: per-minute running + queued queries (time-weighted), as clusters at the documented
        # queries per cluster. P99.5 of busy minutes over the recent window and the whole current epoch; P95 of busy
        # periods' peak demand in their first scale-up-lag minutes (before the autoscaler can add clusters).
        hist = q.get("concurrency_histogram") if isinstance(q.get("concurrency_histogram"), Mapping) else None
        qpc = QUERIES_PER_CLUSTER_PRO_CLASSIC
        def clusters_for(level: Optional[int]) -> Optional[int]:
            return None if level is None else max(1, math.ceil(level / qpc))
        conc_recent = histogram_percentile((hist or {}).get("RECENT"), self.t.max_cluster_target_percentile)
        conc_epoch = histogram_top_level((hist or {}).get("EPOCH"), self.t.peak_demand_min_minutes)
        # The recent window's peak at the same rate as the whole epoch's, for the seasonal comparison.
        recent_days = min(30.0, window_days(recent_from, None, e.analysis_time))
        recent_covers_epoch = self._carry["demand"] is not None and window_days(ep.epoch_start, None, e.analysis_time) <= recent_days
        conc_recent_sustained = recent_peak_at_rate((hist or {}).get("RECENT"), window_days(ep.epoch_start, None, e.analysis_time), self.t, recent_days)
        conc_busy_start = histogram_percentile((hist or {}).get("BUSY_START"), self.t.min_cluster_busy_start_percentile)
        busy_minutes_recent = sum(safe_float(v) for v in ((hist or {}).get("RECENT") or {}).values()) if hist else None
        busy_periods = sum(safe_float(v) for v in ((hist or {}).get("BUSY_START") or {}).values()) if hist else None
        concurrency = {"concurrency_available": hist is not None and all(sc in hist for sc in CONCURRENCY_SCOPES),
                       "peak_concurrency_recent": conc_recent, "peak_concurrency_epoch": conc_epoch, "busy_start_concurrency": conc_busy_start,
                       "required_clusters_recent": clusters_for(conc_recent), "required_clusters_epoch": clusters_for(conc_epoch),
                       "required_clusters_busy_start": clusters_for(conc_busy_start),
                       # A seasonal peak outside the recent window: the epoch's sustained peak needs more clusters than the
                       # recent window's sustained peak (same statistic on both sides).
                       # When carried evidence makes the recent window cover the whole epoch, no epoch peak lies outside it.
                       "epoch_peak_outside_recent": (conc_epoch is not None and conc_recent_sustained is not None and not recent_covers_epoch
                                                     and math.ceil(conc_epoch / qpc) > math.ceil(conc_recent_sustained / qpc)), "busy_minutes_recent": busy_minutes_recent, "busy_periods_recent": busy_periods,
                       "recent_concurrency_histogram": (hist or {}).get("RECENT")}
        def concurrency_findings(knob: str) -> List[Finding]:
            return [
                self._f(e,knob,"peak_concurrency_recent",conc_recent,unit="queries",threshold=self.t.max_cluster_target_percentile,comparator="percentile of busy minutes",table="system.query.history",field="start_time/end_time",window="last 30 days within current epoch",interpretation=f"P{self.t.max_cluster_target_percentile:g} of running + queued queries per busy minute; one cluster per {qpc} concurrent queries on Pro/Classic."),
                self._f(e,knob,"peak_concurrency_epoch",conc_epoch,unit="queries",threshold=self.t.max_cluster_target_percentile,comparator="percentile of busy minutes",table="system.query.history",field="start_time/end_time",window="current epoch (last 365 days)",interpretation="The same peak over the whole current epoch, so a seasonal peak outside the recent window keeps the ceiling."),
                self._f(e,knob,"busy_start_concurrency",conc_busy_start,unit="queries",threshold=self.t.min_cluster_busy_start_percentile,comparator="percentile of busy periods",table="system.query.history",field="start_time/end_time",window="last 30 days within current epoch",interpretation="Peak running + queued queries in the first scale-up-lag minutes of busy periods: demand the floor must carry before the autoscaler adds clusters."),
                self._f(e,knob,"busy_minutes_recent",None if busy_minutes_recent is None else int(busy_minutes_recent),unit="minutes",threshold=self.t.concurrency_min_busy_minutes,comparator=">= evidence minimum",table="system.query.history",field="start_time/end_time",window="last 30 days within current epoch",interpretation="Minutes with at least one query running or queued: the sample the peak percentile is taken over."),
                self._f(e,knob,"busy_periods_recent",None if busy_periods is None else int(busy_periods),unit="periods",threshold=self.t.min_cluster_min_busy_periods,comparator=">= evidence minimum",table="system.query.history",field="start_time/end_time",window="last 30 days within current epoch",interpretation="Busy periods starting after at least the scale-up lag of idle time: the sample the floor percentile is taken over."),
            ]

        # Max clusters
        # Capacity facts come from the recent resource window (last pressure_window_days of the DBU forecast's calendar),
        # not from whole-epoch totals; unknown when the daily series was not collected.
        recent = recent_capacity_facts(resource_window(e, self.t), self.t)
        recent_label = "recent resource window unavailable" if recent["window_days"] is None else f"last {recent['window_days']} complete days within current epoch"
        wait, queue_n = conditional_severity(q, "queued_queries_30", "p95_wait_capacity_ms_when_queued_30"); max_obs = recent["max_observed"]; at_max = recent["at_max_pct"]
        fs = [
            self._f(e,"max_clusters","max_clusters",cfg.max_clusters,table="system.compute.warehouses",field="max_clusters",window="current epoch",interpretation="Configured concurrency ceiling."),
            self._f(e,"max_clusters","max_observed_clusters",max_obs,unit="clusters",table="system.compute.warehouse_events",field="cluster_count",window=recent_label,interpretation="Highest observed scale level, excluding cluster-recycling transients above the ceiling."),
            self._f(e,"max_clusters","time_at_max_pct",round_or_none(at_max,4),unit="%",threshold=self.t.time_at_max_pct_safe_reduce,comparator="<= reduce-safe",table="system.compute.warehouse_events",field="cluster_count",window=recent_label,interpretation="Fraction of running time at configured maximum."),
            self._f(e,"max_clusters","queued_query_pct_30",round_or_none(queue_pct,4),unit="%",threshold=self.t.queue_query_pct_increase_max,comparator=">= increase signal",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Frequency of capacity queueing."),
            self._f(e,"max_clusters","failure_cancel_pct_30",round_or_none(common["failure_pct"],4),unit="%",threshold=self.t.failure_cancel_pct_downsize_safe,comparator="<= cut-safe",table="system.query.history",field="execution_status",window="last 30 days within current epoch",interpretation="Failed or cancelled query share; a high or unknown rate holds every capacity cut."),
            self._f(e,"max_clusters","queued_queries_30",queue_n,unit="queries",threshold=self.t.severity_min_affected_queries,comparator=">= evidence minimum for increase",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Queries that queued at capacity; the wait percentile is computed over these queries only."),
            self._f(e,"max_clusters","p95_wait_capacity_ms_when_queued_30",round_or_none(wait),unit="ms",threshold=self.t.queue_wait_p95_ms_increase_max,comparator=">= increase signal",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Severity of capacity queueing at p95 among queued queries."),
        ]
        # Longer window: the highest scale level over the whole current epoch (events retained 365 days), excluding
        # recycling transients above the ceiling. A seasonal peak outside the recent window shows here; unknown on
        # evidence collected without it.
        epoch_peak_raw = optional_float(ev.get("max_observed_clusters_within_ceiling"))
        epoch_peak = None if epoch_peak_raw is None else int(epoch_peak_raw)
        fs.append(growth_finding("max_clusters"))
        fs.append(self._f(e,"max_clusters","epoch_max_observed_clusters",epoch_peak,unit="clusters",table="system.compute.warehouse_events",field="cluster_count",window="current epoch (events retained 365 days)",interpretation="Highest scale level the current configuration has run at, excluding cluster-recycling transients above the ceiling; the ceiling is never cut below it."))
        fs.extend(concurrency_findings("max_clusters")); fs.append(older_epoch_finding("max_clusters"))
        out["max_clusters"] = AnalysisResult(e.warehouse_id,"max_clusters",ep.epoch_id,fs,{**common,**concurrency,"queue_pct":queue_pct,"queue_n":queue_n,"wait_capacity":wait,"max_observed":max_obs,"epoch_max_observed":epoch_peak,"at_max_pct":at_max,"resource_window_state":recent["state"],
            # The increase is priced on the same recent window it is decided on.
            "minutes_at_max":safe_float(recent["minutes_at_max"]),"cluster_minutes":safe_float(recent["cluster_minutes"])})

        # Min clusters
        at_min = recent["at_min_pct"]
        fs = [
            self._f(e,"min_clusters","min_clusters",cfg.min_clusters,table="system.compute.warehouses",field="min_clusters",window="current epoch",interpretation="Configured minimum scale."),
            self._f(e,"min_clusters","time_at_min_pct",round_or_none(at_min,4),unit="%",threshold=self.t.time_at_min_pct_reduce,comparator=">= reduction signal",table="system.compute.warehouse_events",field="cluster_count",window=recent_label,interpretation="How often the warehouse sits at its minimum."),
            self._f(e,"min_clusters","queued_query_pct_30",round_or_none(queue_pct,4),unit="%",threshold=self.t.queue_query_pct_safe,comparator="<= safe",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Queue guardrail before reducing baseline capacity."),
            self._f(e,"min_clusters","failure_cancel_pct_30",round_or_none(common["failure_pct"],4),unit="%",threshold=self.t.failure_cancel_pct_downsize_safe,comparator="<= cut-safe",table="system.query.history",field="execution_status",window="last 30 days within current epoch",interpretation="Failed or cancelled query share; a high or unknown rate holds every capacity cut."),
            self._f(e,"min_clusters","p95_wait_capacity_ms_when_queued_30",round_or_none(wait),unit="ms",threshold=self.t.queue_wait_p95_ms_safe,comparator="<= safe",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Queue latency guardrail among queued queries."),
            self._f(e,"min_clusters","time_at_max_pct",round_or_none(at_max,4),unit="%",threshold=self.t.time_at_max_pct_block_min_reduce,comparator=">= blocker",table="system.compute.warehouse_events",field="cluster_count",window=recent_label,interpretation="Sustained scale pressure blocks minimum reduction."),
        ]
        fs.append(utilization_finding("min_clusters")); fs.append(growth_finding("min_clusters")); fs.extend(concurrency_findings("min_clusters")); fs.append(older_epoch_finding("min_clusters"))
        out["min_clusters"] = AnalysisResult(e.warehouse_id,"min_clusters",ep.epoch_id,fs,{**common,**concurrency,"queue_pct":queue_pct,"wait_capacity":wait,"at_min_pct":at_min,"at_max_pct":at_max,"resource_window_state":recent["state"],
            # Whole-epoch totals on purpose: the min-cluster savings ratio and the bundle's resource track are both whole-epoch.
            "minutes_at_min":safe_float(ev.get("minutes_at_min")),"cluster_minutes":safe_float(ev.get("cluster_minutes"))})

        # Auto stop: every value on the type's grid, scored on running-idle evidence (idle gaps intersected with the
        # warehouse's observed RUNNING time) on the DBU forecast's own days, as per-day rates blended like the forecast.
        # Lowering removes running idle time and adds the restarts of gaps the warehouse used to run through; raising
        # adds idle time and saves the restarts of gaps shorter than the new value. A restart costs restart-cost
        # cluster-minutes per floor cluster.
        current_auto = cfg.auto_stop_minutes
        serverless = cfg.warehouse_type == "SERVERLESS"
        grid = auto_stop_candidates(self.t, cfg.warehouse_type)
        floor_clusters = max(1, cfg.min_clusters)
        restart_cost = (self.t.restart_cost_cluster_minutes_serverless if serverless else self.t.restart_cost_cluster_minutes_pro_classic) * floor_clusters
        nonprod = nonprod_tag_match(ep.tags, self.t) is not None
        budget = None if serverless else self.t.cold_start_added_share_max_pct
        ri = running_idle_rates(e, self.t)
        running_available = ri is not None
        options = self._auto_stop_options(ri, current_auto, grid, restart_cost, budget)
        meta = e.forecast_metadata or {}
        # The basis is priced only on the forecast's own days; the whole-period fallback serves decisions alone.
        priced_basis = ri is not None and ri["model"] in ("RUN_RATE", "CADENCE")
        trend = safe_float(meta.get("trend_adjustment_factor"), 1.0) if ri is not None and ri["model"] == "RUN_RATE" else 1.0
        if ri is None:
            window = "DBU forecast basis (running-idle evidence not collected)"
        elif ri["model"] == "RUN_RATE":
            days = {label: running_idle_window_days({"window_start": a, "window_end": b}) for label, (a, b) in ri["windows"].items()}
            window = f"DBU forecast basis: last {days['long']:g} days ending {ri['windows']['long'][1][:10]}, blended with the last {days['recent']:g}"
        elif ri["model"] == "CADENCE":
            window = f"DBU forecast basis: whole billing cycles {ri['windows']['cycles'][0][:10]} to {ri['windows']['cycles'][1][:10]}"
        else:
            window = "current epoch since the first retained warehouse event (no forecast basis: decisions only, not priced)"
        running_per_day = None if ri is None else ri["running_cluster_minutes_per_day"]
        queries_basis = None if ri is None else ri["basis_queries"]
        uptime = optional_float(ev.get("uptime_minutes"))
        running_clock_pct = None if ri is None else round(min(100.0, 100.0 * ri["running_wall_minutes_per_day"] / 1440.0), 3)
        busy_minutes = optional_float(gaps.get("busy_minutes"))
        busy_pct = None if not uptime or busy_minutes is None else round(100.0 * min(busy_minutes, uptime) / uptime, 3)
        kept_alive_pct = None if not ri or not ri["running_wall_minutes_per_day"] else round(100.0 * ri["kept_alive_wall_minutes_per_day"] / ri["running_wall_minutes_per_day"], 3)
        # Billing alone: on how many of the last pressure-window settled days of the current epoch the warehouse was
        # billed (it ran), and what those days cost. Used only when no retained warehouse event gives running intervals.
        billed_recent = billed_days_recent(e, self.t.pressure_window_days)
        positive = [o for o in options if o["net_cluster_minutes_per_day"] > 0]
        best_any = max(positive, key=lambda o: (o["net_cluster_minutes_per_day"], -o["target"]), default=None)
        best_ok = max((o for o in positive if o["within_cold_start_budget"]), key=lambda o: (o["net_cluster_minutes_per_day"], -o["target"]), default=None)
        # Annual reclaimable cluster-hours per saving value within the budget: per-day rates x 365 x the forecast trend.
        annual = lambda per_day: round(per_day * 365.0 * trend / 60.0, 1)
        hours_by_value = None if not priced_basis else {str(o["target"]): {
            "net_cluster_hours": annual(o["net_cluster_minutes_per_day"]),
            "idle_cluster_hours": annual(o["gain_cluster_minutes_per_day"] if o["direction"] == "LOWER" else -o["loss_cluster_minutes_per_day"]),
            "restart_cluster_hours": annual(-o["loss_cluster_minutes_per_day"] if o["direction"] == "LOWER" else o["gain_cluster_minutes_per_day"]),
            "added_starts": round(o["added_starts_per_day"] * 365.0 * trend, 1)} for o in positive if o["within_cold_start_budget"]}
        cost_per_hour = (None if not priced_basis or e.baseline_forecast_cost_365 is None or not running_per_day
                         else round(e.baseline_forecast_cost_365 / (running_per_day * 365.0 * trend / 60.0), 4))
        fs = [
            self._f(e,"auto_stop_minutes","auto_stop_minutes",current_auto,unit="minutes",table="system.compute.warehouses",field="auto_stop_minutes",window="current epoch",interpretation="Current idle termination setting."),
            self._f(e,"auto_stop_minutes","failure_cancel_pct_30",round_or_none(common["failure_pct"],4),unit="%",threshold=self.t.failure_cancel_pct_downsize_safe,comparator="<= cut-safe",table="system.query.history",field="execution_status",window="last 30 days within current epoch",interpretation="Failed or cancelled query share; a high or unknown rate holds every capacity cut."),
            self._f(e,"auto_stop_minutes","running_idle_basis",None if ri is None else {"model":ri["model"],"windows":ri["windows"],"blend_weights_per_day":ri["weights"],"recent_check_window":ri["check_window"],"trend_adjustment_factor":trend},unit="windows",table="system.billing.usage + system.compute.warehouse_events",field="forecast basis",window=window,interpretation="The days auto-stop evidence is measured on: the DBU forecast's own (whole weeks, the recent/long blend and trend, or whole billing cycles), so a saving annualizes like the baseline it is a share of."),
            self._f(e,"auto_stop_minutes","basis_running_cluster_minutes",None if ri is None else round(ri["basis_running_cluster_minutes"],2),unit="cluster-minutes",threshold=self.t.min_cluster_minutes_for_cost_model,comparator=">= evidence minimum",table="system.compute.warehouse_events",field="cluster_count/event_time",window=window,interpretation="Running cluster-minutes in the basis window: the running time idle savings are a share of."),
            self._f(e,"auto_stop_minutes","running_cluster_minutes_per_day",running_per_day,unit="cluster-minutes per day",table="system.compute.warehouse_events",field="cluster_count/event_time",window=window,interpretation="Running cluster-minutes per day, blended like the DBU forecast."),
            self._f(e,"auto_stop_minutes","basis_queries",queries_basis,unit="queries",table="system.query.history",field="start_time",window=window,interpretation="Queries in the basis window; added restarts are judged as a share of the queries (blended, and in the recent window alone)."),
            self._f(e,"auto_stop_minutes","kept_alive_wall_minutes_per_day",None if ri is None else ri["kept_alive_wall_minutes_per_day"],unit="minutes per day",table="system.query.history + system.compute.warehouse_events",field="end_time, cluster_count",window=window,interpretation="Running time after the current auto-stop should have stopped the warehouse: something other than queries (open sessions, result fetching) kept it running, so no auto-stop value removes it."),
            self._f(e,"auto_stop_minutes","running_share_of_clock_pct",running_clock_pct,unit="%",threshold=self.t.always_on_running_pct,comparator=">= always-on",table="system.compute.warehouse_events",field="cluster_count",window=window,interpretation="Share of the day the warehouse was running."),
            self._f(e,"auto_stop_minutes","busy_share_of_running_pct",busy_pct,unit="%",threshold=self.t.always_on_busy_pct_max,comparator="<= kept up by tiny queries",table="system.query.history",field="start_time/end_time",window="current epoch since the first retained warehouse event",interpretation="Share of running time with a query running; a round-the-clock warehouse with a small share is kept up by frequent tiny queries (BI polling, keep-alive, dashboard refresh)."),
            self._f(e,"auto_stop_minutes","restart_cost_cluster_minutes",round(restart_cost,3),unit="cluster-minutes per restart",table="policy",field="restart cost",window="policy",interpretation="Priced cost of one restart (start time x floor clusters); Pro/Classic starting minutes are assumed billed (not documented)."),
            self._f(e,"auto_stop_minutes","nonprod_profile",nonprod,unit="state",table="system.compute.warehouses",field="tags",window="current epoch",interpretation="Nonprod by warehouse tag (environment key with a dev/test value): selects the aggressive cost profile unless --cost-profile overrides it."),
            self._f(e,"auto_stop_minutes","auto_stop_values_evaluated",[grid[0],grid[-1],self.t.auto_stop_step_minutes] if grid else None,unit="first, last, step (minutes)",table="policy",field="auto-stop search",window="policy",interpretation="Auto-stop values scored: every step from the type's floor to the maximum; a value covers a gap only when it is the margin longer than the gap."),
            self._f(e,"auto_stop_minutes","auto_stop_best_net_option",best_any,unit="per value, per day",threshold=budget,comparator="added cold-start share <= budget (%)",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,interpretation="The auto-stop value with the largest net saving before the cold-start budget: idle cluster-minutes removed or added, restarts added or avoided, net cluster-minutes saved per day and the added cold-start share (blended and in the recent window)."),
            self._f(e,"auto_stop_minutes","billed_days_of_settled_days_recent",None if billed_recent is None else [billed_recent["billed_days"],billed_recent["settled_days"]],unit="billed days, settled days",table="system.billing.usage",field="usage_date/usage_quantity",window=f"last {self.t.pressure_window_days} settled days within current epoch",interpretation="Days the warehouse was billed (ran) among the settled days; billed every day with no query and no retained warehouse event is a review item."),
            self._f(e,"auto_stop_minutes","auto_stop_best_option_within_budget",best_ok,unit="per value, per day",threshold=budget,comparator="added cold-start share <= budget (%)",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,interpretation="The auto-stop value with the largest net saving among those within the cold-start budget."),
            self._f(e,"auto_stop_minutes","auto_stop_annual_cluster_hours_by_value",hours_by_value,unit="cluster-hours per year",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,interpretation="Per auto-stop value that saves within the cold-start budget: reclaimable cluster-hours a year net of restarts, the idle cluster-hours removed (or added) and the restart cluster-hours added (or saved), and the restarts added a year; per-day rates x 365 x the forecast trend."),
            self._f(e,"auto_stop_minutes","cost_per_cluster_hour",cost_per_hour,unit=f"{e.currency} per cluster-hour",table="system.billing.usage + system.compute.warehouse_events",field="forecast cost / running cluster-hours",window=window,interpretation="Forward DBU cost per running cluster-hour on the same days: the price reclaimable cluster-hours are saved at (DBU baseline / annualized running cluster-hours)."),
        ]
        out["auto_stop_minutes"] = AnalysisResult(e.warehouse_id,"auto_stop_minutes",ep.epoch_id,fs,{**common,"running_available":running_available,"options":options,"grid":grid,
            "running_idle_model":None if ri is None else ri["model"],"priced_basis":priced_basis,"trend_adjustment_factor":trend,
            "running_cluster_minutes_per_day":running_per_day,"basis_running_cluster_minutes":None if ri is None else ri["basis_running_cluster_minutes"],
            "basis_queries":queries_basis,"floor_clusters":floor_clusters,
            "restart_cost_cluster_minutes":restart_cost,"cold_start_budget_pct":budget,
            "kept_alive_pct":kept_alive_pct,"running_clock_pct":running_clock_pct,"busy_pct":busy_pct,"client_applications":as_string_list(q.get("client_applications_30")) or [],
            "billed_recent":billed_recent,"cost_per_cluster_hour":cost_per_hour,
            "running_restarts_per_day":None if ri is None else ri["restarts_per_day"]})

        # Type/serverless
        serverless_observed = bool(e.billing_features.get("workspace_serverless_observed_from_system_compute_warehouses")) or safe_float(e.billing_features.get("serverless_usage_pct_365")) > 0
        pro_classic_observed = bool(e.billing_features.get("workspace_pro_classic_observed_from_system_compute_warehouses"))
        fs = [
            self._f(e,"warehouse_type","warehouse_type",cfg.warehouse_type,table="system.compute.warehouses",field="warehouse_type",window="current epoch",interpretation="Current warehouse type."),
            self._f(e,"warehouse_type","billed_sql_tiers",e.billing_features.get("current_epoch_sql_tiers"),unit="state",table="system.billing.usage",field="product_features.sql_tier",window="current epoch",interpretation="Billed SQL tier beside the configured type (evidence only; not used in decisions)."),
            self._f(e,"warehouse_type","serverless_usage_pct_365",round(safe_float(e.billing_features.get("serverless_usage_pct_365")),4),unit="%",table="system.billing.usage",field="product_features.is_serverless",window="trailing 365 days",interpretation="Observed billing evidence; not used as current configuration source."),
            self._f(e,"warehouse_type","tagged_query_pct_30",round(safe_float(q.get("tagged_query_pct_30")),4),unit="%",table="system.query.history",field="query_tags",window="last 30 days within current epoch",interpretation="Query-tag coverage supplied to HITL for workload attribution and migration validation."),
            self._f(e,"warehouse_type","dashboard_queries_30",safe_int(q.get("dashboard_queries_30")),unit="queries",table="system.query.history",field="query_source.dashboard_id",window="last 30 days within current epoch",interpretation="Dashboard workload mix context."),
            self._f(e,"warehouse_type","job_queries_30",safe_int(q.get("job_queries_30")),unit="queries",table="system.query.history",field="query_source.job_info.job_id",window="last 30 days within current epoch",interpretation="Job workload mix context."),
            self._f(e,"warehouse_type","notebook_queries_30",safe_int(q.get("notebook_queries_30")),unit="queries",table="system.query.history",field="query_source.notebook_id",window="last 30 days within current epoch",interpretation="Notebook workload mix context."),
            self._f(e,"warehouse_type","genie_queries_30",safe_int(q.get("genie_queries_30")),unit="queries",table="system.query.history",field="query_source.genie_space_id",window="last 30 days within current epoch",interpretation="Genie workload mix context."),
            self._f(e,"warehouse_type","alert_queries_30",safe_int(q.get("alert_queries_30")),unit="queries",table="system.query.history",field="query_source.alert_id",window="last 30 days within current epoch",interpretation="Alert workload mix context."),
            self._f(e,"warehouse_type","sql_editor_queries_30",safe_int(q.get("sql_query_source_queries_30")),unit="queries",table="system.query.history",field="query_source.sql_query_id",window="last 30 days within current epoch",interpretation="Databricks SQL editor/query workload mix context."),
            self._f(e,"warehouse_type","power_bi_queries_30",safe_int(q.get("power_bi_queries_30")),unit="queries",table="system.query.history",field="client_application",window="last 30 days within current epoch",interpretation="Power BI client workload mix context."),
            self._f(e,"warehouse_type","tableau_queries_30",safe_int(q.get("tableau_queries_30")),unit="queries",table="system.query.history",field="client_application",window="last 30 days within current epoch",interpretation="Tableau client workload mix context."),
            self._f(e,"warehouse_type","statement_text_visible_pct_30",round(safe_float(q.get("statement_text_visible_pct_30")),4),unit="%",table="system.query.history",field="statement_text",window="last 30 days within current epoch",interpretation="Coverage available for deterministic pattern checks. Raw SQL text is not exported."),
            self._f(e,"warehouse_type","hive_metastore_reference_queries_30",hive_reference_count(q),unit="queries",table="system.query.history",field="statement_text (aggregated pattern only)",window="last 30 days within current epoch",interpretation="Potential migration-review signal only; this does not prove an external legacy Hive metastore is configured."),
        ]
        # The migration target runs at its own best auto-stop (Serverless: 5-minute floor, starts in seconds, no
        # cold-start budget; Pro: 10-minute floor, 4-minute starts, the cold-start budget), measured on the same
        # running-idle evidence. Its cost basis is explicit: today's running cluster-minutes, minus idle it removes (or
        # plus idle it adds), plus every restart it still has at its own restart cost, against today's running time plus
        # today's restarts at today's cost.
        target_type = "PRO" if serverless else "SERVERLESS"
        target_values = auto_stop_candidates(self.t, target_type)
        per_start_current = restart_cost
        per_start_target = (self.t.restart_cost_cluster_minutes_serverless if target_type == "SERVERLESS" else self.t.restart_cost_cluster_minutes_pro_classic) * floor_clusters
        target_budget = None if target_type == "SERVERLESS" else self.t.cold_start_added_share_max_pct
        observed_running = running_per_day  # per day on the forecast basis
        current_restarts = None if ri is None else ri["restarts_per_day"]
        target_option: Optional[Dict[str, Any]] = None
        if running_available and observed_running and current_restarts is not None:
            def target_running(o: Mapping[str, Any]) -> float:
                if o["direction"] == "LOWER":
                    idle_delta = -o["gain_cluster_minutes_per_day"]
                elif o["direction"] == "RAISE":
                    idle_delta = o["loss_cluster_minutes_per_day"]
                else:
                    idle_delta = 0.0
                restarts = current_restarts + o["added_starts_per_day"]  # negative for restarts a raise avoids
                return observed_running + idle_delta + restarts * per_start_target
            # Restart costs are applied in target_running, so the options are scored on idle time only here.
            options_t = self._auto_stop_options(ri, current_auto, target_values, 0.0, target_budget)
            keep = {"target": current_auto, "direction": "KEEP", "gain_cluster_minutes_per_day": 0.0, "loss_cluster_minutes_per_day": 0.0, "added_starts_per_day": 0.0}
            current_valid = current_auto == 0 or current_auto >= target_values[0]
            # Only values within the target's cold-start budget (a raise never adds restarts, so it always qualifies).
            pool = [o for o in options_t if o["within_cold_start_budget"]] + ([keep] if current_valid else [])
            if pool:
                best = min(pool, key=lambda o: (target_running(o), o["target"]))
                target_option = {**best, "target_running_cluster_minutes_per_day": round(target_running(best), 6)}
        # Measured running time excludes starting time (restarts are priced on top, as for auto-stop), so today's basis
        # carries today's restarts at today's cost and the target's carries its own.
        running_fraction = (None if target_option is None
                            else round(clamp(target_option["target_running_cluster_minutes_per_day"] / (observed_running + current_restarts * per_start_current), 0.0, self.t.cost_factor_ceiling), 6))
        fs.append(self._f(e,"warehouse_type","migration_target_auto_stop",None if target_option is None else target_option["target"],unit="minutes",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,
            interpretation=f"Auto-stop the {target_type.title()} alternative is priced at: its own cost-minimizing value on the measured idle gaps."))
        fs.append(self._f(e,"warehouse_type","migration_target_running_fraction",running_fraction,unit="ratio",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,
            interpretation="The alternative's running cluster-minutes as a share of today's: its own auto-stop removes or adds idle time, and its restarts cost its own start time."))
        out["warehouse_type"] = AnalysisResult(e.warehouse_id,"warehouse_type",ep.epoch_id,fs,{**common,"warehouse_type":cfg.warehouse_type,
            "migration_target_option":target_option,"migration_running_fraction":running_fraction,"busy_share":busy_share,"serverless_observed":serverless_observed,"pro_classic_observed":pro_classic_observed,"statement_text_visible_pct_30":safe_float(q.get("statement_text_visible_pct_30")),"hive_metastore_reference_queries_30":hive_reference_count(q)})

        # Canary guard evidence: the values a rollback rests on are findings of the size and cluster knobs, so the
        # package cites them and they enter the evidence fingerprint.
        canary = common["canary"]
        if canary.get("details"):
            now, before = canary["details"]["current"], canary["details"]["previous"]
            window = "the current epoch since the change (last 30 days) against the previous configuration's last 30 days"
            specs = [("canary_failure_cancel_pct", "failure_cancel_pct", "%", f"rollback above previous x {1 + self.t.canary_p95_regression_pct / 100:g} and above {self.t.failure_cancel_pct_downsize_safe:g}%"),
                     ("canary_queued_query_pct", "queued_query_pct", "%", f"rollback above previous x {1 + self.t.canary_p95_regression_pct / 100:g} and above {self.t.queue_query_pct_safe:g}%"),
                     ("canary_p95_execution_ms_shared_sources", "p95_execution_ms_shared_sources", "ms", f"rollback above previous x {1 + self.t.canary_p95_regression_pct / 100:g}"),
                     ("canary_shared_source_queries", "shared_source_queries", "queries", f"current (value) vs previous (threshold); the p95 guard needs >= {self.t.canary_p95_min_queries} current")]
            if "warehouse_size" in canary["changes"]:
                specs.append(("canary_spill_query_pct", "spill_query_pct", "%", f"rollback above previous x {1 + self.t.canary_p95_regression_pct / 100:g} and above {self.t.spill_query_pct_downsize_safe:g}%"))
            for knob in ("warehouse_size", "max_clusters", "min_clusters"):
                for metric, field_name, unit, comparator in specs:
                    out[knob].findings.append(self._f(e, knob, metric, now.get(field_name), unit=unit, threshold=before.get(field_name),
                        comparator=comparator, table="system.query.history", field=field_name, window=window,
                        interpretation="Fail-fast canary guard after a canary-type change: the current value (value) against the previous configuration's (threshold)."))
        return out
