"""Configuration epochs: age, observed days, evidence carried across a change, and settled billed days."""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, Mapping, Optional, Tuple

from publisher.common.time import parse_datetime
from publisher.common.utils import safe_float
from publisher.contracts.source import EpochEvidence, WarehouseEvidence


def billed_days_recent(e: "WarehouseEvidence", window_days: int) -> Optional[Dict[str, Any]]:
    """Billed days among the last `window_days` settled days of the current epoch's forecast basis, with their cost.

    None when the basis is unknown. A day counts as billed when it has positive DBU usage; cost is None when any of
    those days carries unpriced usage (unknown, never understated).
    """
    meta = e.forecast_metadata or {}
    start, end = parse_datetime(meta.get("forecast_basis_start")), parse_datetime(meta.get("forecast_basis_end"))
    if not start or not end or end < start:
        return None
    epoch_start = parse_datetime(e.current_epoch.epoch_start)
    first = max(start.date(), end.date() - dt.timedelta(days=window_days - 1),
                epoch_start.astimezone(dt.timezone.utc).date() if epoch_start else start.date())
    dbu: Dict[str, float] = {}; cost = 0.0; unpriced = False
    current_start = e.current_epoch.epoch_start
    for r in e.daily_cost_365:
        day = parse_datetime(r.get("usage_date"))
        if not day or not first <= day.date() <= end.date():
            continue
        if r.get("epoch_start") is not None and parse_datetime(r.get("epoch_start")) != parse_datetime(current_start):
            continue  # an older epoch's usage on the current epoch's first day
        key = day.date().isoformat()
        dbu[key] = dbu.get(key, 0.0) + safe_float(r.get("dbu_quantity")) + safe_float(r.get("unpriced_dbu_quantity"))
        cost += safe_float(r.get("list_cost")); unpriced = unpriced or safe_float(r.get("unpriced_dbu_quantity")) > 0
    if first > end.date():
        return {"settled_days": 0, "billed_days": 0, "billed_cost": None, "first_day": first.isoformat(), "last_day": end.date().isoformat()}
    return {"settled_days": (end.date() - first).days + 1, "billed_days": sum(1 for v in dbu.values() if v > 1e-12),
            "billed_cost": None if unpriced else round(cost, 2), "first_day": first.isoformat(), "last_day": end.date().isoformat()}


def ready_billed_days(e: "WarehouseEvidence") -> int:
    """Settled billed days of the current epoch's forecast basis: the readiness count (0 when the basis is unknown)."""
    billed = billed_days_recent(e, 100000)
    return 0 if billed is None else int(billed["billed_days"])


def epoch_age_days(epoch: EpochEvidence, as_of: Optional[str] = None) -> int:
    start = parse_datetime(epoch.epoch_start)
    if not start:
        return 0
    end = parse_datetime(epoch.epoch_end) or parse_datetime(as_of)
    if not end:
        raise ValueError("Current epoch age requires a deterministic analysis_time")
    return max(0, (end - start).days)


def observed_epoch_calendar_days(epoch_start: str, epoch_end: Optional[str], analysis_time: str) -> int:
    start = parse_datetime(epoch_start)
    end = parse_datetime(epoch_end) or parse_datetime(analysis_time)
    analysis = parse_datetime(analysis_time)
    if not start or not end or not analysis:
        return 0
    analysis_date = analysis.astimezone(dt.timezone.utc).date()
    start_date = max(start.astimezone(dt.timezone.utc).date(), analysis_date - dt.timedelta(days=365))
    end_date = min(end.astimezone(dt.timezone.utc).date(), analysis_date - dt.timedelta(days=1))
    return max(0, (end_date - start_date).days + 1) if end_date >= start_date else 0


# Query evidence a configuration change cannot affect (LLD 10.2). Demand and per-query fields carry
# across auto-stop, min- and max-cluster changes; capacity queueing carries across auto-stop changes only. Cold-start
# wait (compute wait, total latency) changes with auto-stop and never carries.
DEMAND_CARRY_FIELDS = (
    "queries_30", "active_days_30", "decision_queries_30", "p95_execution_ms_30", "p99_execution_ms_30", "max_execution_ms_30",
    "p95_compilation_ms_30", "p95_result_fetch_ms_30", "spill_query_pct_30", "spill_queries_30", "p95_spill_bytes_when_spilled_30",
    "total_spill_gib_30", "p95_read_bytes_30", "avg_read_io_cache_percent_30", "failure_cancel_pct_30", "result_cache_pct_30",
    "tagged_query_pct_30", "dashboard_queries_30", "job_queries_30", "notebook_queries_30", "genie_queries_30", "alert_queries_30",
    "sql_query_source_queries_30", "power_bi_queries_30", "tableau_queries_30", "looker_queries_30",
    "databricks_sql_editor_client_queries_30", "client_applications_30", "statement_text_visible_pct_30",
    "hive_metastore_reference_queries_30", "spill_sources_30", "top_spill_source_share_pct_30",
)  # weekly counts reach 56 days back, beyond the 30-day carry: the growth guard stays current-epoch


QUEUE_CARRY_FIELDS = ("queued_queries_30", "queued_query_pct_30", "p95_wait_capacity_ms_when_queued_30")


DEMAND_CARRY_FINDINGS = frozenset({
    "spill_query_pct_30", "spill_queries_30", "p95_spill_bytes_when_spilled_30", "failure_cancel_pct_30", "p95_execution_ms_30",
    "p99_execution_ms_30", "avg_read_io_cache_percent_30", "decision_queries_30", "p95_compilation_ms_30", "p95_result_fetch_ms_30",
    "top_spill_source_share_pct_30", "job_query_share_30", "peak_concurrency_recent", "sql_editor_queries_30",
    "busy_start_concurrency", "busy_minutes_recent", "busy_periods_recent",
}) | frozenset(DEMAND_CARRY_FIELDS)


QUEUE_CARRY_FINDINGS = frozenset({"queued_query_pct_30", "queued_queries_30", "p95_wait_capacity_ms_when_queued_30"})


def carried_query_view(query: Mapping[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Optional[str]]]:
    """The query evidence the decisions read: the current epoch's, with carried demand and capacity-queue fields laid over
    it when the collector read them across an earlier change that cannot affect them. Returns (view, period starts)."""
    carried = query.get("carried") if isinstance(query.get("carried"), Mapping) else {}
    demand = carried.get("demand") if isinstance(carried.get("demand"), Mapping) else None
    capacity = carried.get("capacity") if isinstance(carried.get("capacity"), Mapping) else None
    view = dict(query)
    if demand and demand.get("period_start"):
        for k in DEMAND_CARRY_FIELDS:
            if k in demand:
                view[k] = demand[k]
        hist = dict(query.get("concurrency_histogram") or {}) if isinstance(query.get("concurrency_histogram"), Mapping) else None
        if hist is not None:
            carried_hist = demand.get("concurrency_histogram") if isinstance(demand.get("concurrency_histogram"), Mapping) else {}
            for scope in ("RECENT", "BUSY_START"):
                hist[scope] = dict(carried_hist.get(scope) or {})  # collected over the carried window; absent = no busy minute
            view["concurrency_histogram"] = hist
    if capacity and capacity.get("period_start"):
        for k in QUEUE_CARRY_FIELDS:
            if k in capacity:
                view[k] = capacity[k]
    start = lambda part: str(part["period_start"]) if part and part.get("period_start") else None
    return view, {"demand": start(demand), "capacity": start(capacity)}
