"""Synthetic collector for --demo: one simulated warehouse per golden scenario, run through the real collector.

Everything here is deterministic (hash-derived noise, no global RNG, fixed analysis time) and fabricated: owners, IDs and
prices are placeholders, and no Databricks API or system table is touched. Not used for real runs.
"""
from __future__ import annotations

import bisect
import datetime as dt
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.collector.collector import extract_evidence
from publisher.analyzer.collector.pricing import load_aws_price_registry, model_aws_baseline_cost
from publisher.analyzer.collector.queries.events import RESOURCE_DAILY_COLLECT_DAYS, running_idle_request_from_statement, running_idle_windows_from_statement
from publisher.analyzer.collector.queries.query_history import canary_guard_periods_from_statement
from publisher.analyzer.epochs import (
    DEMAND_CARRY_FIELDS,
    QUEUE_CARRY_FIELDS,
    carried_query_view,
    observed_epoch_calendar_days,
)
from publisher.analyzer.forecast import Modeler
from publisher.analyzer.metrics import RUNNING_IDLE_WINDOW_FIELDS, auto_stop_targets, concurrency_histograms, running_idle_basis, running_idle_metrics
from publisher.analyzer.normalizer import derive_query_source_profile
from publisher.common.ids import epoch_id
from publisher.common.logging import LOGGER
from publisher.common.time import parse_datetime
from publisher.common.utils import safe_float, safe_int
from publisher.config.defaults import AWS_SQL_WAREHOUSE_TOPOLOGY, GIB, QUERIES_PER_CLUSTER_PRO_CLASSIC, SQL_DBU_PER_CLUSTER_HOUR, scale_up_lag_minutes
from publisher.config.settings import REPO_ROOT
from publisher.governor.thresholds import Thresholds
from publisher.contracts.source import (
    EpochEvidence,
    PhotonObservation,
    WarehouseAttribution,
    WarehouseConfig,
    WarehouseEvidence,
)

# The default policy the demo collector simulates with (the collector itself only reads unprofiled values).
_DEMO_THRESHOLDS = Thresholds()


def _demo_daily(start: dt.date, days: int, total: float, workspace_id: str, warehouse_id: str, epoch_start: str, dbu_total: float) -> List[Dict[str, Any]]:
    """``days`` daily cost rows from ``start`` with a fixed weekly shape, scaled so costs sum to ``total`` and DBU to ``dbu_total``."""
    base = total / days
    rows: List[Dict[str, Any]] = []
    for i in range(days):
        factor = [0.90, 0.95, 1.00, 1.05, 1.10, 0.85, 1.15][i % 7]
        rows.append({
            "workspace_id": workspace_id,
            "warehouse_id": warehouse_id,
            "epoch_start": epoch_start,
            "usage_date": (start + dt.timedelta(days=i)).isoformat(),
            "currency_code": "USD",
            "list_cost": base * factor,
        })
    scale = total / sum(r["list_cost"] for r in rows)
    for r in rows:
        r["list_cost"] = round(r["list_cost"] * scale, 6)
        r["dbu_quantity"] = round(r["list_cost"] * dbu_total / total, 6)
    return rows


def synthesize_resource_daily(epoch: EpochEvidence, analysis_time: str) -> List[Dict[str, Any]]:
    """Synthetic/fixture daily resource series: the epoch's whole-period event totals spread evenly over its observed
    days, covering what the collector query returns (the last RESOURCE_DAILY_COLLECT_DAYS UTC days before the analysis date). For demo and test
    fixtures only; real runs collect the series."""
    ev = epoch.events
    days_observed = observed_epoch_calendar_days(epoch.epoch_start, epoch.epoch_end, analysis_time)
    start, analysis = parse_datetime(epoch.epoch_start), parse_datetime(analysis_time)
    if days_observed <= 0 or start is None or analysis is None:
        return []
    analysis_day = analysis.astimezone(dt.timezone.utc).date()
    first = max(start.astimezone(dt.timezone.utc).date(), analysis_day - dt.timedelta(days=RESOURCE_DAILY_COLLECT_DAYS))
    uptime = safe_float(ev.get("uptime_minutes"))
    at_min = safe_float(ev.get("minutes_at_min"), uptime * safe_float(ev.get("time_at_min_pct")) / 100.0)
    at_max = safe_float(ev.get("minutes_at_max"), uptime * safe_float(ev.get("time_at_max_pct")) / 100.0)
    per_day = {"uptime_minutes": uptime, "cluster_minutes": safe_float(ev.get("cluster_minutes")),
               "minutes_at_min": at_min, "minutes_at_max": at_max, "recycle_minutes": 0.0}
    floor_demand = synthesize_floor_demand(epoch, at_min / days_observed)
    return [{
        "usage_day": (first + dt.timedelta(days=i)).isoformat(),
        **{k: round(v / days_observed, 6) for k, v in per_day.items()},
        "max_cluster_count": safe_int(ev.get("max_observed_clusters")),
        "starts": 0,
        **({"floor_minutes_by_demand_clusters": floor_demand} if floor_demand is not None else {}),
    } for i in range((analysis_day - first).days)]


def synthesize_floor_demand(epoch: EpochEvidence, minutes_at_min: float) -> Optional[Dict[str, float]]:
    """Fixture floor-demand map for a day with ``minutes_at_min`` at the floor: idle time (running minus busy minutes, as a
    share of running time) is demand 0, and the busy rest follows the recent concurrency histogram's demand clusters
    (ceil(level / QUERIES_PER_CLUSTER_PRO_CLASSIC)); demand at or above the floor is dropped. None when the epoch has no
    running time or no recent histogram. For demo and test fixtures only; real runs collect the map."""
    floor = safe_int(epoch.config.min_clusters, 1)
    uptime = safe_float(epoch.events.get("uptime_minutes"))
    hist = (epoch.query.get("concurrency_histogram") or {}).get("RECENT") if isinstance(epoch.query.get("concurrency_histogram"), Mapping) else None
    if uptime <= 0 or not isinstance(hist, Mapping) or not hist:
        return None
    busy_share = min(1.0, max(0.0, safe_float(epoch.gaps.get("busy_minutes")) / uptime))
    total = sum(safe_float(w) for w in hist.values())
    out: Dict[str, float] = {"0": minutes_at_min * (1.0 - busy_share)}
    for level, weight in hist.items():
        need = int(math.ceil(safe_int(level) / QUERIES_PER_CLUSTER_PRO_CLASSIC))
        if need < floor and total > 0:
            out[str(need)] = out.get(str(need), 0.0) + minutes_at_min * busy_share * safe_float(weight) / total
    return {k: round(v, 6) for k, v in sorted(out.items(), key=lambda kv: int(kv[0])) if v > 0}


def _demo_interp(x: float, points: Sequence[Tuple[float, float, int]]) -> Tuple[float, int]:
    """Linear interpolation of (value, count) between (x, value, count) points (core demo evidence only)."""
    for (x0, v0, n0), (x1, v1, n1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            w = (x - x0) / (x1 - x0)
            return round(v0 + (v1 - v0) * w, 3), int(round(n0 + (n1 - n0) * w))
    raise ValueError(f"{x} outside the interpolation points")


def demo_evidence() -> List[WarehouseEvidence]:
    """The hand-built single-warehouse evidence fixture ("BI - Sales", two epochs, PRO), used by docs and tests.

    Unlike ``synthetic_demo_evidence`` it does not go through the collector; values are set by hand.
    """
    workspace = "1234567890123456"
    wh = "wh-demo-001"
    analysis_time = "2026-10-01T12:00:00+00:00"
    old_start = "2025-10-01T00:00:00+00:00"
    cur_start = "2026-01-15T00:00:00+00:00"

    old = EpochEvidence(
        epoch_id=epoch_id(workspace, wh, old_start),
        warehouse_id=wh, workspace_id=workspace, account_id="acct-demo", warehouse_name="BI - Sales",
        epoch_start=old_start, epoch_end=cur_start, is_current=False,
        config=WarehouseConfig("PRO", "CURRENT", "MEDIUM", 2, 4, 120),
        tags={"environment": "production"},
        query={
            "epoch_queries": 30000, "epoch_active_days": 100, "epoch_p95_execution_ms": 3300,
            "epoch_queued_query_pct": 0.1, "epoch_spill_query_pct": 0.0,
            "concurrency_histogram": {"EPOCH": {"1": 5940.0, "8": 54.0, "20": 6.0}},
        },
        events={
            "uptime_minutes": 8000.0, "cluster_minutes": 12000.0, "minutes_at_min": 6000.0,
            "minutes_at_max": 0.0, "time_at_min_pct": 75.0, "time_at_max_pct": 0.0,
            "max_observed_clusters": 2, "max_observed_clusters_within_ceiling": 2, "event_coverage_start": old_start,
        },
        gaps={},
        cost={"list_cost": 2640.0, "dbu_quantity": 4800.0, "cost_days": 106, "active_cost_days": 106, "currency": "USD"},
    )

    cur = EpochEvidence(
        epoch_id=epoch_id(workspace, wh, cur_start),
        warehouse_id=wh, workspace_id=workspace, account_id="acct-demo", warehouse_name="BI - Sales",
        epoch_start=cur_start, epoch_end=None, is_current=True,
        config=WarehouseConfig("PRO", "CURRENT", "LARGE", 2, 4, 120),
        tags={"environment": "production", "team": "sales-analytics"},
        query={
            "epoch_queries": 110000, "epoch_active_days": 250,
            "queries_30": 12000, "active_days_30": 30,
            "p95_total_ms_30": 3200, "compute_wait_queries_30": 8, "p95_wait_compute_ms_when_waited_30": 4000,
            "queued_queries_30": 0, "p95_wait_capacity_ms_when_queued_30": None,
            "p95_execution_ms_30": 2500, "p99_execution_ms_30": 10000, "max_execution_ms_30": 220000,
            "queued_query_pct_30": 0.0, "spill_query_pct_30": 0.0, "spill_queries_30": 0, "p95_spill_bytes_when_spilled_30": None,
            "total_spill_gib_30": 0.0, "p95_read_bytes_30": 20 * 1024**2,
            "avg_read_io_cache_percent_30": 42.0, "failure_cancel_pct_30": 0.2,
            "result_cache_pct_30": 14.0, "tagged_query_pct_30": 78.0,
            "dashboard_queries_30": 7000, "job_queries_30": 3000, "notebook_queries_30": 1000,
            "genie_queries_30": 500, "alert_queries_30": 300, "sql_query_source_queries_30": 200,
            "power_bi_queries_30": 4200, "tableau_queries_30": 1200, "looker_queries_30": 0,
            "databricks_sql_editor_client_queries_30": 400,
            "client_applications_30": ["Power BI", "Tableau", "Databricks SQL Editor"],
            "statement_text_visible_pct_30": 100.0, "hive_metastore_reference_queries_30": 0,
            "epoch_p95_execution_ms": 2700, "epoch_queued_query_pct": 0.0, "epoch_spill_query_pct": 0.0,
            "decision_queries_30": 10320, "p95_compilation_ms_30": 420, "p95_result_fetch_ms_30": 900,
            "spill_sources_30": 0, "top_spill_source_share_pct_30": 0.0,
            # Running + queued queries per busy minute: P99.5 = 20 (2 clusters); busy periods start at <= 10 (1 cluster).
            "concurrency_histogram": {"RECENT": {"1": 1500.0, "6": 260.0, "15": 12.0, "20": 30.0},
                                      "EPOCH": {"1": 12500.0, "6": 2300.0, "15": 100.0, "20": 100.0},
                                      "BUSY_START": {"4": 20.0, "10": 8.0}},
        },
        events={
            "uptime_minutes": 20000.0, "cluster_minutes": 30000.0, "minutes_at_min": 16000.0,
            "minutes_at_max": 0.0, "time_at_min_pct": 80.0, "time_at_max_pct": 0.0,
            "max_observed_clusters": 2, "max_observed_clusters_within_ceiling": 2, "starts_30": 8, "scale_ups_30": 3, "scale_downs_30": 3,
            "event_coverage_start": cur_start, "resource_coverage_from": None,
        },
        gaps={"gap_count": 480, "p50_gap_minutes": 8.0, "p90_gap_minutes": 95.0},
        cost={"list_cost": 11000.0, "dbu_quantity": 20000.0, "cost_days": 259, "active_cost_days": 259, "currency": "USD"},
    )

    daily = (
        # Billed DBU = cluster-hours x published DBU/hour (MEDIUM 24, LARGE 40); cost = DBU x Pro list price 0.55.
        _demo_daily(dt.date(2025, 10, 1), 106, 2640.0, workspace, wh, old_start, 4800.0)
        + _demo_daily(dt.date(2026, 1, 15), 259, 11000.0, workspace, wh, cur_start, 20000.0)
    )
    model = Modeler(_DEMO_THRESHOLDS).forecast_from_daily_cost(daily, cur_start, analysis_time, billing_watermark="2026-10-01T06:00:00+00:00")
    forecast = {k: v for k, v in model.items() if k not in {"current_cost_365", "forecast_cost_365", "currency"}}
    registry=load_aws_price_registry(str(REPO_ROOT / "pricing" / "aws_ec2_on_demand.json"))
    # Running-idle evidence over the whole epoch, spread over the forecast's windows: idle running time at the minimum
    # (2 clusters) removed by each auto-stop value and the restarts it adds, linear between the measured points,
    # nothing at or above the current 120 minutes.
    basis = running_idle_basis(cur_start, cur_start, forecast, _DEMO_THRESHOLDS)
    cur.gaps.update(running_idle_model=basis["model"], running_idle_windows=scale_running_idle_windows({
        "queries": 110000, "running_segment_count": 1000, "observed_running_cluster_minutes": 30000.0, "observed_running_wall_minutes": 20000.0,
        "kept_alive_wall_minutes": 0.0, "kept_alive_gap_count": 0, "restarts_for_queries": 900,
        "running_idle_by_target": {str(t): {"removable_running_wall_minutes": wall, "removable_running_cluster_minutes": 2 * wall, "additional_starts": starts,
                                            "avoided_starts": 0, "added_idle_wall_minutes": 0.0, "added_idle_cluster_minutes": 0.0}
                                   for t in range(5, 120, 5)
                                   for wall, starts in [_demo_interp(t, ((5, 13000.0, 300), (10, 12500.0, 210), (15, 11500.0, 170), (20, 10500.0, 150),
                                                                        (30, 9000.0, 120), (45, 6500.0, 90), (60, 5000.0, 70), (90, 2500.0, 40), (120, 0.0, 0)))]},
    }, basis, (cur_start, analysis_time)))
    cur.resource_daily=synthesize_resource_daily(cur,analysis_time)
    aws_cost,aws_model=model_aws_baseline_cost(cur,"us-east-1",analysis_time,forecast,registry,_DEMO_THRESHOLDS)
    e=WarehouseEvidence(
        warehouse_id=wh, workspace_id=workspace, account_id=cur.account_id, warehouse_name=cur.warehouse_name,
        source_region="us-east-1", analysis_time=analysis_time, current_epoch_id=cur.epoch_id, epochs=[old, cur],
        current_cost_365=model["current_cost_365"], baseline_forecast_cost_365=model["forecast_cost_365"], currency="USD",
        cost_history_days=model["cost_history_days"], cost_cv_84=model["cost_cv_84"], forecast_metadata=forecast, daily_cost_365=daily,
        billing_features={"photon_usage_pct_365": 100.0, "serverless_usage_pct_365": 0.0, "cloud": "AWS"},
        aws_baseline_cost_365=aws_cost, aws_cost_model=aws_model,
        sql_sku_prices={
            "PREMIUM_SQL_PRO_COMPUTE":{"effective_list_price":0.55,"currency_code":"USD","cloud":"AWS","usage_unit":"DBU","classification":"PRO"},
            "PREMIUM_SQL_SERVERLESS_COMPUTE":{"effective_list_price":0.70,"currency_code":"USD","cloud":"AWS","usage_unit":"DBU","classification":"SERVERLESS"},
        },
        photon_observation=PhotonObservation(
            status="ENABLED", enabled_usage_pct=100.0, known_feature_coverage_pct=100.0,
            true_dbu=20000.0, false_dbu=0.0, null_dbu=0.0,
            latest_usage_time="2026-09-30T23:00:00+00:00", freshness_days=0,
            confidence={"score":0.98,"level":"HIGH","components":{"feature_coverage":1.0,"freshness":1.0,"dbu_volume":1.0,"state_purity":1.0}},
        ),
        attribution=WarehouseAttribution(
            owner="sales-bi@databricks.example", observed_owners=["sales-bi@databricks.example"],
            owner_source="system.billing.usage.identity_metadata.owned_by",
            query_source=derive_query_source_profile(carried_query_view(cur.query)[0]),
        ),
    )
    return [e]


DEMO_ANALYSIS_TIME = "2026-10-01T12:00:00+00:00"


DEMO_ACCOUNT_ID = "acct-demo"


DEMO_WORKSPACES = ("1234567890123456", "2234567890123456", "3234567890123456")


DEMO_SKU_PRICES = {"PREMIUM_SQL_COMPUTE": 0.22, "PREMIUM_SQL_PRO_COMPUTE": 0.55, "PREMIUM_SQL_SERVERLESS_COMPUTE": 0.70}


DEMO_UNPRICED_SKU = "PREMIUM_SQL_PRO_COMPUTE_PROMO"  # billed but absent from list_prices (golden scenario C32)


DEMO_SKU_BY_TYPE = {"CLASSIC": "PREMIUM_SQL_COMPUTE", "PRO": "PREMIUM_SQL_PRO_COMPUTE", "SERVERLESS": "PREMIUM_SQL_SERVERLESS_COMPUTE"}


DEMO_START_LATENCY_MS = {"CLASSIC": 240000, "PRO": 240000, "SERVERLESS": 5000}


DEMO_SCALE_UP_LAG_MINUTES = {"CLASSIC": 5.0, "PRO": 5.0, "SERVERLESS": 1.0}


# Demo fleet: each warehouse instantiates one golden scenario (docs/proposals/publisher/golden-scenarios.md, tag golden_scenario).
# Its evidence is simulated from a workload, not hand-set: query windows and concurrency drive busy time; auto-stop
# drives running time and restarts; clusters follow concurrency (one per 10 concurrent queries, within min/max, with a
# scale-up lag); billed DBU = cluster-hours x published DBU/hour (+-3% daily noise) and cost = DBU x SKU list price.
# The resulting collector rows go through the real extract_evidence. Workload components:
#   days: weekdays | all | month_end (last business day) | half_year_end (last 5 business days of June and December)
#   start/end: "HH:MM" window; conc: concurrent queries; every/dur: periodic queries (minutes) instead of steady load;
#   q_min: average query minutes for steady load; src: query source; client: client application.
# Warehouse options: keepalive (an open session keeps it running with no queries), restart_every_days (restarts with no
# downtime; an always-on warehouse that never restarts has no retained warehouse event), growth (volume doubles every 30
# days), prior (earlier configuration epochs), epoch_start/created, unpriced_from (promotional SKU without a list
# price), correction (a RETRACTION/RESTATEMENT day), and query-quality parameters (p95/p99, spill, failures, cache).
DEMO_FLEET: Tuple[Dict[str, Any], ...] = (
    {"scenario": "C01", "restart_every_days": 30, "name": "Finance Planning", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 1, "auto_stop": 0,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 6, "dur": 1.5, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C02", "name": "Marketing Dashboards", "ws": 0, "type": "SERVERLESS", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 30,
     "work": [{"days": "weekdays", "start": s, "end": e, "every": 2, "dur": 0.5, "conc": 2, "src": "DASHBOARD"} for s, e in (("09:00", "09:30"), ("13:00", "13:20"), ("16:00", "16:30"))]},
    {"scenario": "C03", "name": "Legacy Reporting", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 1, "auto_stop": 30, "keepalive": True, "work": []},
    {"scenario": "C04", "restart_every_days": 30, "name": "BI Gateway", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 30,
     "work": [{"days": "all", "start": "00:00", "end": "24:00", "every": 5, "dur": 0.05, "conc": 1, "src": "SQL", "client": "Tableau"},
              {"days": "weekdays", "start": "09:00", "end": "17:00", "every": 4, "dur": 1.0, "conc": 1, "src": "DASHBOARD", "client": "Tableau"}]},
    {"scenario": "C05", "name": "Nightly ETL", "ws": 1, "type": "PRO", "size": "LARGE", "min": 1, "max": 2, "auto_stop": 45,
     "p95": 600000, "p99": 1500000, "task_parallelism": 24,
     "work": [{"days": "all", "start": "01:00", "end": "02:30", "conc": 6, "q_min": 4.0, "src": "JOB"}]},
    {"scenario": "C06", "name": "Sales Analytics", "ws": 0, "type": "PRO", "size": "LARGE", "min": 1, "max": 4, "auto_stop": 15, "photon": "DISABLED",
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 1, "dur": 0.3, "conc": 3, "src": "DASHBOARD", "client": "Power BI"}]},
    {"scenario": "C07", "name": "Data Science Spill", "ws": 1, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 20,
     "p95": 240000, "p99": 600000, "spill_pct": 25.0, "spill_p95_bytes": 6 * GIB, "task_parallelism": 16,
     "work": [{"days": "weekdays", "start": "09:00", "end": "17:00", "conc": 3, "q_min": 2.0, "src": "NOTEBOOK"}]},
    {"scenario": "C08", "name": "Finance Concurrency", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 20, "queue_wait_ms": 65000,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 18, "q_min": 0.5, "src": "DASHBOARD"},
              {"days": "weekdays", "start": "10:00", "end": "12:00", "conc": 12, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C09", "name": "Executive KPIs", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 10, "auto_stop": 20,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 12, "q_min": 0.4, "src": "DASHBOARD"}]},
    {"scenario": "C10", "name": "Serverless Floor", "ws": 0, "type": "SERVERLESS", "size": "MEDIUM", "min": 4, "max": 8, "auto_stop": 10,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 8, "q_min": 0.4, "src": "DASHBOARD"}]},
    {"scenario": "C11", "name": "Pinned Capacity", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 3, "max": 3, "auto_stop": 30,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 9, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C12", "name": "Morning Rush", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 6, "auto_stop": 30, "queue_wait_ms": 150000,
     "work": [{"days": "weekdays", "start": "08:45", "end": "09:30", "conc": 45, "q_min": 0.5, "src": "DASHBOARD"},
              {"days": "weekdays", "start": "09:30", "end": "17:00", "conc": 5, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C13", "name": "Interactive Analysts", "ws": 1, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 30,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 45, "dur": 3.0, "conc": 1, "src": "SQL", "client": "Databricks SQL Editor"}]},
    {"scenario": "C14", "name": "Serverless BI", "ws": 0, "type": "SERVERLESS", "size": "SMALL", "min": 1, "max": 4, "auto_stop": 20,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 3, "dur": 0.5, "conc": 2, "src": "DASHBOARD"}]},
    {"scenario": "C16", "restart_every_days": 30, "name": "Serverless 24x7 Feed", "ws": 1, "type": "SERVERLESS", "size": "MEDIUM", "min": 1, "max": 4, "auto_stop": 10,
     "work": [{"days": "all", "start": "00:00", "end": "24:00", "conc": 8, "q_min": 0.5, "src": "JOB"}]},
    {"scenario": "C18", "name": "Preview Channel", "ws": 2, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 20, "channel": "PREVIEW",
     "work": [{"days": "weekdays", "start": "09:00", "end": "17:00", "every": 5, "dur": 0.5, "conc": 1, "src": "SQL", "client": "Databricks SQL Editor"}]},
    {"scenario": "C19", "name": "Product Launch", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 30, "epoch_start": "2026-09-30",
     "prior": [{"start": "2025-10-01", "size": "MEDIUM"}],
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 2, "dur": 0.5, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C20", "name": "New Team Sandbox", "ws": 2, "type": "PRO", "size": "SMALL", "min": 1, "max": 1, "auto_stop": 10, "created": "2026-09-26",
     "work": [{"days": "weekdays", "start": "10:00", "end": "16:00", "every": 30, "dur": 1.0, "conc": 1, "src": "NOTEBOOK"}]},
    {"scenario": "C21", "name": "Cached BI", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 20, "cache_pct": 70.0,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 1, "dur": 0.2, "conc": 4, "src": "DASHBOARD", "client": "Power BI"}]},
    {"scenario": "C22", "name": "Few Heavy Queries", "ws": 1, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 20,
     "spill_pct": 3.0, "spill_p95_bytes": 5 * GIB, "spill_statements": 2,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 2, "dur": 0.5, "conc": 2, "src": "DASHBOARD"}]},
    {"scenario": "C25", "restart_every_days": 30, "name": "Weekday Operations", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 0,
     "work": [{"days": "weekdays", "start": "07:00", "end": "19:00", "every": 3, "dur": 0.5, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C26", "name": "Flaky ETL", "ws": 1, "type": "PRO", "size": "LARGE", "min": 1, "max": 3, "auto_stop": 20, "failure_pct": 12.0,
     "p95": 300000, "p99": 900000, "task_parallelism": 24,
     "work": [{"days": "all", "start": "02:00", "end": "04:00", "conc": 5, "q_min": 3.0, "src": "JOB"},
              {"days": "weekdays", "start": "09:00", "end": "17:00", "every": 10, "dur": 2.0, "conc": 1, "src": "JOB"}]},
    {"scenario": "C30", "name": "Billing Corrected", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 1, "auto_stop": 15,
     "correction": {"day": "2026-09-15", "retracted_share": 0.3, "restated_share": 0.2},
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 4, "dur": 1.0, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C32", "name": "Promotional SKU", "ws": 2, "type": "PRO", "size": "SMALL", "min": 1, "max": 1, "auto_stop": 15, "unpriced_from": "2026-09-10",
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 4, "dur": 1.0, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C37", "name": "Half-Year Close", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 8, "auto_stop": 20,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 15, "q_min": 0.5, "src": "DASHBOARD"},
              {"days": "half_year_end", "start": "08:00", "end": "20:00", "conc": 60, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C38", "name": "Growing Product BI", "ws": 0, "type": "PRO", "size": "LARGE", "min": 1, "max": 4, "auto_stop": 20, "growth": True,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 1, "dur": 0.3, "conc": 3, "src": "DASHBOARD"}]},
    {"scenario": "C39", "name": "Dev Sandbox", "ws": 2, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 30, "env": "nonprod",
     # Hour-long notebook sessions with ~25-minute breaks: a lower auto-stop adds ~4 restarts a day for ~75 queries.
     "work": [{"days": "weekdays", "start": s, "end": e, "every": 4, "dur": 0.5, "conc": 1, "src": "NOTEBOOK"}
              for s, e in (("10:00", "11:00"), ("11:25", "12:25"), ("12:50", "13:50"), ("14:15", "15:15"), ("15:40", "16:40"))]},
    {"scenario": "C41", "name": "Genie Space", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 15,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 3, "dur": 0.5, "conc": 1, "src": "GENIE"}]},
    {"scenario": "C42", "name": "Queue Flood", "ws": 1, "type": "PRO", "size": "LARGE", "min": 1, "max": 2, "auto_stop": 20, "queue_wait_ms": 600000,
     "work": [{"days": "weekdays", "start": "09:00", "end": "09:15", "conc": 1000, "q_min": 0.2, "src": "JOB"},
              {"days": "weekdays", "start": "10:00", "end": "17:00", "conc": 12, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C45", "restart_every_days": 30, "name": "Steady 24x7 Pro", "ws": 1, "type": "PRO", "size": "MEDIUM", "min": 2, "max": 4, "auto_stop": 30, "task_parallelism": 12,
     "work": [{"days": "all", "start": "00:00", "end": "24:00", "conc": 16, "q_min": 0.5, "src": "JOB"}]},
    {"scenario": "C46", "name": "Hourly Burst", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 4, "auto_stop": 15, "queue_wait_ms": 180000,
     "work": [{"days": "weekdays", "start": f"{h:02d}:00", "end": f"{h:02d}:08", "conc": 60, "q_min": 0.3, "src": "DASHBOARD"} for h in range(8, 18)]
             + [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 4, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C47", "name": "Tiny Lookup", "ws": 2, "type": "PRO", "size": "2X_SMALL", "min": 1, "max": 1, "auto_stop": 10, "p95": 900, "p99": 2500,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 2, "dur": 0.1, "conc": 1, "src": "SQL"}]},
    {"scenario": "C48", "name": "Long Unchanged", "ws": 1, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 20, "epoch_start": "2024-06-01",
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 3, "dur": 0.5, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C49", "name": "Cold Start Thrash", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 10,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 14, "dur": 1.0, "conc": 1, "src": "DASHBOARD"}]},
    {"scenario": "C50", "name": "Single Cluster Queue", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 1, "auto_stop": 20, "queue_wait_ms": 90000,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "conc": 17, "q_min": 0.5, "src": "DASHBOARD"}]},
    {"scenario": "C51", "name": "Mostly Idle", "ws": 2, "type": "PRO", "size": "SMALL", "min": 1, "max": 1, "auto_stop": 120,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 180, "dur": 5.0, "conc": 1, "src": "NOTEBOOK"}]},
    {"scenario": "C52", "name": "Month-End Close", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 15,
     "work": [{"days": "month_end", "start": "08:00", "end": "20:00", "conc": 8, "q_min": 1.0, "src": "DASHBOARD"}]},
    {"scenario": "C53", "name": "Hourly Sync Reports", "ws": 1, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 275,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 90, "dur": 3.0, "conc": 1, "src": "JOB"}]},
    {"scenario": "C54", "name": "Morning Batch Pair", "ws": 1, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 365,
     "work": [{"days": "weekdays", "start": "06:00", "end": "06:20", "conc": 2, "q_min": 2.0, "src": "JOB"},
              {"days": "weekdays", "start": "09:30", "end": "09:50", "conc": 2, "q_min": 2.0, "src": "JOB"}]},
    {"scenario": "C55", "name": "Downsized Dashboards", "ws": 0, "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 15, "epoch_start": "2026-09-27",
     "prior": [{"start": "2025-10-01", "size": "LARGE"}], "after_change": {"p95_factor": 1.6},
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 2, "dur": 0.3, "conc": 1, "src": "DASHBOARD", "client": "Power BI"}]},
    {"scenario": "A01", "name": "Serverless Fast Stop", "ws": 0, "type": "SERVERLESS", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 1,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 10, "dur": 1.0, "conc": 1, "src": "DASHBOARD"}]},
)


def _demo_unit(*parts: Any) -> float:
    """Deterministic pseudo-random number in [0, 1) from the given parts (no global RNG state)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def _demo_minutes(hhmm: str) -> int:
    """Minutes since midnight of an "HH:MM" string."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _demo_day_matches(rule: str, day: dt.date) -> bool:
    """Whether a workload component runs on ``day`` under its day rule (all, weekdays, month_end, half_year_end); ValueError otherwise."""
    if rule == "all":
        return True
    if rule == "weekdays":
        return day.weekday() < 5
    if rule == "month_end":
        nxt = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        last = nxt - dt.timedelta(days=1)
        while last.weekday() >= 5:
            last -= dt.timedelta(days=1)
        return day == last
    if rule == "half_year_end":
        if day.month not in (6, 12) or day.weekday() >= 5:
            return False
        nxt = (day.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        business_left = sum(1 for i in range((nxt - day).days) if (day + dt.timedelta(days=i)).weekday() < 5)
        return business_left <= 5
    raise ValueError(f"unknown demo day rule {rule}")


def _demo_config_at(spec: Mapping[str, Any], epochs: Sequence[Tuple[float, Optional[float], Dict[str, Any]]], t: float) -> Dict[str, Any]:
    """Configuration in force at simulation minute ``t`` (the first epoch's if none covers it); ``spec`` is unused."""
    for start, end, cfg in epochs:
        if start <= t and (end is None or t < end):
            return cfg
    return epochs[0][2]


def _demo_floor_demand(pieces: Sequence[Tuple[float, float, int, float, float]], floor: int) -> str:
    """A day's time at the floor by the clusters its demand needed (below the floor), as the collector's JSON object."""
    out: Dict[str, float] = {}
    for a, b, cl, conc, _ in pieces:
        need = int(math.ceil(conc / QUERIES_PER_CLUSTER_PRO_CLASSIC - 1e-9)) if conc > 0 else 0
        if cl > 0 and cl == floor and need < floor and b > a:
            out[str(need)] = out.get(str(need), 0.0) + (b - a)
    return json.dumps({k: round(v, 3) for k, v in sorted(out.items(), key=lambda kv: int(kv[0]))})


def _simulate_demo_warehouse(spec: Mapping[str, Any], index: int, analysis_time: str, *, world: bool = False) -> Dict[str, List[Dict[str, Any]]]:
    """Collector rows (one list per collector query label) for one simulated demo warehouse.

    ``world`` also keeps what a measurement of any epoch and window needs (``simulate_demo_world``): running-idle inputs
    for every epoch, not only the current one, and the query timeline the windowed canary guard is computed from."""
    analysis = parse_datetime(analysis_time)
    assert analysis is not None
    analysis_day = analysis.date()
    t0 = dt.datetime.combine(analysis_day - dt.timedelta(days=366), dt.time(), dt.timezone.utc)
    now_t = (analysis - t0).total_seconds() / 60.0
    window_365 = now_t - 365 * 1440.0
    window_30 = now_t - 30 * 1440.0

    def at(day: dt.date, minute: float = 0.0) -> float:
        """Simulation minute (since ``t0``) of ``day`` 00:00 UTC plus ``minute``."""
        return (dt.datetime.combine(day, dt.time(), dt.timezone.utc) - t0).total_seconds() / 60.0 + minute

    def stamp(t: float) -> str:
        """ISO UTC timestamp of simulation minute ``t``."""
        return (t0 + dt.timedelta(minutes=t)).isoformat()

    workspace = DEMO_WORKSPACES[spec["ws"]]
    wh = f"wh-demo-{index:03d}"
    base_cfg = {"warehouse_type": spec["type"], "warehouse_channel": spec.get("channel", "CURRENT"), "warehouse_size": spec["size"],
                "min_clusters": spec["min"], "max_clusters": spec["max"], "auto_stop_minutes": spec["auto_stop"]}
    created = dt.date.fromisoformat(spec.get("created") or (spec["prior"][0]["start"] if spec.get("prior") else spec.get("epoch_start") or "2025-07-01"))
    current_start = dt.date.fromisoformat(spec.get("epoch_start") or created.isoformat())
    prior_keys = {"size": "warehouse_size", "min": "min_clusters", "max": "max_clusters", "auto_stop": "auto_stop_minutes", "type": "warehouse_type"}
    epoch_defs: List[Tuple[dt.date, Dict[str, Any]]] = [(dt.date.fromisoformat(p["start"]), {**base_cfg, **{prior_keys[k]: v for k, v in p.items() if k != "start"}}) for p in spec.get("prior", [])]
    epoch_defs.append((current_start, base_cfg))
    epochs: List[Tuple[float, Optional[float], Dict[str, Any]]] = []
    for i, (d, cfg) in enumerate(epoch_defs):
        end = at(epoch_defs[i + 1][0]) if i + 1 < len(epoch_defs) else None
        epochs.append((at(d), end, cfg))
    sim_start = max(at(created), 0.0)

    # 1. Busy segments per day from the workload components. Queries are kept by start time: periodic queries as
    # points (start, count, source) and steady load as spans (start, end, queries per minute, source).
    segments: List[Tuple[float, float, float]] = []  # (start, end, concurrency)
    points: List[Tuple[float, float, str]] = []
    spans: List[Tuple[float, float, float, str]] = []
    day = max(created, analysis_day - dt.timedelta(days=366))
    while day <= analysis_day:
        jitter = 0.9 + 0.2 * _demo_unit(spec["scenario"], day)
        # Growth: volume doubles every 30 days (floored at 5% of today's), so the last month is clearly above the one before.
        growth = max(0.05, 2.0 ** ((day - analysis_day).days / 30.0)) if spec.get("growth") else 1.0
        day_t = at(day)
        for comp in spec["work"]:
            if not _demo_day_matches(comp["days"], day):
                continue
            s, e = _demo_minutes(comp["start"]), _demo_minutes(comp["end"])
            key = comp["src"] + "|" + comp.get("client", "")
            if comp.get("every"):
                conc = float(comp["conc"]) * growth
                k = s
                while k < e:
                    segments.append((day_t + k, day_t + min(k + comp["dur"], 1440), conc))
                    points.append((day_t + k, conc, key))
                    k += comp["every"]
            else:
                conc = comp["conc"] * jitter * growth
                segments.append((day_t + s, day_t + e, conc))
                spans.append((day_t + s, day_t + e, conc / comp.get("q_min", 0.5), key))
        day += dt.timedelta(days=1)
    segments = [(max(a, sim_start), min(b, now_t), c) for a, b, c in segments if b > sim_start and a < now_t]
    points = sorted(x for x in points if sim_start <= x[0] < now_t)
    point_times = [x[0] for x in points]
    spans = sorted((max(a, sim_start), min(b, now_t), r, k) for a, b, r, k in spans if b > sim_start and a < now_t)

    day0 = 366 * 1440.0  # the analysis day's 00:00 UTC, in simulation minutes

    def queries_between(lo: float, hi: float) -> Tuple[float, Dict[str, float], set]:
        """Queries whose start time is in [lo, hi): total, by source key, and the UTC days they start on."""
        total, by_src, days = 0.0, {}, set()
        for t, n, key in points[bisect.bisect_left(point_times, lo):bisect.bisect_left(point_times, hi)]:
            total += n; by_src[key] = by_src.get(key, 0.0) + n; days.add(int(t // 1440.0))
        for a, b, rate, key in spans:
            if b <= lo or a >= hi:
                continue
            a2, b2 = max(a, lo), min(b, hi)
            n = (b2 - a2) * rate
            total += n; by_src[key] = by_src.get(key, 0.0) + n
            days.update(range(int(a2 // 1440.0), int((b2 - 1e-9) // 1440.0) + 1))
        return total, by_src, days

    # Sweep to merged busy runs with total concurrency (piecewise constant), split at epoch boundaries.
    marks: Dict[float, float] = {}
    for a, b, c in segments:
        marks[a] = marks.get(a, 0.0) + c
        marks[b] = marks.get(b, 0.0) - c
    for start, _, _ in epochs[1:]:
        marks.setdefault(start, 0.0)
    runs: List[List[float]] = []  # [start, end, conc]
    level, prev = 0.0, None
    for x in sorted(marks):
        if prev is not None and level > 1e-9 and x > prev:
            if runs and abs(runs[-1][1] - prev) < 1e-9 and abs(runs[-1][2] - level) < 1e-9 and not any(abs(prev - s) < 1e-9 for s, _, _ in epochs[1:]):
                runs[-1][1] = x
            else:
                runs.append([prev, x, level])
        level += marks[x]; prev = x

    # 2. Running timeline: pieces (start, end, clusters, conc, queued_conc), starts and scaling events.
    typ = spec["type"]
    lag = DEMO_SCALE_UP_LAG_MINUTES[typ]
    pieces: List[Tuple[float, float, int, float, float]] = []
    starts: List[float] = []
    stops: List[float] = []
    scale_events: List[Tuple[float, int]] = []  # (time, +1 up / -1 down)
    if spec.get("keepalive"):
        starts.append(sim_start)
        t = sim_start
        while t < now_t:
            cfg = _demo_config_at(spec, epochs, t)
            nxt = min([now_t] + [s for s, _, _ in epochs if s > t])
            pieces.append((t, nxt, cfg["min_clusters"], 0.0, 0.0)); t = nxt
    clusters = 0
    for i, (a, b, conc) in enumerate(runs):
        cfg = _demo_config_at(spec, epochs, a)
        lo, hi = cfg["min_clusters"], cfg["max_clusters"]
        if i == 0 or clusters == 0:
            starts.append(a); clusters = lo
        need = min(max(lo, int(math.ceil(conc / 10.0 - 1e-9))), hi)
        if need > clusters:
            lag_end = min(b, a + lag)
            pieces.append((a, lag_end, clusters, conc, max(0.0, conc - clusters * 10.0)))
            scale_events.extend([(a + min(lag, b - a), +1)] * (need - clusters)); clusters = need
            a = lag_end
        elif need < clusters:
            scale_events.extend([(a, -1)] * (clusters - need)); clusters = need
        if b > a:
            pieces.append((a, b, clusters, conc, max(0.0, conc - clusters * 10.0)))
        nxt = runs[i + 1][0] if i + 1 < len(runs) else now_t
        gap = nxt - b
        if gap <= 0:
            continue
        idle_cfg = _demo_config_at(spec, epochs, b)
        if clusters > idle_cfg["min_clusters"]:
            scale_events.extend([(b, -1)] * (clusters - idle_cfg["min_clusters"]))
        clusters = idle_cfg["min_clusters"]
        keep = gap if idle_cfg["auto_stop_minutes"] == 0 else min(gap, float(idle_cfg["auto_stop_minutes"]))
        pieces.append((b, b + keep, clusters, 0.0, 0.0))
        if keep < gap:
            clusters = 0
            stops.append(b + keep)
    # Restarts with no downtime (e.g. an always-on warehouse restarted for maintenance) are STARTING events only.
    if spec.get("restart_every_days"):
        t_restart = sim_start + 180.0
        while t_restart < now_t:
            starts.append(t_restart); t_restart += spec["restart_every_days"] * 1440.0
        starts.sort()
    # warehouse_events keeps 365 days: running time is visible only from the first event inside that window.
    event_times = sorted([x for x in starts + stops + [t for t, _ in scale_events] if x >= window_365])
    coverage = event_times[0] if event_times else None

    # Split pieces at midnight so daily series and billing land on their UTC day.
    def split_daily(items: Iterable[Tuple[float, float, int, float, float]]) -> List[Tuple[float, float, int, float, float]]:
        """Cut each (start, end, ...) piece at UTC midnights, keeping its other fields."""
        out = []
        for a, b, cl, c, qc in items:
            while a < b:
                cut = min(b, (math.floor(a / 1440.0) + 1) * 1440.0)
                out.append((a, cut, cl, c, qc)); a = cut
        return out
    pieces = split_daily(pieces)
    pieces_by_day: Dict[int, List[Tuple[float, float, int, float, float]]] = {}
    for piece in pieces:
        pieces_by_day.setdefault(int(piece[0] // 1440.0), []).append(piece)
    starts_by_day: Dict[int, int] = {}
    for s0 in starts:
        starts_by_day[int(s0 // 1440.0)] = starts_by_day.get(int(s0 // 1440.0), 0) + 1

    rows: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("config_epochs", "query_metrics", "canary_guard", "warehouse_events", "idle_gaps", "running_idle_inputs", "concurrency", "resource_daily", "billing_daily_cost", "billing_features", "billing_attribution_photon_owner")}
    # Behaviour after the last configuration change (spec after_change): the current epoch's latency and failure rate.
    after = spec.get("after_change") or {}
    guard_window: Dict[float, Dict[str, Any]] = {}  # per epoch: its last 30 days, for the canary guard
    tags = {"environment": spec.get("env", "production"), "team": spec["name"].lower().replace(" ", "-"), "golden_scenario": spec["scenario"]}
    dbu_rate = SQL_DBU_PER_CLUSTER_HOUR[spec["size"]]
    owner = f"{tags['team']}@databricks.example"
    for ep_start, ep_end, cfg in epochs:
        lo_t, hi_t = max(ep_start, window_365), (ep_end if ep_end is not None else now_t)
        start_iso = stamp(ep_start)
        rows["config_epochs"].append({"account_id": DEMO_ACCOUNT_ID, "workspace_id": workspace, "warehouse_id": wh, "warehouse_name": spec["name"], **cfg,
                                      "tags": dict(tags), "epoch_start": start_iso, "epoch_end": None if ep_end is None else stamp(ep_end), "is_current": ep_end is None})
        ep_pieces = [p for p in pieces if p[0] >= ep_start and (ep_end is None or p[0] < ep_end)]
        ep_runs = [r for r in runs if r[0] >= ep_start and (ep_end is None or r[0] < ep_end)]
        # Query metrics.
        epoch_q, _, epoch_day_set = queries_between(max(ep_start, window_365), hi_t)
        q30, src30, day_set30 = queries_between(max(ep_start, window_30), hi_t)
        epoch_days, days30 = len(epoch_day_set), len(day_set30)
        busy_conc_minutes = sum((min(b, hi_t) - max(a, lo_t)) * c for a, b, c in ep_runs if b > lo_t and a < hi_t)
        q_avg = busy_conc_minutes / epoch_q if epoch_q > 0 else 0.5
        def queued(lo: float) -> float:
            return sum((b - max(a, lo)) * qc / q_avg for a, b, _, _, qc in ep_pieces if b > lo and qc > 0) if q_avg > 0 else 0.0
        queued30 = min(q30, queued(max(ep_start, window_30)))
        queued_epoch = min(epoch_q, queued(max(ep_start, window_365)))
        starts30 = [s for s in starts if s >= max(ep_start, window_30) and (ep_end is None or s < ep_end)]
        # Queries that wait for compute are those arriving while the warehouse starts.
        latency_min = DEMO_START_LATENCY_MS[typ] / 60000.0
        compute_wait30 = int(min(q30, round(sum(queries_between(s0, s0 + latency_min)[0] for s0 in starts30))))
        spill_pct = spec.get("spill_pct", 0.0)
        def src_count(prefix: str = "", client: str = "") -> int:
            return int(round(sum(v for k, v in src30.items() if (not prefix or k.split("|")[0] == prefix) and (not client or k.split("|")[1] == client))))
        clients = sorted({k.split("|")[1] for k in src30 if k.split("|")[1]})
        n30 = int(round(q30))
        decision30 = int(round(n30 * max(0.0, 1.0 - spec.get("cache_pct", 10.0) / 100.0)))  # not result-cache hits
        p95, p99 = spec.get("p95", 2500), spec.get("p99", 9000)
        failure = spec.get("failure_pct", 0.3)
        if ep_end is None and len(epochs) > 1:
            p95, p99 = round(p95 * after.get("p95_factor", 1.0)), round(p99 * after.get("p95_factor", 1.0))
            failure = after.get("failure_pct", failure)
        # The canary guard reads each epoch's last 30 days (the current epoch's are its 30-day window).
        g_lo = max(ep_start, hi_t - 30 * 1440.0)
        g_n, g_src, _ = queries_between(g_lo, hi_t)
        g_queued = min(g_n, queued(g_lo))
        g_decision = g_n * max(0.0, 1.0 - spec.get("cache_pct", 10.0) / 100.0)
        guard_window[ep_start] = {"n": g_n, "src": g_src, "queued": g_queued, "decision": g_decision, "failure": failure, "p95": p95}
        if world:
            rows.setdefault("world_epochs", []).append({"start": ep_start, "end": ep_end, "p95": p95, "failure": failure})
        rows["query_metrics"].append({
            "workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso,
            "epoch_queries": int(round(epoch_q)), "epoch_active_days": epoch_days, "queries_30": n30, "active_days_30": days30,
            "p95_total_ms_30": p95 + 500, "compute_wait_queries_30": compute_wait30,
            "p95_wait_compute_ms_when_waited_30": DEMO_START_LATENCY_MS[typ] if compute_wait30 else None,
            "queued_queries_30": int(round(queued30)), "p95_wait_capacity_ms_when_queued_30": spec.get("queue_wait_ms", 120000) if queued30 >= 0.5 else None,
            "decision_queries_30": decision30,
            "p95_compilation_ms_30": 400 if n30 else None, "p95_result_fetch_ms_30": 800 if n30 else None,
            "p95_execution_ms_30": p95 if n30 else None, "p99_execution_ms_30": p99 if n30 else None, "max_execution_ms_30": p99 * 2 if n30 else None,
            "queued_query_pct_30": round(100.0 * round(queued30) / n30, 4) if n30 else None,
            "spill_query_pct_30": spill_pct if decision30 else None, "spill_queries_30": int(round(decision30 * spill_pct / 100.0)),
            # Spilled bytes spread evenly over the spilling statement sources (spec spill_statements, default one per 50
            # spilling queries); the top three carry min(3, n) / n of them.
            **({"spill_sources_30": n_src, "top_spill_source_share_pct_30": round(100.0 * min(3, n_src) / n_src, 3)}
               if (n_src := (spec.get("spill_statements") or max(1, int(round(decision30 * spill_pct / 100.0 / 50.0))))) and decision30 * spill_pct > 0 else {}),
            "p95_spill_bytes_when_spilled_30": spec.get("spill_p95_bytes") if spill_pct and n30 else None,
            "total_spill_gib_30": round(n30 * spill_pct / 100.0 * spec.get("spill_p95_bytes", 0) / 2 / GIB, 2),
            "p95_read_bytes_30": 50 * 1024 ** 2, "avg_read_io_cache_percent_30": 35.0 if n30 else None,
            "failure_cancel_pct_30": failure if n30 else None, "result_cache_pct_30": spec.get("cache_pct", 10.0) if n30 else None,
            "tagged_query_pct_30": 60.0 if n30 else None,
            "dashboard_queries_30": src_count("DASHBOARD"), "job_queries_30": src_count("JOB"), "notebook_queries_30": src_count("NOTEBOOK"),
            "genie_queries_30": src_count("GENIE"), "alert_queries_30": 0, "sql_query_source_queries_30": src_count("SQL"),
            "power_bi_queries_30": src_count(client="Power BI"), "tableau_queries_30": src_count(client="Tableau"), "looker_queries_30": 0,
            "databricks_sql_editor_client_queries_30": src_count(client="Databricks SQL Editor"), "client_applications_30": clients,
            "statement_text_visible_pct_30": 100.0 if n30 else None, "hive_metastore_reference_queries_30": 0,
            "epoch_p95_execution_ms": p95 if epoch_q else None, "epoch_queued_query_pct": round(100.0 * queued_epoch / epoch_q, 4) if epoch_q else None,
            "epoch_spill_query_pct": spill_pct if epoch_q else None,
            "epoch_task_duration_ms": round(busy_conc_minutes * 60000.0 * spec.get("task_parallelism", 4), 1) if epoch_q else None,
            # Queries per complete UTC week before the analysis day; null for a week not wholly inside the epoch.
            **{f"queries_week_{k}": (int(round(queries_between(max(day0 - 7 * k * 1440.0, ep_start), day0 - 7 * (k - 1) * 1440.0 if ep_end is None else min(day0 - 7 * (k - 1) * 1440.0, ep_end))[0]))
                                     if ep_start <= day0 - 7 * k * 1440.0 else None) for k in range(1, 9)},
        })
        # Warehouse events: no row when no retained event covers the epoch; otherwise from the coverage start.
        cov = None if coverage is None or coverage >= hi_t else max(lo_t, coverage)
        live = [] if cov is None else [(max(a, cov), b, cl, c, qc) for a, b, cl, c, qc in ep_pieces if b > cov]
        uptime = sum(b - a for a, b, cl, _, _ in live if cl > 0)
        cluster_minutes = sum((b - a) * cl for a, b, cl, _, _ in live)
        at_min = sum(b - a for a, b, cl, _, _ in live if cl > 0 and cl == cfg["min_clusters"])
        at_max = sum(b - a for a, b, cl, _, _ in live if cl > 0 and cl == cfg["max_clusters"])
        lo30 = max(ep_start, window_30)
        if cov is not None:
            rows["warehouse_events"].append({
                "workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "event_coverage_start": stamp(cov),
                "uptime_minutes": round(uptime, 3), "cluster_minutes": round(cluster_minutes, 3), "minutes_at_min": round(at_min, 3), "minutes_at_max": round(at_max, 3),
                "time_at_min_pct": round(100.0 * at_min / uptime, 4) if uptime else None, "time_at_max_pct": round(100.0 * at_max / uptime, 4) if uptime else None,
                "max_observed_clusters": max((cl for _, _, cl, _, _ in live), default=0),
                "max_observed_clusters_within_ceiling": max((cl for _, _, cl, _, _ in live if cl <= cfg["max_clusters"]), default=None),
                "starts_30": len([s for s in starts if s >= lo30 and (ep_end is None or s < ep_end)]),
                "scale_ups_30": len([t for t, d in scale_events if d > 0 and t >= lo30 and (ep_end is None or t < ep_end)]),
                "scale_downs_30": len([t for t, d in scale_events if d < 0 and t >= lo30 and (ep_end is None or t < ep_end)]),
            })
        # Idle gaps between busy runs (query-level gaps) after the first retained event.
        gap_lo = lo_t if coverage is None else max(lo_t, coverage)
        gaps = [] if coverage is None else [ep_runs[i + 1][0] - ep_runs[i][1] for i in range(len(ep_runs) - 1)
                                            if ep_runs[i][0] >= gap_lo and ep_runs[i + 1][0] > ep_runs[i][1]]
        # Busy wall-clock minutes (union of query runs) over the same period: every epoch with queries gets a row.
        busy_runs = [] if coverage is None else [(a, b) for a, b, _ in ep_runs if a >= gap_lo]
        if busy_runs:
            gap_row = {"workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "busy_minutes": round(sum(b - a for a, b in busy_runs), 3),
                       "gap_count": len(gaps), "p50_gap_minutes": None, "p90_gap_minutes": None}
            if gaps:
                ordered = sorted(gaps)
                def pct(q: float) -> float:
                    return round(ordered[min(len(ordered) - 1, int(math.ceil(q * len(ordered))) - 1)], 4)
                gap_row.update({"p50_gap_minutes": pct(0.5), "p90_gap_minutes": pct(0.9)})
            rows["idle_gaps"].append(gap_row)
        # Per-minute concurrency histograms (running + queued queries) for the epoch, plus the recent window and busy
        # starts for the current epoch.
        conc = concurrency_histograms([(a, b, c) for a, b, c in ep_runs], (max(ep_start, window_365), hi_t),
                                      max(ep_start, window_30) if ep_end is None else None,
                                      scale_up_lag_minutes(cfg["warehouse_type"]))
        rows["concurrency"].extend({"workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "scope": sc, "level": int(level), "weight": w}
                                   for sc, hist in sorted(conc.items()) for level, w in sorted(hist.items(), key=lambda x: int(x[0])))
        # Running-idle evidence (current epoch): query gaps intersected with the simulated running timeline, over the
        # same period as the event totals; queries from a day before the window seed the busy frontier.
        # The collector asks for it per window only after the forecast, so the simulation keeps its inputs (query runs,
        # running segments, queries per UTC day) and the demo client computes the rows for the windows asked for.
        if (ep_end is None or world) and cov is not None:
            rows["running_idle_inputs"].append({
                "workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "t0": t0.isoformat(),
                "epoch_end": None if ep_end is None else stamp(ep_end),
                "busy": [[a, b] for a, b, _ in ep_runs if b > cov - 1440.0], "segments": [[a, b, cl] for a, b, cl, _, _ in live if cl > 0],
                "epoch_window": [cov, hi_t], "auto_stop": cfg["auto_stop_minutes"], "floor_clusters": cfg["min_clusters"],
                "queries_epoch": queries_between(cov, hi_t)[0],
                "queries_by_day": {str(k): queries_between(max(cov, k * 1440.0), min(hi_t, (k + 1) * 1440.0))[0]
                                   for k in range(int(cov // 1440.0), int(hi_t // 1440.0) + 1)},
            })
        # Daily resource series for the current epoch (last 120 days before the analysis date).
        if ep_end is None and coverage is not None:
            series_from = max(ep_start, coverage, at(analysis_day - dt.timedelta(days=RESOURCE_DAILY_COLLECT_DAYS)))
            d = dt.date.fromisoformat(stamp(series_from)[:10])
            while d < analysis_day:
                lo_d = at(d)
                dp = [(max(a, series_from), b, cl, c, qc) for a, b, cl, c, qc in pieces_by_day.get(int(lo_d // 1440.0), []) if b > series_from]
                rows["resource_daily"].append({
                    "workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "usage_day": d.isoformat(),
                    "uptime_minutes": round(sum(b - a for a, b, cl, _, _ in dp if cl > 0), 3),
                    "cluster_minutes": round(sum((b - a) * cl for a, b, cl, _, _ in dp), 3),
                    "minutes_at_min": round(sum(b - a for a, b, cl, _, _ in dp if cl > 0 and cl == cfg["min_clusters"]), 3),
                    "minutes_at_max": round(sum(b - a for a, b, cl, _, _ in dp if cl > 0 and cl == cfg["max_clusters"]), 3),
                    "recycle_minutes": 0.0, "max_cluster_count": max((cl for _, _, cl, _, _ in dp), default=0),
                    "starts": starts_by_day.get(int(lo_d // 1440.0), 0),
                    "floor_minutes_by_demand_clusters": _demo_floor_demand(dp, cfg["min_clusters"]),
                    "collected_from": (analysis_day - dt.timedelta(days=RESOURCE_DAILY_COLLECT_DAYS)).isoformat(),
                })
                d += dt.timedelta(days=1)
        # Billing: DBU from cluster-hours at the published rate, priced at the SKU list price.
        sku = DEMO_SKU_BY_TYPE[cfg["warehouse_type"]]
        rate = SQL_DBU_PER_CLUSTER_HOUR[cfg["warehouse_size"]]
        by_day: Dict[dt.date, float] = {}
        first_billed_day = analysis_day - dt.timedelta(days=365)  # usage_date >= CURRENT_DATE - 365, today excluded
        for a, b, cl, _, _ in ep_pieces:
            if cl > 0:
                dd = (t0 + dt.timedelta(minutes=a)).date()
                if first_billed_day <= dd < analysis_day:
                    by_day[dd] = by_day.get(dd, 0.0) + (b - a) * cl / 60.0 * rate
        unpriced_from = dt.date.fromisoformat(spec["unpriced_from"]) if spec.get("unpriced_from") else None
        correction = spec.get("correction") or {}
        photon_true = photon_false = photon_null = 0.0
        for dd in sorted(by_day):
            dbu = round(by_day[dd] * (0.97 + 0.06 * _demo_unit(spec["scenario"], "dbu", dd)), 6)
            retracted = restated = 0.0
            if correction and dd.isoformat() == correction["day"]:
                retracted = round(dbu * correction["retracted_share"], 6); restated = round(dbu * correction["restated_share"], 6)
            net = round(dbu - retracted + restated, 6)
            unpriced = unpriced_from is not None and dd >= unpriced_from
            rows["billing_daily_cost"].append({
                "workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "usage_date": dd.isoformat(),
                "currency_code": None if unpriced else "USD", "dbu_quantity": net, "unpriced_dbu_quantity": net if unpriced else 0.0,
                "list_cost": None if unpriced else round(net * DEMO_SKU_PRICES[sku], 6),
                "retracted_dbu_quantity": retracted, "restated_dbu_quantity": restated,
            })
            photon = spec.get("photon", "ENABLED")
            if photon == "ENABLED":
                photon_true += net
            elif photon == "DISABLED":
                photon_false += net
            else:
                photon_null += net
        last_bill = max((r["usage_date"] for r in rows["billing_daily_cost"] if r["epoch_start"] == start_iso), default=None)
        rows["billing_attribution_photon_owner"].append({
            "workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso,
            "photon_true_dbu": round(photon_true, 6), "photon_false_dbu": round(photon_false, 6), "photon_null_dbu": round(photon_null, 6),
            "latest_usage_time": None if last_bill is None else f"{last_bill}T23:00:00+00:00", "latest_owner": owner, "observed_owners": [owner],
            # node_type is billed for non-serverless warehouses only; sql_tier is CLASSIC or PRO (null for Serverless).
            "observed_node_types": [] if cfg["warehouse_type"] == "SERVERLESS" else sorted({AWS_SQL_WAREHOUSE_TOPOLOGY[cfg["warehouse_size"]]["driver"], "i3.2xlarge"}),
            "observed_sql_tiers": [] if cfg["warehouse_type"] == "SERVERLESS" else [cfg["warehouse_type"]],
        })
    # Carried evidence: the current epoch's query metrics over its demand period (since the last size or type change)
    # and capacity period (since the last size, type, min- or max-cluster change), when those started earlier.
    def period_start(keys: Sequence[str]) -> float:
        """Start of the current carry period: back to the earliest epoch whose ``keys`` all equal the current epoch's."""
        i = len(epoch_defs) - 1
        while i > 0 and all(epoch_defs[i - 1][1][k] == epoch_defs[i][1][k] for k in keys):
            i -= 1
        return at(epoch_defs[i][0])
    current_query = next(r for r in rows["query_metrics"] if r["epoch_start"] == stamp(epochs[-1][0]))
    for family, keys in (("demand", ("warehouse_type", "warehouse_size", "warehouse_channel")),
                         ("capacity", ("warehouse_type", "warehouse_size", "warehouse_channel", "min_clusters", "max_clusters"))):
        p_start = period_start(keys)
        if p_start >= epochs[-1][0] or epochs[-1][0] <= window_30:
            continue
        lo30 = max(p_start, window_30)
        n_q, src, day_set = queries_between(lo30, now_t)
        pcs = [x for x in pieces if x[0] >= p_start]
        rns = [x for x in runs if x[0] >= p_start]
        busy = sum((min(b, now_t) - max(a, lo30)) * c for a, b, c in rns if b > lo30 and a < now_t)
        avg = busy / n_q if n_q > 0 else 0.5
        queued_n = min(n_q, sum((b - max(a, lo30)) * qc / avg for a, b, _, _, qc in pcs if b > lo30 and qc > 0)) if avg > 0 else 0.0
        n30 = int(round(n_q))
        decision = int(round(n30 * max(0.0, 1.0 - spec.get("cache_pct", 10.0) / 100.0)))
        def count(prefix: str = "", client: str = "") -> int:
            return int(round(sum(v for k, v in src.items() if (not prefix or k.split("|")[0] == prefix) and (not client or k.split("|")[1] == client))))
        row = {k: current_query[k] for k in DEMAND_CARRY_FIELDS + QUEUE_CARRY_FIELDS if k in current_query}
        row.update({
            "period_start": stamp(p_start), "queries_30": n30, "active_days_30": len(day_set), "decision_queries_30": decision,
            "queued_queries_30": int(round(queued_n)), "queued_query_pct_30": round(100.0 * round(queued_n) / n30, 4) if n30 else None,
            "spill_queries_30": int(round(decision * spec.get("spill_pct", 0.0) / 100.0)),
            "dashboard_queries_30": count("DASHBOARD"), "job_queries_30": count("JOB"), "notebook_queries_30": count("NOTEBOOK"),
            "genie_queries_30": count("GENIE"), "sql_query_source_queries_30": count("SQL"), "power_bi_queries_30": count(client="Power BI"),
            "tableau_queries_30": count(client="Tableau"), "databricks_sql_editor_client_queries_30": count(client="Databricks SQL Editor"),
            "p95_wait_capacity_ms_when_queued_30": spec.get("queue_wait_ms", 120000) if queued_n >= 0.5 else None,
        })
        key = {"workspace_id": workspace, "warehouse_id": wh, "cur_start": stamp(epochs[-1][0])}
        rows.setdefault(f"query_metrics_carried_{family}", []).append({**key, **row})
        if family == "demand":
            lag = scale_up_lag_minutes(base_cfg["warehouse_type"])
            conc = concurrency_histograms([(a, b, c) for a, b, c in rns], (lo30, now_t), lo30, lag)
            rows.setdefault("concurrency_carried", []).extend(
                {"workspace_id": workspace, "warehouse_id": wh, "epoch_start": stamp(epochs[-1][0]), "scope": sc, "level": int(level), "weight": w}
                for sc in ("RECENT", "BUSY_START") for level, w in sorted(conc.get(sc, {}).items(), key=lambda x: int(x[0])))
    # Canary guard: the current epoch against the one before it, on statement sources seen in both windows.
    if len(epochs) > 1 and epochs[-1][0] >= window_30:
        cur_w, prev_w = guard_window[epochs[-1][0]], guard_window[epochs[-2][0]]
        shared = set(cur_w["src"]) & set(prev_w["src"])
        cache = max(0.0, 1.0 - spec.get("cache_pct", 10.0) / 100.0)
        for period, w in (("CURRENT", cur_w), ("PREVIOUS", prev_w)):
            n, dec = int(round(w["n"])), int(round(w["decision"]))
            spill_n = int(round(dec * spec.get("spill_pct", 0.0) / 100.0))
            shared_n = int(round(sum(v for k, v in w["src"].items() if k in shared) * cache))
            rows["canary_guard"].append({
                "workspace_id": workspace, "warehouse_id": wh, "epoch_start": stamp(epochs[-1][0]), "period": period,
                "queries": n, "failure_cancel_pct": w["failure"] if n else None,
                "queued_queries": int(round(w["queued"])), "queued_query_pct": round(100.0 * round(w["queued"]) / n, 4) if n else None,
                "decision_queries": dec, "spill_queries": spill_n, "spill_query_pct": round(100.0 * spill_n / dec, 4) if dec else None,
                "shared_source_queries": shared_n, "p95_execution_ms_shared_sources": w["p95"] if shared_n else None,
            })
    total_dbu = sum(r["dbu_quantity"] for r in rows["billing_daily_cost"])
    photon_dbu = total_dbu if spec.get("photon", "ENABLED") == "ENABLED" else 0.0
    rows["billing_features"].append({
        "workspace_id": workspace, "warehouse_id": wh,
        "photon_usage_pct_365": None if spec.get("photon") == "UNKNOWN" or total_dbu <= 0 else round(100.0 * photon_dbu / total_dbu, 4),
        "serverless_usage_pct_365": None if total_dbu <= 0 else (100.0 if spec["type"] == "SERVERLESS" else 0.0),
        "cloud": "AWS", "sku_names": sorted({DEMO_SKU_BY_TYPE[c["warehouse_type"]] for _, _, c in epochs} | ({DEMO_UNPRICED_SKU} if spec.get("unpriced_from") else set())),
    })
    if world:
        # The query timeline (start-time points and steady spans with their source keys) and the queued concurrency of
        # the running pieces, for canary guard rows over any pair of windows (``demo_canary_guard_rows``).
        rows["world_guard"] = [{
            "workspace_id": workspace, "warehouse_id": wh, "t0": t0.isoformat(), "points": [list(x) for x in points],
            "spans": [list(x) for x in spans], "runs": [list(x) for x in runs],
            "queued_pieces": [[a, b, qc] for a, b, _, _, qc in pieces if qc > 0], "epochs": rows.pop("world_epochs", []),
            "cache_pct": spec.get("cache_pct", 10.0), "spill_pct": spec.get("spill_pct", 0.0),
        }]
    return rows


_DEMO_ROWS_CACHE: Dict[Tuple[int, str], Dict[str, List[Dict[str, Any]]]] = {}
_DEMO_RUNNING_IDLE_CACHE: Dict[Tuple[int, str], Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}  # per fleet (size, analysis time), then windows asked


def demo_running_idle_rows(inputs: Sequence[Mapping[str, Any]], windows: Sequence[Mapping[str, Any]],
                           request: Optional[Mapping[str, Any]] = None) -> Dict[str, List[Dict[str, Any]]]:
    """Rows of the running-idle collector (by label: running_idle, running_idle_targets) for the windows a statement
    asks for, computed by the reference arithmetic from each simulated warehouse's inputs (``running_idle_inputs``).

    Without an epoch-bound ``request`` the current epoch is measured up to the analysis time; with one, the named epoch
    up to the earlier of its end and the as-of time, for the requested auto-stop values only."""
    th = _DEMO_THRESHOLDS
    asked: Dict[Tuple[str, str], List[Mapping[str, Any]]] = {}
    for w in windows:
        asked.setdefault((str(w["workspace_id"]), str(w["warehouse_id"])), []).append(w)
    epochs = (request or {}).get("epochs")
    targets = (request or {}).get("targets") or auto_stop_targets(th)
    out: Dict[str, List[Dict[str, Any]]] = {"running_idle": [], "running_idle_targets": []}
    for x in inputs:
        wins = asked.get((x["workspace_id"], x["warehouse_id"]))
        if not wins:
            continue
        if epochs is None:
            if x.get("epoch_end") is not None:
                continue
        elif parse_datetime(epochs.get(f"{x['workspace_id']}/{x['warehouse_id']}")) != parse_datetime(x["epoch_start"]):
            continue
        t0 = parse_datetime(x["t0"])
        assert t0 is not None
        minute = lambda day, shift=0: (dt.datetime.combine(dt.date.fromisoformat(day) + dt.timedelta(days=shift), dt.time(), dt.timezone.utc) - t0).total_seconds() / 60.0
        stamp = lambda t: (t0 + dt.timedelta(minutes=t)).isoformat()
        cov, hi_t = x["epoch_window"]
        as_of = parse_datetime((request or {}).get("as_of"))
        if as_of is not None:
            hi_t = min(hi_t, (as_of - t0).total_seconds() / 60.0)
        if hi_t <= cov:
            continue
        bounds = {str(w["label"]): ((cov, hi_t) if w.get("start") is None else (max(cov, minute(w["start"])), min(hi_t, minute(w["end"], 1)))) for w in wins}
        ri = running_idle_metrics([tuple(b) for b in x["busy"] if b[0] < hi_t], [(a, min(b, hi_t), c) for a, b, c in x["segments"] if a < hi_t], (cov, hi_t), bounds, x["auto_stop"],
                                  targets, x["floor_clusters"], th.running_idle_full_window_fraction, th.auto_stop_bridge_margin_pct,
                                  th.kept_alive_gap_tolerance_minutes, th.kept_alive_stopped_gap_minutes)
        for name, (a, b) in sorted(bounds.items()):
            key = {"workspace_id": x["workspace_id"], "warehouse_id": x["warehouse_id"], "epoch_start": x["epoch_start"], "window_label": name}
            queries = x["queries_epoch"] if name == "epoch" else sum(v for d, v in x["queries_by_day"].items() if a <= int(d) * 1440.0 < b)
            out["running_idle"].append({**key, "window_start": stamp(a), "window_end": stamp(b), "queries": int(round(queries)),
                                        **{k: v for k, v in ri[name].items() if k != "running_idle_by_target"}})
            out["running_idle_targets"].extend({**key, "target_min": int(t), **v} for t, v in ri[name]["running_idle_by_target"].items())
    return out


def replay_demo_rows(rows: Mapping[str, Sequence[Mapping[str, Any]]], statement: str, label: str,
                     cache: Optional[Dict[str, Dict[str, List[Dict[str, Any]]]]] = None) -> List[Dict[str, Any]]:
    """What a collector query returns from simulated rows (``_simulate_demo_warehouse``): the rows of its label, except
    running idle, which is computed for the windows the statement asks for (both labels at once). ``cache`` keeps those
    rows per windows asked; it must belong to these ``rows``, since the same windows over other rows give other rows."""
    if label == "canary_guard" and canary_guard_periods_from_statement(statement):
        return demo_canary_guard_rows(rows.get("world_guard", []), canary_guard_periods_from_statement(statement))
    if label in ("running_idle", "running_idle_targets"):
        windows = running_idle_windows_from_statement(statement)
        request = running_idle_request_from_statement(statement)
        key = json.dumps([windows, request], sort_keys=True) if request is not None else json.dumps(windows, sort_keys=True)
        computed = cache.get(key) if cache is not None else None
        if computed is None:
            computed = demo_running_idle_rows(rows.get("running_idle_inputs", []), windows, request)
            if cache is not None:
                cache[key] = computed
        return [dict(r) for r in computed[label]]
    return [dict(r) for r in rows.get(label, [])]


def demo_canary_guard_rows(worlds: Sequence[Mapping[str, Any]], periods: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Canary guard rows for explicit pairs of periods (``build_canary_guard_sql``) from simulated worlds
    (``simulate_demo_world``), with the simulation's own per-period arithmetic: queries and sources by start time,
    queued queries from the queued concurrency of the running pieces, result-cache share, spill share and the epoch's
    p95 and failure rate (the epoch the period starts in)."""
    out: List[Dict[str, Any]] = []
    by_wh = {(str(w["workspace_id"]), str(w["warehouse_id"])): w for w in worlds}
    for x in periods:
        w = by_wh.get((str(x["workspace_id"]), str(x["warehouse_id"])))
        if w is None:
            continue
        t0 = parse_datetime(w["t0"])
        assert t0 is not None
        minute = lambda day, shift=0: (dt.datetime.combine(dt.date.fromisoformat(day) + dt.timedelta(days=shift), dt.time(), dt.timezone.utc) - t0).total_seconds() / 60.0
        windows = {"PREVIOUS": (minute(x["previous"][0]), minute(x["previous"][1], 1)), "CURRENT": (minute(x["current"][0]), minute(x["current"][1], 1))}
        stats: Dict[str, Dict[str, Any]] = {}
        for period, (lo, hi) in windows.items():
            n, src = 0.0, {}
            for t, k, key in w["points"]:
                if lo <= t < hi:
                    n += k; src[key] = src.get(key, 0.0) + k
            for a, b, rate, key in w["spans"]:
                if b > lo and a < hi:
                    k = (min(b, hi) - max(a, lo)) * rate
                    n += k; src[key] = src.get(key, 0.0) + k
            busy = sum((min(b, hi) - max(a, lo)) * c for a, b, c in w["runs"] if b > lo and a < hi)
            q_avg = busy / n if n > 0 else 0.5
            queued = min(n, sum((min(b, hi) - max(a, lo)) * qc / q_avg for a, b, qc in w["queued_pieces"] if b > lo and a < hi)) if q_avg > 0 else 0.0
            epoch = next((e for e in w["epochs"] if e["start"] <= lo and (e["end"] is None or lo < e["end"])), w["epochs"][-1])
            stats[period] = {"n": n, "src": src, "queued": queued, "p95": epoch["p95"], "failure": epoch["failure"]}
        shared = set(stats["CURRENT"]["src"]) & set(stats["PREVIOUS"]["src"])
        cache = max(0.0, 1.0 - safe_float(w["cache_pct"]) / 100.0)
        for period in ("CURRENT", "PREVIOUS"):
            st = stats[period]
            n, dec = int(round(st["n"])), int(round(st["n"] * cache))
            spill_n = int(round(dec * safe_float(w["spill_pct"]) / 100.0))
            shared_n = int(round(sum(v for k, v in st["src"].items() if k in shared) * cache))
            out.append({
                "workspace_id": x["workspace_id"], "warehouse_id": x["warehouse_id"], "epoch_start": x["epoch_start"], "period": period,
                "queries": n, "failure_cancel_pct": st["failure"] if n else None,
                "queued_queries": int(round(st["queued"])), "queued_query_pct": round(100.0 * round(st["queued"]) / n, 4) if n else None,
                "decision_queries": dec, "spill_queries": spill_n, "spill_query_pct": round(100.0 * spill_n / dec, 4) if dec else None,
                "shared_source_queries": shared_n, "p95_execution_ms_shared_sources": st["p95"] if shared_n else None,
            })
    return out


def simulate_demo_world(spec: Mapping[str, Any], index: int, analysis_time: str) -> Dict[str, List[Dict[str, Any]]]:
    """Collector rows of one simulated warehouse that answer measurements of any epoch and window: the demo fleet's
    simulation with every epoch's running-idle inputs and the query timeline kept (``replay_demo_rows`` answers
    epoch-bound running-idle and windowed canary guard statements from them). ``spec`` has the shape of a
    ``DEMO_FLEET`` entry; ``index`` numbers its warehouse ``wh-demo-<index>``. Never a measurement of a real warehouse."""
    return _simulate_demo_warehouse(spec, index, analysis_time, world=True)


def scale_running_idle_windows(flat: Mapping[str, Any], basis: Mapping[str, Any], period: Tuple[str, str]) -> Dict[str, Dict[str, Any]]:
    """Hand-built evidence only (the single-warehouse demo, test fixtures): running-idle windows from whole-period
    totals (``flat``: the window fields and a ``running_idle_by_target`` map) spread over each window of ``basis``
    (``running_idle_basis``) in proportion to its share of ``period`` (start, end), counts rounded. Collected evidence
    is measured per window, never scaled."""
    p0, p1 = parse_datetime(period[0]), parse_datetime(period[1])
    assert p0 is not None and p1 is not None
    out: Dict[str, Dict[str, Any]] = {}
    for label, days in sorted(basis["windows"].items()):
        a = p0 if days is None else max(p0, dt.datetime.combine(dt.date.fromisoformat(days[0]), dt.time(), dt.timezone.utc))
        b = p1 if days is None else min(p1, dt.datetime.combine(dt.date.fromisoformat(days[1]) + dt.timedelta(days=1), dt.time(), dt.timezone.utc))
        length = (p1 - p0).total_seconds()
        share = 0.0 if length <= 0 else max(0.0, (b - a).total_seconds()) / length  # an empty period spreads nothing
        scale = lambda k, v: int(round(safe_float(v) * share)) if k in _COUNT_FIELDS else round(safe_float(v) * share, 3)
        by_target = {t: {k: scale(k, v) for k, v in row.items()} for t, row in (flat.get("running_idle_by_target") or {}).items()}
        out[label] = {"window_start": a.isoformat(), "window_end": b.isoformat(),
                      **{k: scale(k, flat.get(k)) for k in RUNNING_IDLE_WINDOW_FIELDS},
                      "running_idle_by_target": {t: row for t, row in by_target.items() if any(row.values())}}
    return out


_COUNT_FIELDS = {"queries", "running_segment_count", "kept_alive_gap_count", "restarts_for_queries", "additional_starts", "avoided_starts"}


def _demo_fleet_rows(count: int, analysis_time: str) -> Dict[str, List[Dict[str, Any]]]:
    """Collector rows of the first ``count`` demo warehouses, merged per query label, plus the account-level billing watermark and SKU price rows.

    Per-warehouse simulations are cached per process; every call returns fresh copies so callers cannot mutate the cache.
    """
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for i, spec in enumerate(DEMO_FLEET[:count], start=1):
        # The simulation is deterministic, so each warehouse is simulated once per process; callers get copies.
        if (i, analysis_time) not in _DEMO_ROWS_CACHE:
            LOGGER.debug("demo.simulate index=%d scenario=%s", i, spec["scenario"])
            _DEMO_ROWS_CACHE[(i, analysis_time)] = _simulate_demo_warehouse(spec, i, analysis_time)
        for label, items in _DEMO_ROWS_CACHE[(i, analysis_time)].items():
            merged.setdefault(label, []).extend(json.loads(json.dumps(items)))
    merged["billing_watermark"] = [{"workspace_id": ws, "billing_watermark": analysis_time[:10] + "T06:00:00+00:00", "billing_ingested_through": analysis_time[:10]}
                                   for ws in sorted({r["workspace_id"] for r in merged.get("config_epochs", [])})]
    merged["sql_sku_prices"] = [{"account_id": DEMO_ACCOUNT_ID, "sku_name": name, "cloud": "AWS", "currency_code": "USD", "usage_unit": "DBU", "effective_list_price": price}
                                for name, price in sorted(DEMO_SKU_PRICES.items())]
    return merged


def synthetic_demo_evidence(count: int = len(DEMO_FLEET)) -> List[WarehouseEvidence]:
    """Deterministic demo fleet at the collector boundary: one simulated warehouse per golden scenario.

    Collector rows are simulated from each warehouse's workload (see DEMO_FLEET) and normalized by the real
    extract_evidence, so every downstream layer (epochs, analyzers, candidates, scenarios, package contract, workbook
    projection) runs on physically consistent evidence in one `--demo` run.
    """
    if not (1 <= count <= len(DEMO_FLEET)):
        raise ValueError(f"demo count must be between 1 and {len(DEMO_FLEET)}")
    LOGGER.info("demo.start warehouses=%d", count)
    rows = _demo_fleet_rows(count, DEMO_ANALYSIS_TIME)

    class _DemoClient:
        """Replays the simulated rows by query label; unknown labels return no rows. Statements are ignored except the
        running-idle windows they carry, for which the rows are computed from the simulation."""
        def query(self, statement: str, *, label: str = "collector_sql") -> List[Dict[str, Any]]:
            # The fleet's rows are deterministic per size and analysis time, so its running idle is computed once per process.
            return replay_demo_rows(rows, statement, label, _DEMO_RUNNING_IDLE_CACHE.setdefault((count, DEMO_ANALYSIS_TIME), {}))

    registry = load_aws_price_registry(str(REPO_ROOT / "pricing" / "aws_ec2_on_demand.json"))
    evidence = extract_evidence(_DemoClient(), _DEMO_THRESHOLDS, "us-east-1", registry, analysis_time=DEMO_ANALYSIS_TIME)  # type: ignore[arg-type]
    for e in evidence:
        e.compatibility_evidence = {"mode": "B_READ_ONLY_API", "status": "AVAILABLE", "workspace_id": e.workspace_id, "enable_serverless_compute": True,
                                    "external_legacy_hive_metastore_detected": False, "external_legacy_hive_metastore_config_keys": [],
                                    "s3_access_policy_status": "UNKNOWN", "enabled_warehouse_types": {"PRO": True, "SERVERLESS": True},
                                    "source": "synthetic compatibility fixture"}
    LOGGER.info("demo.done warehouses=%d epochs=%d", len(evidence), sum(len(e.epochs) for e in evidence))
    return evidence
