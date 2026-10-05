#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse optimizer and publisher.

Pipeline:
collect -> normalize/epochs -> analyze -> optimize -> model -> estimate ->
review -> scenario plan -> stateful bundle estimate -> package/report

Source policy
-------------
* Source configs: system.compute.warehouses only.
* Source metrics: Databricks system tables only.
* Source configs and workload/cost metrics remain system-table-only.
* Read-only Databricks REST is permitted only for execution-target resolution/validation and Serverless compatibility evidence.
* Compatibility uses API evidence when available (B) and deterministically falls back to HITL/system-table evidence (A) when unavailable.
* OAuth is resolved from a local Databricks CLI profile; tokens are never logged.

The runtime is read-only and contains no LLM calls. Identical normalized
source evidence + policy/model versions produces identical recommendation IDs
and decision semantics.
"""
from __future__ import annotations

import argparse
import bisect
import dataclasses
import configparser
import datetime as dt
import hashlib
import json
import logging
import math
import os
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from publisher.config.settings import REPO_ROOT, _CONTRACT_SCHEMA_PATH, _EVIDENCE_SCHEMA_PATH  # noqa: F401
from publisher.analyzer import analyzer
from publisher.governor import governor
from publisher.governor.compatibility import migration_compatibility
from publisher.contracts.governance import GovernanceResult
from publisher.config.loader import _resolve_execution_args, request_from_args  # noqa: F401
from publisher.contracts import reporter_contract as rc
from publisher.contracts.analysis import FleetAnalysis, WarehouseAnalysis
from publisher.contracts.request import MODE_LIVE, RunContext
from publisher.contracts.analysis import AnalysisResult, Finding
from publisher.contracts.candidate import CandidateRecommendation, OptimizationDecision, Range3
from publisher.contracts.review import WarehouseOptimization
from publisher.contracts.scenario import PlanStep, PortfolioEstimate, Scenario
from publisher.contracts.source import (
    EpochEvidence, PhotonObservation, QuerySourceProfile, WarehouseAttribution, WarehouseConfig, WarehouseEvidence,
)
from publisher.config.versions import (  # noqa: F401
    ANALYZER_VERSION, BUNDLE_MODEL_VERSION, CONTRACT_VERSION, DECISION_VERSION, ESTIMATOR_VERSION, EVIDENCE_FORMAT_VERSION,
    MODEL_VERSION, OPTIMIZER_VERSION, POLICY_VERSION, PUBLISHER_VERSION, REVIEWER_VERSION, TECHNIQUE_CATALOG_VERSION,
)
from publisher.config.defaults import (  # noqa: F401
    ACTIVE_KNOBS, AWS_SQL_WAREHOUSE_TOPOLOGY, GIB, SIZE_ORDER, SQL_DBU_PER_CLUSTER_HOUR,
)
from publisher.common.utils import (  # noqa: F401
    _map_pairs, as_serializable, as_string_list, clamp, normalize_map, optional_float, round_or_none, safe_float, safe_int,
)
from publisher.common.time import (  # noqa: F401
    _iso_z, iso, parse_datetime,
)
from publisher.common.hashing import (  # noqa: F401
    canonical_json,
)
from publisher.common.ids import (  # noqa: F401
    epoch_id, finding_id, recommendation_id, scenario_id,
)
from publisher.common.logging import (  # noqa: F401
    LOGGER, setup_logging,
)
from publisher.config.thresholds import (  # noqa: F401
    Thresholds,
)
from publisher.config.defaults import (  # noqa: F401
    SUPPORTED_WAREHOUSE_TYPES,
)
from publisher.analyzer.collector.queries.config_epochs import (  # noqa: F401
    CONFIG_EPOCHS_CTE, CONFIG_EPOCHS_SQL,
)
from publisher.analyzer.collector.queries.query_history import (  # noqa: F401
    CANARY_GUARD_FIELDS, CANARY_GUARD_SQL, CARRY_PERIOD_COLUMNS, CONCURRENCY_SCOPES, EPOCH_QUERY_METRICS_SQL, PERIODS_CTE, SPILL_SOURCE_SQL_TEMPLATE, build_carried_concurrency_sql, build_carried_query_metrics_sql, build_carried_spill_source_sql, build_concurrency_sql, build_spill_source_sql,
)
from publisher.analyzer.collector.queries.events import (  # noqa: F401
    EPOCH_EVENT_METRICS_SQL, RESOURCE_DAILY_COLLECT_DAYS, RESOURCE_DAILY_SQL, RUNNING_IDLE_HEADER_FIELDS, RUNNING_IDLE_TARGET_FIELDS, _running_idle_ctes, build_epoch_gap_metrics_sql, build_running_idle_sql, build_running_idle_targets_sql,
)
from publisher.analyzer.collector.queries.billing import (  # noqa: F401
    BILLING_FEATURE_SQL, BILLING_WATERMARK_SQL, EPOCH_BILLING_ATTRIBUTION_SQL, EPOCH_DAILY_COST_SQL, SQL_SKU_PRICE_SQL, UNSEEN_BILLED_WAREHOUSES_SQL,
)
from publisher.analyzer.collector.sql_executor import (  # noqa: F401
    SqlClient, UTC_TIMEZONE_NAMES, with_ingestion_cutoff,
)
from publisher.analyzer.collector.workspace_api import (  # noqa: F401
    AuthContext, _api_get, _current_workspace_id, _external_hive_config_keys, _load_profile_host, _load_profile_values, _resolve_cli_token, _validate_all_purpose_cluster, collect_workspace_compatibility_evidence, resolve_cli_oauth,
)
from publisher.analyzer.collector.billing import (  # noqa: F401
    _is_unpriced_only_row, billing_check_through, collect_unseen_billed_warehouses, effective_billing_watermark, settle_reference_time,
)
from publisher.analyzer.collector.pricing import (  # noqa: F401
    aws_cluster_hourly_price, classify_sql_sku, load_aws_price_registry, model_aws_baseline_cost,
)
from publisher.analyzer.normalizer import (  # noqa: F401
    REDACTED_OWNER_VALUES, clean_owner, derive_query_source_profile, hive_reference_count, infer_photon_observation, is_nonprod, normalize_size,
)
from publisher.analyzer.epochs import (  # noqa: F401
    DEMAND_CARRY_FIELDS, DEMAND_CARRY_FINDINGS, QUEUE_CARRY_FIELDS, QUEUE_CARRY_FINDINGS, billed_days_recent, carried_query_view, epoch_age_days, observed_epoch_calendar_days, ready_billed_days,
)
from publisher.analyzer.metrics import (  # noqa: F401
    RESOURCE_SERIES_FIELDS, annualized_resource_rates, auto_stop_candidates, auto_stop_targets, blended_daily_rate, concurrency_histograms, conditional_severity, cost_stability, histogram_percentile, histogram_top_level, query_volume_growth_ratio, recent_capacity_facts, recent_peak_at_rate, resource_window, resource_window_from, running_idle_metrics, size_busy_share, whole_weeks, window_days,
)
from publisher.analyzer.forecast import (  # noqa: F401
    Modeler, billing_cadence,
)
from publisher.analyzer.blockers import (  # noqa: F401
    DBU_BASELINE_WITHHELD, MIGRATION_BASELINE_RATIONALE, RESOURCE_WINDOW_BLOCKERS, dbu_baseline_blocker, failure_blockers,
)
from publisher.analyzer.collector.collector import (  # noqa: F401
    _index_epoch_rows, _row_key, _warehouse_key, extract_evidence,
)
from publisher.analyzer.collector.replay import (  # noqa: F401
    _epoch_from_dict, evidence_from_dict, load_evidence_files, save_evidence, validate_evidence_payload,
)
from publisher.analyzer.calibration import (  # noqa: F401
    _billed_dbu_per_cluster_hour, _prepare_workspace_calibration,
)
from publisher.analyzer.findings import (  # noqa: F401
    Analyzer, canary_observation, canary_rationale,
)
from publisher.analyzer.collector.demo import (  # noqa: F401
    DEMO_ACCOUNT_ID, DEMO_ANALYSIS_TIME, DEMO_FLEET, DEMO_SCALE_UP_LAG_MINUTES, DEMO_SKU_BY_TYPE, DEMO_SKU_PRICES, DEMO_START_LATENCY_MS, DEMO_UNPRICED_SKU, DEMO_WORKSPACES, _DEMO_ROWS_CACHE, _demo_config_at, _demo_daily, _demo_day_matches, _demo_fleet_rows, _demo_interp, _demo_minutes, _demo_unit, _simulate_demo_warehouse, demo_evidence, synthesize_resource_daily, synthetic_demo_evidence,
)
from publisher.analyzer.analyzer import (  # noqa: F401
    _is_supported_warehouse,
)
from publisher.governor.eligibility import (  # noqa: F401
    unsupported_warehouse_exclusions,
)
from publisher.governor.constraints import (  # noqa: F401
    aws_region_price_guard,
)


# ---------------------------------------------------------------------------
# System-table SQL
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Utilities / auth / collection
# ---------------------------------------------------------------------------


def aws_cluster_hourly_from_evidence(e: WarehouseEvidence, size: str) -> Tuple[Optional[float], Dict[str, Any]]:
    topology = AWS_SQL_WAREHOUSE_TOPOLOGY.get(size)
    prices = e.aws_cost_model.get("region_instance_prices") if isinstance(e.aws_cost_model, Mapping) else None
    if not topology or not isinstance(prices, Mapping):
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_EVIDENCE_NOT_AVAILABLE", "warehouse_size": size, "region": e.source_region}
    driver = str(topology["driver"]); worker_type = "i3.2xlarge"; workers = int(topology["workers"])
    if driver not in prices or worker_type not in prices:
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_INSTANCE_PRICE_MISSING", "driver": driver, "worker": worker_type, "region": e.source_region}
    hourly = safe_float(prices[driver]) + workers * safe_float(prices[worker_type])
    return round(hourly, 6), {"status": "MODELED", "region": e.source_region, "warehouse_size": size, "driver": driver, "workers": workers, "worker_type": worker_type, "cluster_hourly_price": round(hourly, 6)}


def resolve_target_sku_price(sql_sku_prices: Mapping[str, Any], target_type: str) -> Tuple[Optional[float], Dict[str, Any]]:
    target = str(target_type).upper()
    candidates: List[Tuple[str, float]] = []
    for name, payload in sql_sku_prices.items():
        if classify_sql_sku(name) != target:
            continue
        price = payload.get("effective_list_price") if isinstance(payload, Mapping) else payload
        if price is not None:
            candidates.append((name, safe_float(price)))
    distinct = sorted({round(v, 10) for _, v in candidates})
    if not candidates:
        return None, {"status": "UNAVAILABLE", "blocker": "TARGET_SQL_SKU_PRICE_NOT_FOUND", "target_type": target}
    if len(distinct) != 1:
        return None, {"status": "UNAVAILABLE", "blocker": "AMBIGUOUS_TARGET_SQL_SKU_PRICE", "target_type": target, "candidates": candidates}
    return distinct[0], {"status": "RESOLVED", "target_type": target, "unit_price_per_dbu": distinct[0], "sku_names": [n for n, _ in candidates]}


# Why a ceiling increase's best case is zero added cost, stated with the candidate and its pricing method.
MAX_INCREASE_COST_BOUNDS = ("Cost: the best case adds no cost, because the same queries run on more clusters at once and the "
                            "total running time does not grow; the worst case runs every added cluster whenever the warehouse "
                            "sat at its current ceiling.")


def previous_size(size: str) -> Optional[str]:
    if size not in SIZE_ORDER:
        return None
    i = SIZE_ORDER.index(size)
    return SIZE_ORDER[i - 1] if i > 0 else None


def next_size(size: str) -> Optional[str]:
    if size not in SIZE_ORDER:
        return None
    i = SIZE_ORDER.index(size)
    return SIZE_ORDER[i + 1] if i < len(SIZE_ORDER) - 1 else None


def validated_size_rate_ratio(rates: Mapping[str, float], current: str, target: str, band: float) -> Tuple[Optional[float], str]:
    """Target/current DBU-per-cluster-hour ratio from a workspace's billed rates, or None with the reason it is unusable.

    Usable only when the observed rates rise with size and the pair ratio is within `band` of the published size
    ratio; otherwise a guessed ratio could price a downsize as an upsize.
    """
    observed = sorted((SIZE_ORDER.index(s), safe_float(r)) for s, r in rates.items() if s in SIZE_ORDER and safe_float(r) > 0)
    if any(later <= earlier for (_, earlier), (_, later) in zip(observed, observed[1:])):
        return None, "RATES_NOT_RISING_WITH_SIZE"
    if safe_float(rates.get(current)) <= 0 or safe_float(rates.get(target)) <= 0:
        return None, "SIZE_RATE_NOT_OBSERVED"
    published_cur, published_target = SQL_DBU_PER_CLUSTER_HOUR.get(current), SQL_DBU_PER_CLUSTER_HOUR.get(target)
    if not (published_cur and published_target):
        return None, "NO_PUBLISHED_SIZE_RATE"
    ratio = safe_float(rates[target]) / safe_float(rates[current])
    if abs(ratio / (published_target / published_cur) - 1.0) > band:
        return None, "OUTSIDE_PUBLISHED_RATIO_BAND"
    return ratio, "VALIDATED"


# ---------------------------------------------------------------------------
# Analyzer: facts and evidence only; no config recommendations.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Optimizer: config decisions only. No cost arithmetic.
# ---------------------------------------------------------------------------

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
                    and safe_int(mf["epoch_max_observed"]) > self._ceiling_for(safe_int(mf["max_observed"]), cfg.min_clusters):
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

        # Auto stop: jump to the cost-minimizing value on the type's grid.
        a=analyses["auto_stop_minutes"]; f=a.facts; cur_auto=cfg.auto_stop_minutes
        def auto_stop(status:str,target:int,blockers:List[str],rationale:str)->None:
            # Non-work activity is reported with every outcome; no auto-stop value removes it, so nothing is priced.
            if f.get("kept_alive_pct") is not None and f["kept_alive_pct"]>=self.t.kept_alive_flag_pct:
                blockers=blockers+["ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING"]
                rationale+=f" {f['kept_alive_pct']:g}% of running time came after the auto-stop should have stopped the warehouse, with no query running (open sessions, result fetching or keep-alive)."
            out.append(OptimizationDecision(e.warehouse_id,"auto_stop_minutes","COST",status,
                {"auto_stop_minutes":cur_auto},{"auto_stop_minutes":target},sorted(set(blockers)),[],[],[],rationale))
        grid=f["grid"]; options={o["target"]:o for o in f["options"]}
        if cfg.warehouse_type=="SERVERLESS" and 0<cur_auto<min(grid):
            auto_stop("NO_CHANGE",cur_auto,[],"Serverless auto-stop is below the lowest value the console offers (set through the API); it is left alone.")
        elif not f["running_available"]:
            billed=f.get("billed_recent")
            if (billed and f["queries_30"]==0 and not young
                    and billed["settled_days"]>=self.t.min_economics_cost_history_days and billed["billed_days"]==billed["settled_days"]
                    and e.baseline_forecast_cost_365 is not None and (cur_auto==0 or min(grid)<cur_auto)):
                # Billing shows it ran every settled day with no query, but without retained running intervals no
                # running hours or idle saving can be measured: a human decides first, and nothing is priced.
                spent="" if billed["billed_cost"] is None else f" ({billed['billed_cost']:,.0f} {e.currency})"
                auto_stop("REVIEW",min(grid),["NO_QUERIES_WHILE_RUNNING"],
                    f"The warehouse was billed on {billed['billed_days']} of the last {billed['settled_days']} settled days{spent} with no query in the last 30 days; "
                    f"its forward cost is {e.baseline_forecast_cost_365:,.0f} {e.currency} a year. Review whether it is still needed or what keeps it running "
                    f"(open sessions, keep-alive, a connected tool). If it stays, {min(grid)} minutes is the lowest auto-stop; no saving is priced because "
                    "no retained warehouse event gives its running time.")
            else:
                auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE"],
                    "Idle time intersected with the warehouse's running time was not collected (older evidence or no retained warehouse event); no auto-stop change is priced.")
        elif safe_float(f.get("cluster_minutes"))<self.t.min_cluster_minutes_for_cost_model:
            auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["INSUFFICIENT_EVENT_RESOURCE_EVIDENCE"],"The warehouse recorded too little running time to price an auto-stop change.")
        elif any(o["direction"]=="LOWER" and o["gain_cluster_minutes"]>safe_float(f.get("cluster_minutes"))+0.01 for o in options.values()):
            # Running idle time is part of running time by construction; more of it than the events recorded means the
            # two collectors saw different periods or data, so nothing can be priced.
            auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["IDLE_EVIDENCE_EXCEEDS_RUNTIME"],
                "Removable idle time exceeds the running time warehouse events recorded; the evidence is inconsistent, so no auto-stop change is priced.")
        elif young:
            auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["CURRENT_CONFIG_EPOCH_TOO_NEW"],"The current configuration is not ready yet (too few settled billed days) to measure its idle gaps; auto-stop is re-evaluated once it is.")
        elif not f.get("running_idle_queries"):
            # No queries but running time: a human decides whether the warehouse is still needed or what keeps it up.
            floor=min(grid); o=options.get(floor)
            if o is not None and o["direction"]=="LOWER" and o["net_cluster_minutes"]>0:
                auto_stop("REVIEW",floor,["NO_QUERIES_WHILE_RUNNING"],
                    "The warehouse ran with no queries in the window; review whether it is still needed. The lowest auto-stop value is priced on its measured idle time.")
            else:
                auto_stop("NO_CHANGE",cur_auto,["NO_QUERIES_WHILE_RUNNING"],
                    "The warehouse ran with no queries in the window, and no lower auto-stop removes that running time; review whether it is still needed or what keeps it running.")
        else:
            saving=[o for o in options.values() if o["net_cluster_minutes"]>0]
            allowed=[o for o in saving if o["within_cold_start_budget"]]
            if not allowed:
                polling=(not saving and f.get("running_clock_pct") is not None and f["running_clock_pct"]>=self.t.always_on_running_pct
                         and f.get("busy_pct") is not None and f["busy_pct"]<=self.t.always_on_busy_pct_max)
                clients=", ".join(f.get("client_applications") or []) or "unknown clients"
                auto_stop("NO_CHANGE",cur_auto,(["COLD_START_BUDGET_EXCEEDED"] if saving else [])+(["SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING"] if polling else []),
                    "Every value that saves money adds more cold starts than the budget allows; auto-stop stays." if saving else
                    f"The warehouse runs {f['running_clock_pct']:g}% of the time while queries are busy only {f['busy_pct']:g}% of it: frequent tiny queries ({clients}) keep it up, so no auto-stop value can stop it. Review the polling, keep-alive or refresh schedule." if polling else
                    "No auto-stop value saves money on the measured idle gaps; the current value is cost-optimal.")
            else:
                best=max(o["net_cluster_minutes"] for o in allowed)
                near=[o for o in allowed if o["net_cluster_minutes"]>=(1-self.t.auto_stop_near_best_pct/100.0)*best]
                # The smallest change among near-best values (an always-on warehouse changes least at the largest value).
                pick=min(near,key=lambda o:((abs(o["target"]-cur_auto) if cur_auto>0 else -o["target"]),o["target"]))
                blockers=failure_blockers(f["failure_pct"], self.t) if pick["direction"]=="LOWER" else []
                if blockers:
                    auto_stop("BLOCKED",pick["target"],blockers,"A cheaper auto-stop exists but guardrails block the recommendation.")
                elif pick["direction"]=="RAISE":
                    # The saving is entirely restarts avoided, priced on the assumption that starting minutes are billed.
                    auto_stop("TEST_REQUIRED",pick["target"],["RESTART_BILLING_UNVERIFIED"],
                        "A higher auto-stop saves more restart cost than the idle time it adds; the restart cost is an assumption (starting minutes billed), so billed cost is measured after the change.")
                else:
                    auto_stop("ACTIONABLE",pick["target"],[],
                        "The cost-minimizing auto-stop on measured running idle time, counting the restarts it adds, within the cold-start budget.")

        # Warehouse-type migration is always a separate scenario branch.
        a=analyses["warehouse_type"]; f=a.facts
        blockers=["A_B_COST_PERFORMANCE_VALIDATION_REQUIRED"]
        if e.baseline_forecast_cost_365 is None or e.cost_history_days < self.t.min_economics_cost_history_days:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","INSUFFICIENT_EVIDENCE",
                {"warehouse_type":cfg.warehouse_type},{"warehouse_type":cfg.warehouse_type},[dbu_baseline_blocker(e,self.t)],[],[],[],
                MIGRATION_BASELINE_RATIONALE[dbu_baseline_blocker(e,self.t)]))
        elif any(d.knob=="auto_stop_minutes" and "NO_QUERIES_WHILE_RUNNING" in d.blockers for d in out):
            # A warehouse that ran with no query may not be needed at all: the review comes before any migration.
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","NO_CHANGE",
                {"warehouse_type":cfg.warehouse_type},{"warehouse_type":cfg.warehouse_type},["NO_QUERIES_WHILE_RUNNING"],[],[],[],
                "The warehouse ran with no queries; no migration experiment is proposed until a review decides whether it is still needed."))
        elif cfg.warehouse_type == "SERVERLESS":
            if not f.get("pro_classic_observed"):
                blockers.append("PRO_CLASSIC_WORKSPACE_CAPABILITY_NOT_CONFIRMED_FROM_SYSTEM_TABLES")
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","TEST_REQUIRED",
                {"warehouse_type":cfg.warehouse_type},{"warehouse_type":"PRO"},blockers,[],
                ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
                ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
                "Pro is evaluated as an alternate migration experiment for Serverless warehouses. The estimate is pre-canary and compatibility remains a deterministic gate."))
        else:
            if not f.get("serverless_observed"):
                blockers.append("SERVERLESS_WORKSPACE_CAPABILITY_NOT_CONFIRMED_FROM_SYSTEM_TABLES")
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","PERFORMANCE_AND_OPERATIONS","TEST_REQUIRED",
                {"warehouse_type":cfg.warehouse_type},{"warehouse_type":"SERVERLESS"},blockers,[],
                ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
                ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
                "Serverless is evaluated as a separate migration experiment with a pre-canary DBU/AWS counterfactual and deterministic compatibility gate."))
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
        return out


# ---------------------------------------------------------------------------
# Estimator: independent candidate counterfactuals, then stateful bundle.
# ---------------------------------------------------------------------------

class Estimator:
    """Price independent candidate counterfactuals and attach deterministic confidence."""
    def __init__(self, thresholds: Thresholds):
        self.t=thresholds

    # The sample each knob's decision is made on and its full-credit size; an unknown sample earns no credit.
    _DECISION_SAMPLE={"warehouse_size":("decision_queries","strong_query_sample_size"),"max_clusters":("busy_minutes_recent","confidence_strong_busy_minutes"),
                      "min_clusters":("busy_periods_recent","confidence_strong_busy_periods"),"auto_stop_minutes":("running_idle_queries","strong_query_sample_size"),
                      "warehouse_type":("queries_30","strong_query_sample_size")}

    def _confidence(self,e:WarehouseEvidence,a:AnalysisResult,signal:float=1.0,cap:float=1.0)->Dict[str,Any]:
        fact,strong=self._DECISION_SAMPLE[a.knob]
        coverage=clamp(e.cost_history_days/self.t.confidence_coverage_days,0,1); sample=clamp(safe_float(a.facts.get(fact))/getattr(self.t,strong),0,1)
        stability=cost_stability(e.cost_cv_84); epoch=clamp(epoch_age_days(e.current_epoch, e.analysis_time)/self.t.confidence_epoch_maturity_days,0,1)
        score=clamp(
            self.t.confidence_base_weight
            + self.t.confidence_coverage_weight*coverage
            + self.t.confidence_sample_weight*sample
            + self.t.confidence_stability_weight*(stability or 0.0)  # unknown stability earns no credit
            + self.t.confidence_epoch_weight*epoch
            + self.t.confidence_signal_weight*clamp(signal,0,1),
            0,cap)
        return {"score":round(score,3),"level":"HIGH" if score>=self.t.confidence_high_threshold else "MEDIUM" if score>=self.t.confidence_medium_threshold else "LOW",
                "components":{"coverage":round(coverage,3),"sample":round(sample,3),"stability":None if stability is None else round(stability,3),"epoch_maturity":round(epoch,3),"signal":round(signal,3)}}

    def _range_from_factor(self,baseline:float,factors:Range3)->Tuple[Range3,Range3]:
        costs=Range3(baseline*factors.low,baseline*factors.base,baseline*factors.high).rounded()
        savings=Range3(max(0,baseline-costs.high),max(0,baseline-costs.base),max(0,baseline-costs.low)).rounded()
        return costs,savings

    def _factors(self,e:WarehouseEvidence,d:OptimizationDecision,a:AnalysisResult)->Tuple[Optional[Range3],Dict[str,Any]]:
        knob=d.knob
        meta:Dict[str,Any]={"estimator_version":ESTIMATOR_VERSION}
        if d.status in {"BLOCKED","INSUFFICIENT_EVIDENCE"}:
            # No counterfactual is attempted for a candidate the constraints reject: unknown cost is null, never zero.
            return None,{"method":"estimate withheld: the candidate is blocked or lacks the evidence to be priced","bundle_effect":{"type":"no_cost_change"},**meta}
        if d.status=="NO_CHANGE":
            return Range3(1.0,1.0,1.0),{"method":"no modeled change","bundle_effect":{"type":"no_cost_change"},**meta}
        if "CANARY_GUARDRAIL_BREACHED" in d.blockers:
            reason="A rollback restores the previous configuration; its cost is the previous configuration's, which this estimate does not price."
            return None,{"method":"estimate withheld: canary rollback","bundle_effect":{"type":"estimate_withheld","status":"UNAVAILABLE","reason":reason},**meta}
        if knob=="max_clusters" and d.objective=="COST_GUARDRAIL":
            return Range3(1.0,1.0,1.0),{"method":"guardrail/stability change; no direct savings assigned","bundle_effect":{"type":"no_cost_change"},**meta}
        if e.baseline_forecast_cost_365 is None or e.cost_history_days < self.t.min_economics_cost_history_days:
            # Every estimate below scales the forward DBU cost, which is unknown (unpriced usage, too few billed days, a
            # non-positive run rate) or not trustworthy on a young epoch, even when replayed evidence carries a number.
            blocker=dbu_baseline_blocker(e,self.t); status,reason=DBU_BASELINE_WITHHELD[blocker]
            return None,{
                "method":f"estimate withheld: {reason}",
                "estimate_blocker":blocker,
                "bundle_effect":{"type":"estimate_withheld","status":status,"reason":reason},**meta}
        if knob=="warehouse_type":
            return None,{"method":"warehouse-type migration uses explicit DBU + AWS pre-canary cost model","bundle_effect":{"type":"migration_cost_override"},**meta}

        if knob=="auto_stop_minutes":
            target=safe_int(d.recommended_config.get("auto_stop_minutes"))
            opt=next((o for o in a.facts.get("options") or [] if o["target"]==target),None)
            cluster=safe_float(a.facts.get("cluster_minutes"))
            if opt is None or cluster<=0:
                reason="The auto-stop step cannot be priced without the warehouse's running time (no retained warehouse event); economics are withheld."
                return None,{"method":"estimate withheld: no running-idle evidence for the recommended auto-stop",
                             "bundle_effect":{"type":"estimate_withheld","status":"UNAVAILABLE","reason":reason},**meta}
            # Net cluster-minutes saved (idle removed minus restarts added, or restarts saved minus idle added) as a share
            # of running cluster-minutes over the same period; the realization range applies to that net.
            raw=clamp(opt["net_cluster_minutes"]/cluster,0,1)
            factors=Range3(1-raw*self.t.realization_optimistic,1-raw*self.t.realization_base,1-raw*self.t.realization_conservative)
            return factors,{
                "method":"running idle time (idle gaps intersected with observed RUNNING time) at the recommended auto-stop, net of priced restarts",
                "raw_savings_fraction":round(raw,4),"auto_stop_option":dict(opt),
                "restart_cost_assumption":"Pro/Classic starting minutes are assumed billed at the floor cluster count (start time about 4 minutes, documented; billing not documented); Serverless restarts cost about nothing.",
                "bundle_effect":{"type":"idle_cluster_minute_reduction","gain_cluster_minutes":opt["gain_cluster_minutes"],"loss_cluster_minutes":opt["loss_cluster_minutes"],
                                 "floor_clusters":safe_int(a.facts.get("floor_clusters"),1)},
                **meta,
            }

        if knob=="min_clusters":
            minutes_at_min=safe_float(a.facts.get("minutes_at_min"))
            cluster=safe_float(a.facts.get("cluster_minutes"))
            current=safe_int(d.current_config.get("min_clusters"),1)
            target=safe_int(d.recommended_config.get("min_clusters"),current)
            delta=max(0,current-target)
            theoretical_removed=minutes_at_min*delta
            raw=clamp(theoretical_removed/cluster,0,self.t.min_cluster_savings_fraction_cap) if cluster>0 else 0
            realization=[self.t.min_cluster_realization_optimistic,self.t.min_cluster_realization_base,self.t.min_cluster_realization_conservative]
            factors=Range3(*(1-raw*r for r in realization))
            return factors,{
                "method":"cluster-minutes observed at the minimum are an upper bound on removable capacity (the floor may have been needed); high savings is that upper bound, low is no savings, base is the policy midpoint; canary required",
                "raw_savings_fraction":round(raw,4),
                "savings_high_is_upper_bound":True,
                # The capped removable minutes (not the raw time-at-min) feed the stateful bundle so both models agree.
                "bundle_effect":{"type":"min_cluster_reduction","cluster_minutes_to_remove":raw*cluster,"target_min_clusters":target,"realization":realization},
                **meta,
            }

        if knob=="warehouse_size":
            cur=str(d.current_config.get("warehouse_size")); target=str(d.recommended_config.get("warehouse_size"))
            warehouse_type=str(e.current_epoch.config.warehouse_type or "").upper()
            if warehouse_type=="SERVERLESS":
                cur_dbu=SQL_DBU_PER_CLUSTER_HOUR.get(cur); target_dbu=SQL_DBU_PER_CLUSTER_HOUR.get(target)
                if not (cur_dbu and target_dbu):
                    # No published Serverless DBU/hour for one side (5X-Large): a nominal ratio would be a guess, so the
                    # candidate keeps its decision but is not priced, and any plan containing it needs a canary to price.
                    unpriced=[x for x in (cur,target) if not SQL_DBU_PER_CLUSTER_HOUR.get(x)]
                    return None,{
                        "method":"estimate withheld: no published Serverless DBU/hour for "+"/".join(unpriced)+"; a nominal size ratio would be a guess",
                        "estimate_blocker":"SERVERLESS_DBU_RATE_NOT_PUBLISHED","unpublished_sizes":unpriced,
                        "bundle_effect":{"type":"requires_canary"},**meta,
                    }
                capacity=target_dbu/cur_dbu
                capacity_source="DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR_RATIO"
            else:
                # Pro/Classic DBU per size is not documented first-party: price only on the workspace's billed rates
                # when they are consistent with the published size table; otherwise withhold (never a guessed ratio).
                empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
                ratio,check=validated_size_rate_ratio(empirical if isinstance(empirical,Mapping) else {},cur,target,self.t.size_rate_published_band)
                if ratio is None:
                    return None,{
                        "method":"estimate withheld: the workspace's billed DBU per cluster-hour by size does not validate this size change",
                        "estimate_blocker":"PRO_CLASSIC_SIZE_RATE_UNVERIFIED","size_rate_check":check,
                        "bundle_effect":{"type":"requires_canary"},**meta,
                    }
                capacity=ratio
                capacity_source="WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR_RATIO"
            # Speed-up multipliers follow the decided direction, never the price ratio.
            if SIZE_ORDER.index(target)<SIZE_ORDER.index(cur):
                busy_runtime=Range3(self.t.downsize_runtime_multiplier_optimistic,self.t.downsize_runtime_multiplier_base,self.t.downsize_runtime_multiplier_conservative)
            else:
                busy_runtime=Range3(self.t.upsize_runtime_multiplier_optimistic,self.t.upsize_runtime_multiplier_base,self.t.upsize_runtime_multiplier_conservative)
            # The multiplier changes query execution time only; idle running time is unchanged. Unknown busy share:
            # all running time is treated as busy.
            busy_share=a.facts.get("busy_share")
            share=1.0 if busy_share is None else float(busy_share)
            runtime=Range3(*(round(share*x+(1.0-share),6) for x in (busy_runtime.low,busy_runtime.base,busy_runtime.high)))
            factors=Range3(capacity*runtime.low,capacity*runtime.base,capacity*runtime.high)
            vals=sorted([
                clamp(factors.low,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
                clamp(factors.base,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
                clamp(factors.high,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
            ])
            factors=Range3(vals[0],vals[1],vals[2])
            return factors,{
                "method":"capacity ratio × runtime elasticity on busy running time",
                "capacity_ratio":capacity,
                "capacity_ratio_source":capacity_source,
                "busy_share":busy_share,
                "busy_runtime_multiplier_range":as_serializable(busy_runtime),
                "runtime_multiplier_range":as_serializable(runtime),
                "bundle_effect":{"type":"multiplicative_cost_factor","resource_runtime_factor_range":as_serializable(runtime)},
                **meta,
            }

        if knob=="max_clusters" and d.recommended_config.get("max_clusters",0)>d.current_config.get("max_clusters",0):
            at=safe_float(a.facts.get("minutes_at_max")); cluster=safe_float(a.facts.get("cluster_minutes"))
            current=safe_int(d.current_config.get("max_clusters"),1); target=safe_int(d.recommended_config.get("max_clusters"),current)
            delta=max(0,target-current)
            # Worst case: the added clusters run whenever the warehouse sat at its ceiling. No cap: a single-cluster
            # warehouse at its ceiling all the time doubles its cost with a second cluster.
            raw=max(0.0,(at*delta)/cluster) if cluster>0 else 0
            hist=a.facts.get("recent_concurrency_histogram")
            if e.current_epoch.config.warehouse_type!="SERVERLESS" and hist and cluster>0:
                # Base: each busy minute runs only the clusters its running + queued queries need under the new ceiling
                # (one per documented queries-per-cluster), never more than the worst case.
                qpc=self.t.queries_per_cluster_pro_classic
                added=sum(safe_float(w)*(min(math.ceil(int(level)/qpc),target)-min(math.ceil(int(level)/qpc),current)) for level,w in hist.items())
                base=min(raw,max(0.0,added/cluster)); method="cost uplift from the clusters peak demand needs under the new ceiling (base) up to the added clusters running whenever the warehouse sat at its ceiling (worst case)"
            else:
                base=raw*self.t.max_cluster_base_uplift_weight; method="upper-bound cost uplift from additional clusters during observed saturation"
            factors=Range3(1.0,1+base,1+raw)
            return factors,{
                "method":method+". "+MAX_INCREASE_COST_BOUNDS,
                "raw_uplift_fraction":round(raw,4),"base_uplift_fraction":round(base,4),
                "bundle_effect":{"type":"multiplicative_cost_factor","resource_runtime_factor_range":as_serializable(factors)},
                **meta,
            }

        return Range3(1,1,1),{"method":"no direct modeled cost delta","bundle_effect":{"type":"no_cost_change"},**meta}

    @staticmethod
    def _delta_range(baseline: float, costs: Range3) -> Range3:
        return Range3(round(baseline-costs.high,2), round(baseline-costs.base,2), round(baseline-costs.low,2))

    def _aws_factor_range(self,e:WarehouseEvidence,d:OptimizationDecision,dbu_factors:Optional[Range3],meta:Mapping[str,Any])->Optional[Range3]:
        if e.aws_baseline_cost_365 is None:
            return None
        if e.current_epoch.config.warehouse_type == "SERVERLESS":
            return Range3(1.0,1.0,1.0)
        if dbu_factors is None:
            return None
        if d.knob != "warehouse_size":
            return dbu_factors
        current=str(d.current_config.get("warehouse_size")); target=str(d.recommended_config.get("warehouse_size"))
        cur_hourly,_=aws_cluster_hourly_from_evidence(e,current); target_hourly,_=aws_cluster_hourly_from_evidence(e,target)
        rr=meta.get("runtime_multiplier_range")
        if cur_hourly is None or target_hourly is None or not isinstance(rr,Mapping) or cur_hourly<=0:
            return None
        ratio=target_hourly/cur_hourly
        vals=sorted([ratio*safe_float(rr.get("low"),1.0),ratio*safe_float(rr.get("base"),1.0),ratio*safe_float(rr.get("high"),1.0)])
        return Range3(vals[0],vals[1],vals[2])

    def _annualized_resource_bounds(self,e:WarehouseEvidence)->Tuple[Optional[Range3],Dict[str,Any]]:
        # Same window and model as the DBU forecast (run-rate blend, or whole billing cycles): AWS/resource bounds and
        # DBU describe the same year.
        rates,info=annualized_resource_rates(e.current_epoch,e.forecast_metadata or {},self.t,("uptime_minutes","cluster_minutes"))
        if rates is None:
            return None,{"status":"UNAVAILABLE",**info}
        cluster_minutes=info["window_cluster_minutes"]; uptime=info["window_uptime_minutes"]; max_obs=max(1,int(info["max_observed_clusters"] or 1))
        if cluster_minutes<=0 or uptime<=0:
            return None,{"status":"UNAVAILABLE","blocker":"INSUFFICIENT_CURRENT_EPOCH_RESOURCE_EVIDENCE","resource_window_days":info["resource_window_days"],"cluster_minutes":cluster_minutes,"uptime_minutes":uptime}
        trend=info["trend_adjustment_factor"]
        low=rates["uptime_minutes"]
        base=rates["cluster_minutes"]
        high=max(base,low*max_obs)
        vals=sorted([low,base,high])
        return Range3(vals[0],vals[1],vals[2]),{"status":"MODELED","method":"recent resource-time structural bounds on the DBU forecast's window and model, annualized forward","resource_window_start":info["resource_window_start"],"resource_window_end":info["resource_window_end"],"resource_window_days":info["resource_window_days"],"trend_adjustment_factor":trend,"window_cluster_minutes":cluster_minutes,"window_uptime_minutes":uptime,"max_observed_clusters":max_obs,"annualized_cluster_minutes_range":as_serializable(Range3(vals[0],vals[1],vals[2]))}

    def _current_model_reconciliation(self,e:WarehouseEvidence,resources:Range3)->Dict[str,Any]:
        """Modeled current DBU cost (running cluster-hours x current DBU/hour x current price) over the billed forecast."""
        cfg=e.current_epoch.config
        if cfg.warehouse_type=="SERVERLESS":
            rate=SQL_DBU_PER_CLUSTER_HOUR.get(cfg.warehouse_size)
        else:
            empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
            rate=(safe_float(empirical.get(cfg.warehouse_size),0.0) if isinstance(empirical,Mapping) else 0.0) or None
        price,_=resolve_target_sku_price(e.sql_sku_prices,cfg.warehouse_type)
        billed=e.baseline_forecast_cost_365
        if not rate or price is None or not billed:
            return {"blocker":"MIGRATION_MODEL_NOT_RECONCILED","reason":"CURRENT_RATE_OR_PRICE_UNAVAILABLE","ratio":None}
        modeled=resources.base/60.0*rate*price
        ratio=round(modeled/billed,6)
        out={"ratio":ratio,"modeled_current_dbu_cost_365":round(modeled,2),"billed_dbu_forecast_365":round(billed,2),"current_dbu_per_cluster_hour":rate,"current_unit_price":price,
             "band":[self.t.migration_reconciliation_min,self.t.migration_reconciliation_max]}
        if not self.t.migration_reconciliation_min<=ratio<=self.t.migration_reconciliation_max:
            out["blocker"]="MIGRATION_MODEL_NOT_RECONCILED"
        return out

    def _migration_costs(self,e:WarehouseEvidence,d:OptimizationDecision,facts:Optional[Mapping[str,Any]]=None)->Dict[str,Any]:
        target=str(d.recommended_config.get("warehouse_type") or "").upper()
        size=e.current_epoch.config.warehouse_size
        current_resources,resource_meta=self._annualized_resource_bounds(e)
        if current_resources is None:
            return {"available":False,"blocker":resource_meta.get("blocker"),"resource_model":resource_meta}
        fraction=(facts or {}).get("migration_running_fraction")
        if fraction is None:
            return {"available":False,"blocker":"RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE","resource_model":resource_meta}
        # The target runs at its own best auto-stop: base = today's annualized cluster-minutes x its running fraction;
        # optimistic = busy cluster-time only (it never runs idle); conservative = today's structural upper bound
        # (every running minute at the peak cluster count), never below the base.
        busy_share=(facts or {}).get("busy_share")
        base_t=current_resources.base*fraction
        low_t=min(base_t,current_resources.base*float(busy_share)) if busy_share is not None else base_t
        resources=Range3(round(low_t,3),round(base_t,3),round(max(base_t,current_resources.high),3))
        resource_meta={**resource_meta,"target_running_fraction":fraction,"target_auto_stop_option":as_serializable((facts or {}).get("migration_target_option")),
                       "busy_share":busy_share,"target_annualized_cluster_minutes_range":as_serializable(resources),
                       "target_method":"target at its own cost-minimizing auto-stop (base), busy cluster-time only (optimistic), today's running time at the peak cluster count (conservative)"}
        if target=="SERVERLESS":
            dbu_per_hour=SQL_DBU_PER_CLUSTER_HOUR.get(size)
            rate_source="DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR"
        else:
            empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
            dbu_per_hour=safe_float(empirical.get(size),0.0) if isinstance(empirical,Mapping) else 0.0
            dbu_per_hour=dbu_per_hour or None
            rate_source="WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR"
        if dbu_per_hour is None:
            return {"available":False,"blocker":"TARGET_DBU_PER_CLUSTER_HOUR_NOT_AVAILABLE","warehouse_size":size,"target_type":target,"resource_model":resource_meta,"rate_source":rate_source}
        unit_price,price_meta=resolve_target_sku_price(e.sql_sku_prices,target)
        if unit_price is None:
            return {"available":False,"blocker":price_meta.get("blocker"),"sku_price_model":price_meta,"resource_model":resource_meta}
        # Reconcile: the same formula on the current warehouse must reproduce its billed DBU forecast; the target is then
        # priced relative to billing (scaled by billed / modeled), so systematic model error cancels out.
        reconciliation=self._current_model_reconciliation(e,current_resources)
        if reconciliation.get("blocker"):
            return {"available":False,"blocker":reconciliation["blocker"],"reconciliation_ratio":reconciliation.get("ratio"),"reconciliation":reconciliation,"resource_model":resource_meta,"sku_price_model":price_meta}
        scale=1.0/reconciliation["ratio"]
        dbu_cost=Range3(*(round(x/60.0*dbu_per_hour*unit_price*scale,2) for x in (resources.low,resources.base,resources.high)))
        if target=="SERVERLESS":
            aws_cost=Range3(0.0,0.0,0.0)
            aws_meta={"status":"NOT_CUSTOMER_BILLED","reason":"Serverless SQL infrastructure is Databricks-managed; no separate customer EC2 line is modeled."}
        elif target in {"PRO","CLASSIC"}:
            hourly,aws_meta=aws_cluster_hourly_from_evidence(e,size)
            if hourly is None:
                return {"available":False,"blocker":aws_meta.get("blocker"),"sku_price_model":price_meta,"resource_model":resource_meta,"aws_model":aws_meta}
            aws_cost=Range3(*(round(x/60.0*hourly,2) for x in (resources.low,resources.base,resources.high)))
        else:
            return {"available":False,"blocker":"UNSUPPORTED_TARGET_WAREHOUSE_TYPE","target_type":target}
        total_cost=Range3(round(dbu_cost.low+aws_cost.low,2),round(dbu_cost.base+aws_cost.base,2),round(dbu_cost.high+aws_cost.high,2))
        return {"available":True,"target_type":target,"warehouse_size":size,"reconciliation_ratio":reconciliation["ratio"],"reconciliation":reconciliation,"dbu_cost_range":dbu_cost,"aws_cost_range":aws_cost,"total_cost_range":total_cost,"dbu_per_cluster_hour":dbu_per_hour,"dbu_rate_source":rate_source,"unit_price_per_dbu":unit_price,"resource_model":resource_meta,"sku_price_model":price_meta,"aws_model":aws_meta,"estimate_status":"PRE_CANARY_MODELED_RANGE"}

    def candidates(self,e:WarehouseEvidence,analyses:Dict[str,AnalysisResult],decisions:Sequence[OptimizationDecision])->List[CandidateRecommendation]:
        out=[]; ep=e.current_epoch
        dbu_baseline=round_or_none(e.baseline_forecast_cost_365,2); aws_baseline=None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365,2)
        total_baseline=None if aws_baseline is None or dbu_baseline is None else round(dbu_baseline+aws_baseline,2)
        for d in decisions:
            a=analyses[d.knob]
            out.append(self._candidate(e,ep,a,d,dbu_baseline,aws_baseline,total_baseline))
            gate=self._value_gate(d,out[-1])
            if gate:
                # A change made to save money ships only when its own modeled savings clear the floor; the modeled
                # economics stay on the held candidate so the reason is visible.
                held=dataclasses.replace(d,status=gate.get("status","NO_CHANGE"),recommended_config=dict(d.current_config),blockers=sorted(set(d.blockers)|{gate["reason_code"]}),
                    invalidates=[],reanalysis_required=[],rationale=gate["rationale"])
                out[-1]=self._candidate(e,ep,a,held,dbu_baseline,aws_baseline,total_baseline)
                out[-1].metadata["value_gate"]=gate
        return out

    def _value_gate(self,d:OptimizationDecision,c:"CandidateRecommendation")->Optional[Dict[str,Any]]:
        if d.knob=="warehouse_type" and d.status in {"ACTIONABLE","TEST_REQUIRED"} and d.current_config!=d.recommended_config:
            # A migration whose best case still costs more is not worth a canary (it may still be wanted for other
            # reasons, which is a human decision outside this cost model).
            # Total only: DBU alone is not comparable across types (Serverless DBU includes compute that Pro/Classic
            # pays as AWS), and unknown AWS cost is not a loss.
            r=c.total_365d_savings_range
            if r is None or r.high>0:
                return None
            return {"reason_code":"MIGRATION_NOT_COST_EFFECTIVE","modeled_savings_365":c.estimated_savings_365,"modeled_savings_range_365":as_serializable(r),
                    "floor_365":0.0,"held_change":as_serializable(d.recommended_config),"original_status":d.status,
                    "rationale":f"Held: even the best case of the {d.recommended_config.get('warehouse_type')} migration costs more than today."}
        if d.objective!="COST" or d.status not in {"ACTIONABLE","TEST_REQUIRED"} or d.current_config==d.recommended_config:
            return None
        save=c.estimated_savings_365
        if save is None and d.status=="ACTIONABLE" and c.metadata.get("estimate_blocker"):
            # A cost change applied without a canary has nothing else to validate it: it waits for its own price.
            blocker=c.metadata["estimate_blocker"]
            return {"reason_code":blocker,"status":"INSUFFICIENT_EVIDENCE","modeled_savings_365":None,"modeled_savings_range_365":None,
                    "floor_365":self.t.cost_change_min_annual_savings,"held_change":as_serializable(d.recommended_config),"original_status":d.status,
                    "rationale":f"Held: the {d.knob} change is applied without a canary, so it waits until its own savings can be priced. {DBU_BASELINE_WITHHELD[blocker][1]}"}
        if save is None:
            return None  # unpriced (withheld) changes are canaries; there is nothing to gate on
        floor=self.t.cost_change_min_annual_savings
        if save<=0:
            code,why="COST_CHANGE_NOT_COST_EFFECTIVE","its own modeled savings are not positive"
        elif save<floor:
            code,why="COST_SAVINGS_BELOW_MATERIALITY",f"its own modeled savings are below the {floor:,.0f} a year materiality floor"
        else:
            return None
        return {"reason_code":code,"modeled_savings_365":save,"modeled_savings_range_365":as_serializable(c.estimated_savings_range_365),
                "floor_365":floor,"held_change":as_serializable(d.recommended_config),"original_status":d.status,
                "rationale":f"Held: the {d.knob} change was proposed to save money but {why}."}

    def _candidate(self,e:WarehouseEvidence,ep:EpochEvidence,a:AnalysisResult,d:OptimizationDecision,dbu_baseline:Optional[float],aws_baseline:Optional[float],total_baseline:Optional[float])->"CandidateRecommendation":
        factors,meta=self._factors(e,d,a)
        dbu_costs=dbu_savings=None; aws_costs=aws_savings=None; total_costs=total_savings=None
        est=save=None
        if d.knob=="warehouse_type" and d.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"} and d.current_config!=d.recommended_config:
            migration=self._migration_costs(e,d,a.facts); meta={**meta,"migration_model":as_serializable(migration),"bundle_effect":{"type":"migration_cost_override"}}
            if migration.get("available"):
                dbu_costs=migration["dbu_cost_range"]; aws_costs=migration["aws_cost_range"]; total_costs=migration["total_cost_range"]
                dbu_savings=self._delta_range(dbu_baseline,dbu_costs)
                if aws_baseline is not None:
                    aws_savings=self._delta_range(aws_baseline,aws_costs)
                if total_baseline is not None:
                    total_savings=self._delta_range(total_baseline,total_costs); est=total_costs.base; save=total_savings.base
            else:
                meta["estimate_blocker"]=migration.get("blocker")
        elif factors is not None and dbu_baseline is not None:
            dbu_costs=Range3(round(dbu_baseline*factors.low,2),round(dbu_baseline*factors.base,2),round(dbu_baseline*factors.high,2))
            dbu_savings=self._delta_range(dbu_baseline,dbu_costs)
            aws_factors=self._aws_factor_range(e,d,factors,meta)
            meta={**meta,"aws_cost_factor_range":as_serializable(aws_factors) if aws_factors else None}
            if aws_baseline is not None and aws_factors is not None:
                aws_costs=Range3(round(aws_baseline*aws_factors.low,2),round(aws_baseline*aws_factors.base,2),round(aws_baseline*aws_factors.high,2))
                aws_savings=self._delta_range(aws_baseline,aws_costs)
                total_costs=Range3(round(dbu_costs.low+aws_costs.low,2),round(dbu_costs.base+aws_costs.base,2),round(dbu_costs.high+aws_costs.high,2))
                if total_baseline is not None: total_savings=self._delta_range(total_baseline,total_costs)
            elif e.current_epoch.config.warehouse_type=="SERVERLESS":
                aws_costs=Range3(0,0,0); aws_savings=Range3(0,0,0); total_costs=dbu_costs; total_savings=dbu_savings
            est=(total_costs.base if total_costs else dbu_costs.base); save=(total_savings.base if total_savings else dbu_savings.base)
        signal=self.t.confidence_signal_change if d.status in {"ACTIONABLE","TEST_REQUIRED"} else self.t.confidence_signal_no_change
        cap=(self.t.confidence_cap_test_required if d.status=="TEST_REQUIRED" else self.t.confidence_cap_actionable if d.status=="ACTIONABLE"
             else self.t.confidence_cap_insufficient_evidence if d.status=="INSUFFICIENT_EVIDENCE" else self.t.confidence_cap_default)
        if (d.knob=="max_clusters" and e.current_epoch.config.warehouse_type=="SERVERLESS"
                and safe_int(d.recommended_config.get("max_clusters"))>safe_int(d.current_config.get("max_clusters"))):
            cap=min(cap,self.t.confidence_cap_serverless_ceiling_increase)
        confidence=self._confidence(e,a,signal=signal,cap=cap)
        current=d.current_config; recommended=d.recommended_config
        rid=recommendation_id(e.workspace_id,e.warehouse_id,d.knob,ep.epoch_id,current,recommended)
        return CandidateRecommendation(
            recommendation_id=rid,warehouse_id=e.warehouse_id,workspace_id=e.workspace_id,warehouse_name=e.warehouse_name,knob=d.knob,objective=d.objective,status=d.status,
            current_config=current,recommended_config=recommended,current_cost_365=round_or_none(e.current_cost_365,2),
            baseline_forecast_cost_365=dbu_baseline,estimated_cost_365=est,
            estimated_cost_range_365=total_costs or dbu_costs,estimated_savings_365=save,estimated_savings_range_365=total_savings or dbu_savings,
            confidence=confidence,findings=a.findings,blockers=d.blockers,dependencies=d.dependencies,invalidates=d.invalidates,
            reanalysis_required=d.reanalysis_required,metadata={
                **meta,"cost_factor_range":as_serializable(factors) if factors else None,"estimate_is_independent_scenario":True,
                "candidate_savings_must_not_be_summed":True,"current_epoch_id":ep.epoch_id,"currency":e.currency,
                "rationale":d.rationale,"policy_version":POLICY_VERSION,"analyzer_version":ANALYZER_VERSION,"optimizer_version":OPTIMIZER_VERSION,
                "cost_scope":"DBU at published list price (observed system.billing usage x system.billing.list_prices, not contract-effective) plus modeled customer AWS EC2 at on-demand list price for Pro/Classic (Spot not modeled); Serverless cloud infrastructure is embedded in the Serverless DBU line.",
                "aws_cost_model":e.aws_cost_model,
            },
            dbu_365d_baseline_cost=dbu_baseline,aws_365d_baseline_cost=aws_baseline,total_365d_baseline_cost=total_baseline,
            dbu_365d_savings_estimate=(dbu_savings.base if dbu_savings else None),aws_365d_savings_estimate=(aws_savings.base if aws_savings else None),
            total_365d_savings_estimate=(total_savings.base if total_savings else None),dbu_365d_savings_range=dbu_savings,aws_365d_savings_range=aws_savings,
            total_365d_savings_range=total_savings,dbu_365d_estimated_cost_range=dbu_costs,aws_365d_estimated_cost_range=aws_costs,total_365d_estimated_cost_range=total_costs,
        )

class Reviewer:
    """Fail-fast invariant gate for identity, evidence epoch, config, cost, and confidence contracts."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def review(self,e:WarehouseEvidence,candidates:Sequence[CandidateRecommendation])->None:
        seen=set(); cfg=e.current_epoch.config
        knobs=[c.knob for c in candidates]
        if set(knobs) != set(ACTIVE_KNOBS) or len(knobs) != len(ACTIVE_KNOBS):
            raise ValueError(f"Expected exactly one candidate per active knob for {e.workspace_id}/{e.warehouse_id}: {knobs}")
        for c in candidates:
            if c.workspace_id != e.workspace_id or c.warehouse_id != e.warehouse_id:
                raise ValueError(f"Candidate identity mismatch: {c.recommendation_id}")
            if c.recommendation_id in seen: raise ValueError(f"Duplicate recommendation id: {c.recommendation_id}")
            seen.add(c.recommendation_id)
            if any(x is not None and x<0 for x in (c.current_cost_365,c.baseline_forecast_cost_365)): raise ValueError(f"Negative cost: {c.recommendation_id}")
            score=safe_float(c.confidence.get("score"),-1)
            if not 0<=score<=1: raise ValueError(f"Confidence out of range: {c.recommendation_id}")
            if c.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"} and c.current_config==c.recommended_config:
                raise ValueError(f"Change status without config change: {c.recommendation_id}")
            if c.status in {"BLOCKED","INSUFFICIENT_EVIDENCE"} and not c.blockers: raise ValueError(f"{c.status} without blockers: {c.recommendation_id}")
            if c.status=="INSUFFICIENT_EVIDENCE" and c.current_config!=c.recommended_config: raise ValueError(f"INSUFFICIENT_EVIDENCE with config change: {c.recommendation_id}")
            if c.status=="NO_CHANGE" and c.estimated_savings_365 not in (None,0,0.0): raise ValueError(f"NO_CHANGE with savings: {c.recommendation_id}")
            if c.estimated_cost_range_365:
                r=c.estimated_cost_range_365
                if not (0<=r.low<=r.base<=r.high): raise ValueError(f"Invalid cost range: {c.recommendation_id}")
            if c.estimated_savings_range_365:
                r=c.estimated_savings_range_365
                if not (r.low<=r.base<=r.high): raise ValueError(f"Invalid savings range: {c.recommendation_id}")
            for f in c.findings:
                if f.epoch_id!=e.current_epoch_id and f.metric not in {"serverless_usage_pct_365"}:
                    raise ValueError(f"Decision finding not current epoch: {c.recommendation_id} {f.finding_id}")
            if c.knob=="warehouse_size":
                x=c.recommended_config.get("warehouse_size")
                if x not in SIZE_ORDER and x!=cfg.warehouse_size: raise ValueError(f"Invalid size: {x}")
            if c.knob=="min_clusters":
                x=safe_int(c.recommended_config.get("min_clusters"),cfg.min_clusters)
                if not 1 <= x <= self.t.min_clusters_supported_limit: raise ValueError("min_clusters outside supported range")
                # A rollback restores floor and ceiling together: the floor must fit under the ceiling it comes back with.
                ceiling=cfg.max_clusters
                if "CANARY_GUARDRAIL_BREACHED" in c.blockers:
                    ceiling=next((safe_int(o.recommended_config.get("max_clusters"),ceiling) for o in candidates
                                  if o.knob=="max_clusters" and "CANARY_GUARDRAIL_BREACHED" in o.blockers),ceiling)
                if x > ceiling: raise ValueError("min_clusters exceeds current max_clusters")
            if c.knob=="max_clusters":
                x=safe_int(c.recommended_config.get("max_clusters"),cfg.max_clusters)
                if not 1<=x<=self.t.max_clusters_supported_limit: raise ValueError("max_clusters outside supported range")
                if x < cfg.min_clusters: raise ValueError("max_clusters below current min_clusters")
            if c.knob=="auto_stop_minutes":
                x=safe_int(c.recommended_config.get("auto_stop_minutes"),cfg.auto_stop_minutes)
                if x < 0: raise ValueError("auto_stop_minutes < 0")
                if x != cfg.auto_stop_minutes and cfg.warehouse_type != "SERVERLESS" and x < self.t.auto_stop_floor_pro_classic:
                    raise ValueError("auto_stop_minutes below the Pro/Classic minimum")
            if str(c.metadata.get("currency"))!=e.currency: raise ValueError("Currency mismatch")

    def review_scenarios(
        self,
        e: WarehouseEvidence,
        candidates: Sequence[CandidateRecommendation],
        scenarios: Sequence[Scenario],
        recommended_scenario_id: Optional[str],
    ) -> None:
        """Validate dependency ordering, confidence propagation, and portfolio ranges."""
        by_id={c.recommendation_id:c for c in candidates}
        ids={s.scenario_id for s in scenarios}
        if recommended_scenario_id not in ids:
            raise ValueError(f"Recommended scenario missing for {e.workspace_id}/{e.warehouse_id}: {recommended_scenario_id}")
        if sum(1 for s in scenarios if s.recommended)!=1:
            raise ValueError(f"Expected exactly one recommended scenario for {e.workspace_id}/{e.warehouse_id}")
        for sc in scenarios:
            sc_score=safe_float(sc.confidence.get("score"),-1)
            if not (0<=sc_score<=1 or (not sc.steps and sc.confidence.get("score") is None)):
                raise ValueError(f"Scenario confidence out of range: {sc.scenario_id}")
            seen_steps=set(); invalidated=set()
            for st in sc.steps:
                if st.recommendation_id not in by_id:
                    raise ValueError(f"Scenario references unknown recommendation: {st.recommendation_id}")
                if st.recommendation_id in seen_steps:
                    raise ValueError(f"Scenario duplicates recommendation: {st.recommendation_id}")
                seen_steps.add(st.recommendation_id)
                c=by_id[st.recommendation_id]
                if st.knob!=c.knob or st.current_config!=c.current_config or st.recommended_config!=c.recommended_config:
                    raise ValueError(f"Scenario step diverges from candidate contract: {st.recommendation_id}")
                step_score=safe_float(st.confidence.get("score"),-1)
                if not 0<=step_score<=1:
                    raise ValueError(f"Plan-step confidence out of range: {st.recommendation_id}")
                if abs(step_score-safe_float(c.confidence.get("score"),-2))>1e-9:
                    raise ValueError(f"Plan-step confidence does not match candidate: {st.recommendation_id}")
                if st.knob in invalidated and st.execution_state!="PENDING_REEVALUATION":
                    raise ValueError(f"Invalidated downstream step must be pending reevaluation: {st.recommendation_id}")
                invalidated.update(st.invalidates)
            # A canary rollback is a first, ready step and restores its knob regardless of what later steps invalidate.
            rollbacks=[i for i,st in enumerate(sc.steps) if "CANARY_GUARDRAIL_BREACHED" in by_id[st.recommendation_id].blockers]
            if rollbacks!=list(range(len(rollbacks))) or any(sc.steps[i].execution_state=="PENDING_REEVALUATION" for i in rollbacks):
                raise ValueError(f"Canary rollback must be a first, ready step: {sc.scenario_id}")
            for i,st in enumerate(sc.steps):
                if i not in rollbacks and any(st.knob in later.invalidates for later in sc.steps[i+1:]):
                    raise ValueError(f"Plan step precedes a step that invalidates it: {st.recommendation_id}")
            pe=sc.portfolio_estimate
            for r in (pe.estimated_cost_range_365, pe.estimated_savings_range_365, pe.dbu_365d_estimated_cost_range, pe.aws_365d_estimated_cost_range, pe.total_365d_estimated_cost_range, pe.dbu_365d_savings_range, pe.aws_365d_savings_range, pe.total_365d_savings_range):
                if r is not None and not (r.low<=r.base<=r.high):
                    raise ValueError(f"Scenario range is not ordered: {sc.scenario_id}")
            # Authoritative economics may not include steps whose evidence was invalidated upstream (audit H1).
            for st in sc.steps:
                pending=st.execution_state=="PENDING_REEVALUATION"
                if pending != (st.pricing_basis==PRICING_CONTINGENT):
                    raise ValueError(f"Plan-step pricing basis must match its execution state: {st.recommendation_id}")
            authoritative=[st for st in sc.steps if st.pricing_basis==PRICING_AUTHORITATIVE]
            contingent_steps=[st for st in sc.steps if st.pricing_basis==PRICING_CONTINGENT]
            if pe.contingent is not None:
                if not contingent_steps:
                    raise ValueError(f"Contingent economics without a pending step: {sc.scenario_id}")
                for r in (pe.contingent.estimated_cost_range_365, pe.contingent.estimated_savings_range_365):
                    if r is not None and not (r.low<=r.base<=r.high):
                        raise ValueError(f"Contingent range is not ordered: {sc.scenario_id}")
                last=contingent_steps[-1].running_estimated_savings_range_365
                got=pe.contingent.estimated_savings_range_365
                if last is not None and got is not None and any(abs(a-b)>0.01 for a,b in ((last.low,got.low),(last.base,got.base),(last.high,got.high))):
                    raise ValueError(f"Contingent savings must equal the last pending step's running savings: {sc.scenario_id}")
            if authoritative and pe.estimate_status not in {"REQUIRES_CANARY","REQUIRES_COST_MODEL"}:
                last=authoritative[-1].running_estimated_savings_range_365
                got=pe.estimated_savings_range_365
                if last is not None and got is not None and any(abs(a-b)>0.01 for a,b in ((last.low,got.low),(last.base,got.base),(last.high,got.high))):
                    raise ValueError(f"Authoritative scenario savings must equal the last authoritative step's running savings: {sc.scenario_id}")
            if sc.name.startswith("Plan A") and any(st.knob=="warehouse_type" for st in sc.steps):
                raise ValueError(f"Current-type Plan A cannot contain warehouse_type migration: {sc.scenario_id}")
            if sc.name.startswith("Plan B") and len(sc.steps)>1:
                raise ValueError(f"Migration Plan B must remain a separate one-step branch: {sc.scenario_id}")


PRICING_AUTHORITATIVE = "AUTHORITATIVE"
PLAN_KNOB_RANK = {"warehouse_size": 0, "max_clusters": 1, "min_clusters": 2, "auto_stop_minutes": 3}  # plan-order tie-break
PRICING_CONTINGENT = "CONTINGENT_PENDING_REEVALUATION"


@dataclass
class _ChainState:
    """Evolving cost/resource state for one pricing chain; `halt` = (status, method) once it cannot be priced further."""
    dbu_tracks: List[float]
    aws_tracks: Optional[List[float]]
    resource_tracks: List[float]
    # Floor clusters per track: a minimum-cluster cut realizes a different share of its reduction on each track (none on
    # the conservative one), so later steps scale with that track's own floor.
    floor_tracks: List[float]
    halt: Optional[Tuple[str, str]] = None
    # Net running wall-clock minutes an earlier auto-stop change removed per floor cluster, per track (a lower auto-stop:
    # idle removed minus restarts added; a higher one: restarts saved minus idle added). A later minimum-cluster cut
    # re-scales that effect to the lower floor, so the plan total does not depend on step order.
    auto_stop_wall_minutes: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Cluster-minutes an earlier minimum-cluster cut removed, per track: a later auto-stop's saving at the lower floor
    # gives back at most this much (the overlap of the two is bounded by what the floor cut removed), so the plan total
    # does not depend on step order even when the floor cut's claim is clamped at zero.
    floor_cut_minutes: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])


class StatefulBundleEstimator:
    """Compose ordered recommendations against evolving DBU, AWS, and resource state.

    Candidate estimates are standalone counterfactuals. Portfolio estimates are
    stateful and sequential. Min-cluster and auto-stop consume remaining
    cluster-minutes, so the same capacity minute cannot be claimed twice.
    """
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    @staticmethod
    def _range_from_tracks(tracks: Sequence[float]) -> Range3:
        vals=sorted(max(0.0,float(x)) for x in tracks)
        return Range3(*(round(x,2) for x in vals))

    @staticmethod
    def _step_range(before: Optional[Sequence[float]], after: Optional[Sequence[float]]) -> Optional[Range3]:
        """This step's own savings on its chain, per track (cost before minus after, clamped like running costs):
        low/high = smallest/largest track change, base = the base track's change, so low <= base <= high always holds."""
        if before is None or after is None: return None
        d=[max(0.0,float(b))-max(0.0,float(a)) for b,a in zip(before,after)]
        return Range3(round(min(d),2),round(d[1],2),round(max(d),2))

    @staticmethod
    def _delta(baseline: Optional[float], cost: Optional[Range3]) -> Optional[Range3]:
        if baseline is None or cost is None: return None
        return Range3(round(baseline-cost.high,2),round(baseline-cost.base,2),round(baseline-cost.low,2))

    @staticmethod
    def _factors_from_range(baseline: Optional[float], costs: Optional[Range3]) -> Optional[List[float]]:
        if baseline is None or baseline <= 0 or costs is None: return None
        return [costs.low/baseline,costs.base/baseline,costs.high/baseline]

    def _portfolio(self,e:WarehouseEvidence,status:str,dbu_tracks:Sequence[float],aws_tracks:Optional[Sequence[float]],method:str)->PortfolioEstimate:
        dbu_cost=self._range_from_tracks(dbu_tracks)
        aws_cost=self._range_from_tracks(aws_tracks) if aws_tracks is not None else None
        total_cost=(Range3(round(dbu_cost.low+aws_cost.low,2),round(dbu_cost.base+aws_cost.base,2),round(dbu_cost.high+aws_cost.high,2)) if aws_cost else None)
        total_base=e.total_365d_baseline_cost
        dbu_save=self._delta(e.dbu_365d_baseline_cost,dbu_cost)
        aws_save=self._delta(e.aws_baseline_cost_365,aws_cost)
        total_save=self._delta(total_base,total_cost)
        legacy_base=total_base if total_base is not None else e.dbu_365d_baseline_cost
        legacy_cost=total_cost if total_cost is not None else dbu_cost
        legacy_save=total_save if total_save is not None else dbu_save
        return PortfolioEstimate(
            status,legacy_base,legacy_cost,legacy_save,method,True,
            dbu_365d_baseline_cost=e.dbu_365d_baseline_cost,
            aws_365d_baseline_cost=e.aws_baseline_cost_365,
            total_365d_baseline_cost=total_base,
            dbu_365d_estimated_cost_range=dbu_cost,
            aws_365d_estimated_cost_range=aws_cost,
            total_365d_estimated_cost_range=total_cost,
            dbu_365d_savings_range=dbu_save,
            aws_365d_savings_range=aws_save,
            total_365d_savings_range=total_save,
        )

    def _withheld(self,e:WarehouseEvidence,status:str,method:str)->PortfolioEstimate:
        """Estimate that cannot be priced (needs a canary or a cost model): every range is null, never a zero."""
        return PortfolioEstimate(
            status,e.total_365d_baseline_cost if e.total_365d_baseline_cost is not None else e.dbu_365d_baseline_cost,None,None,method,True,
            dbu_365d_baseline_cost=e.dbu_365d_baseline_cost,aws_365d_baseline_cost=e.aws_baseline_cost_365,total_365d_baseline_cost=e.total_365d_baseline_cost,
        )

    def _apply(self,st:"_ChainState",c:CandidateRecommendation,effect:Mapping[str,Any],original_resource:float,realizations:Sequence[float])->None:
        """Apply one candidate's bundle effect to a chain's evolving cost/resource state."""
        if st.halt is not None: return
        typ=effect.get("type")
        if typ=="migration_cost_override":
            if c.dbu_365d_estimated_cost_range is None:
                st.halt=("REQUIRES_COST_MODEL","Migration candidate lacks a deterministic DBU counterfactual; cost is withheld until required source pricing/resource evidence is available.")
                return
            st.dbu_tracks=[c.dbu_365d_estimated_cost_range.low,c.dbu_365d_estimated_cost_range.base,c.dbu_365d_estimated_cost_range.high]
            if c.aws_365d_estimated_cost_range is not None:
                st.aws_tracks=[c.aws_365d_estimated_cost_range.low,c.aws_365d_estimated_cost_range.base,c.aws_365d_estimated_cost_range.high]
            elif c.aws_365d_baseline_cost is None:
                st.aws_tracks=None
        elif typ=="multiplicative_cost_factor":
            df=self._factors_from_range(c.dbu_365d_baseline_cost,c.dbu_365d_estimated_cost_range)
            af=self._factors_from_range(c.aws_365d_baseline_cost,c.aws_365d_estimated_cost_range)
            if df:
                for i in range(3): st.dbu_tracks[i]*=max(0.0,df[i])
            if st.aws_tracks is not None and af:
                for i in range(3): st.aws_tracks[i]*=max(0.0,af[i])
            rr=effect.get("resource_runtime_factor_range")
            if rr and original_resource>0:
                rf=[safe_float(rr.get("low")),safe_float(rr.get("base")),safe_float(rr.get("high"))] if isinstance(rr,Mapping) else list(rr)
                for i in range(3): st.resource_tracks[i]*=max(0.0,rf[i])
        elif typ=="idle_cluster_minute_reduction":
            # Auto-stop: net cluster-minutes (idle removed minus restarts added, or restarts saved minus idle added)
            # measured at the floor the candidate saw; both terms scale with the track's current floor.
            floor0=max(1,safe_int(effect.get("floor_clusters"),1))
            unit=(safe_float(effect.get("gain_cluster_minutes"))-safe_float(effect.get("loss_cluster_minutes")))/floor0
            for i,realization in enumerate(realizations):
                before=st.resource_tracks[i]
                if before<=0 or unit<=0: continue
                # The saving at the floor the candidate saw, less its overlap with an earlier floor cut (the floor
                # clusters that cut already removed), never more than that cut removed.
                overlap=min(unit*realization*max(0.0,floor0-st.floor_tracks[i]),st.floor_cut_minutes[i])
                st.floor_cut_minutes[i]-=overlap
                removed=min(before,unit*floor0*realization-overlap)
                if removed<=0: continue
                st.auto_stop_wall_minutes[i]+=removed/max(1.0,st.floor_tracks[i])
                ratio=(before-removed)/before
                st.dbu_tracks[i]*=ratio
                if st.aws_tracks is not None: st.aws_tracks[i]*=ratio
                st.resource_tracks[i]=before-removed
        elif typ=="min_cluster_reduction":
            theoretical=max(0.0,safe_float(effect.get("cluster_minutes_to_remove")))
            target_min=max(1,safe_int(effect.get("target_min_clusters"),1))
            effect_realizations=[safe_float(x) for x in effect["realization"]] if effect.get("realization") else realizations
            for i,realization in enumerate(effect_realizations):
                before=st.resource_tracks[i]; floor=st.floor_tracks[i]
                # The track keeps the share of the floor reduction it did not realize.
                st.floor_tracks[i]=floor-max(0.0,floor-target_min)*realization
                if before<=0: continue
                claim=max(0.0,theoretical-st.auto_stop_wall_minutes[i]*max(0.0,floor-target_min))
                removed=min(before,claim*realization)
                st.floor_cut_minutes[i]+=removed
                after=max(0.0,before-removed)
                ratio=after/before
                st.dbu_tracks[i]*=ratio
                if st.aws_tracks is not None: st.aws_tracks[i]*=ratio
                st.resource_tracks[i]=after
        elif typ=="estimate_withheld":
            st.halt=(str(effect.get("status") or "UNAVAILABLE"),str(effect.get("reason") or "Estimate withheld."))
        elif typ=="requires_canary":
            st.halt=("REQUIRES_CANARY","At least one step changes the execution regime without a deterministic counterfactual; portfolio cost is withheld until canary measurement.")
        elif typ!="no_cost_change":
            raise ValueError(f"Unknown bundle effect type: {typ}")

    def estimate(self,e:WarehouseEvidence,steps:List[PlanStep],candidate_by_id:Dict[str,CandidateRecommendation])->PortfolioEstimate:
        """Price an ordered plan as two chains over the same evolving-state model.

        The *authoritative* chain prices only steps that are not PENDING_REEVALUATION: their evidence has not been
        invalidated by an upstream change. The *contingent* chain prices every step using pre-change evidence; it is
        a big-picture upside that must be regenerated after upstream remeasurement and never feeds package economics.
        """
        dbu_base=e.dbu_365d_baseline_cost
        aws_base=e.aws_baseline_cost_365
        if not steps:
            if dbu_base is None:
                return self._withheld(e,"NO_CHANGE","No scenario steps; the forward DBU cost is unavailable.")
            return self._portfolio(e,"NO_CHANGE",[dbu_base]*3,[aws_base]*3 if aws_base is not None else None,"No scenario steps.")

        original_resource=max(0.0,safe_float(e.current_epoch.events.get("cluster_minutes")))
        def fresh()->_ChainState:
            st=_ChainState([dbu_base]*3,[aws_base]*3 if aws_base is not None else None,[original_resource]*3,[float(max(1,e.current_epoch.config.min_clusters))]*3)
            if dbu_base is None:
                st.halt=DBU_BASELINE_WITHHELD[dbu_baseline_blocker(e,self.t)]
            return st
        auth,cont=fresh(),fresh()
        realizations=[self.t.realization_optimistic,self.t.realization_base,self.t.realization_conservative]

        for step in steps:
            c=candidate_by_id[step.recommendation_id]
            effect=c.metadata.get("bundle_effect") or {"type":"no_cost_change"}
            pending=step.execution_state=="PENDING_REEVALUATION"
            step.pricing_basis=PRICING_CONTINGENT if pending else PRICING_AUTHORITATIVE
            chain=cont if pending else auth
            # The step's own savings are differenced on its own chain, so capture that chain's tracks before applying.
            before_dbu,before_aws=list(chain.dbu_tracks),(list(chain.aws_tracks) if chain.aws_tracks is not None else None)
            if not pending: self._apply(auth,c,effect,original_resource,realizations)
            self._apply(cont,c,effect,original_resource,realizations)
            snap=self._portfolio(e,"RUNNING",chain.dbu_tracks,chain.aws_tracks,"Running stateful plan estimate.") if chain.halt is None else None
            step.running_dbu_cost_range_365=snap.dbu_365d_estimated_cost_range if snap else None
            step.running_aws_cost_range_365=snap.aws_365d_estimated_cost_range if snap else None
            step.running_dbu_savings_range_365=snap.dbu_365d_savings_range if snap else None
            step.running_aws_savings_range_365=snap.aws_365d_savings_range if snap else None
            step.running_estimated_cost_range_365=(snap.total_365d_estimated_cost_range or snap.dbu_365d_estimated_cost_range) if snap else None
            step.running_estimated_savings_range_365=(snap.total_365d_savings_range or snap.dbu_365d_savings_range) if snap else None
            step.step_dbu_savings_range_365=self._step_range(before_dbu,chain.dbu_tracks) if snap else None
            step.step_aws_savings_range_365=self._step_range(before_aws,chain.aws_tracks) if snap and snap.aws_365d_savings_range is not None else None

        priced=[x for x in steps if x.pricing_basis==PRICING_AUTHORITATIVE]
        has_pending=len(priced)<len(steps)
        has_migration=lambda group: any(candidate_by_id[x.recommendation_id].knob=="warehouse_type" for x in group)
        method=("Stateful DBU+AWS bundle. Each step changes the evolving cost/resource state. Min-cluster and auto-stop remove remaining cluster-minutes rather than adding standalone savings; migration steps replace the state with a DBU+AWS pre-canary counterfactual.")
        if has_pending:
            method+=" Steps invalidated by an upstream change (PENDING_REEVALUATION) are excluded from this authoritative estimate; regenerate after remeasurement."
        if auth.halt is not None:
            result=self._withheld(e,auth.halt[0],auth.halt[1])
        else:
            provisional=any(candidate_by_id[x.recommendation_id].status=="TEST_REQUIRED" for x in priced)
            status="PRE_CANARY_MODELED_RANGE" if has_migration(priced) else "PROVISIONAL_MODELED_RANGE" if provisional else "MODELED_RANGE"
            result=self._portfolio(e,status,auth.dbu_tracks,auth.aws_tracks,method)
        if has_pending and cont.halt is None:
            result.contingent=self._portfolio(
                e,"PRE_CANARY_MODELED_RANGE" if has_migration(steps) else "PROVISIONAL_MODELED_RANGE",cont.dbu_tracks,cont.aws_tracks,
                "Contingent upside: the same stateful chain priced through steps marked PENDING_REEVALUATION using pre-change evidence. "
                "Not authoritative: it never feeds package economics and must be regenerated after upstream remeasurement.")
        return result


class ScenarioPlanner:
    """Build ordered, dependency-aware scenarios from reviewed candidates."""
    def __init__(self,thresholds:Thresholds): self.t=thresholds; self.bundle=StatefulBundleEstimator(thresholds)

    def _scenario_confidence(self, steps: Sequence[PlanStep]) -> Dict[str, Any]:
        scores=[safe_float(s.confidence.get("score"),0.0) for s in steps if s.confidence]
        if not scores:
            return {"score":None,"level":"UNKNOWN","method":"no steps: nothing to be confident about"}
        score=min(scores)
        return {"score":round(score,3),"level":"HIGH" if score>=self.t.confidence_high_threshold else "MEDIUM" if score>=self.t.confidence_medium_threshold else "LOW","method":"bottleneck minimum of step confidences"}

    def _plan_order(self,eligible:Sequence[CandidateRecommendation])->List[CandidateRecommendation]:
        """Order plan steps by value, then place each step only after every step that invalidates it.

        Value = base savings x confidence / risk (a canary step carries more risk than a HITL-approved one). Priced
        positive steps come first, then unpriced steps (a canary prices them), then zero/negative ones (guardrails and
        performance fixes); ties follow the knob rank. Rounding to cents keeps the order platform-independent.
        """
        def key(c:CandidateRecommendation)->Tuple[int,float,int]:
            base=c.estimated_savings_365
            risk=self.t.plan_risk_canary_change if c.status=="TEST_REQUIRED" else self.t.plan_risk_hitl_change
            group=-1 if "CANARY_GUARDRAIL_BREACHED" in c.blockers else 1 if base is None else 0 if base>0 else 2
            value=0.0 if base is None else round(base*safe_float(c.confidence.get("score"),0.0)/risk,2)
            return (group,-value,PLAN_KNOB_RANK[c.knob])
        # A canary rollback restores the previous configuration: it is first and never waits on another step, whatever
        # the kept steps invalidate (a rollback restores its knob regardless).
        rollbacks=[c for c in sorted(eligible,key=key) if "CANARY_GUARDRAIL_BREACHED" in c.blockers]
        remaining=[c for c in sorted(eligible,key=key) if c not in rollbacks]; ordered:List[CandidateRecommendation]=[]
        while remaining:
            nxt=next((c for c in remaining if not any(c.knob in x.invalidates for x in remaining if x is not c)),None)
            if nxt is None:
                raise ValueError("Plan steps invalidate each other in a cycle: "+", ".join(c.knob for c in remaining))
            ordered.append(nxt); remaining.remove(nxt)
        # A step another plan step invalidates is always pending re-evaluation (its invalidator precedes it), so ready
        # steps come first: what can be applied now is listed before what waits, and the pending tail prices the
        # contingent upside on its own.
        invalidated={k for c in rollbacks+ordered for k in c.invalidates}
        return rollbacks+[c for c in ordered if c.knob not in invalidated]+[c for c in ordered if c.knob in invalidated]

    def _gate(self,c:CandidateRecommendation)->str:
        if c.knob=="warehouse_size" and c.status=="TEST_REQUIRED": return "A_B_CANARY_LATENCY_COST_ERROR"
        if c.knob=="min_clusters" and c.status=="TEST_REQUIRED": return "A_B_CANARY_QUEUE_LATENCY_COST"
        if c.knob=="warehouse_type": return "A_B_CANARY_COST_PERFORMANCE_COMPATIBILITY"
        if c.knob=="auto_stop_minutes" and c.status=="TEST_REQUIRED": return "A_B_CANARY_BILLED_COST"
        return "HITL_APPROVAL_AND_POST_CHANGE_VALIDATION"

    def _compatibility(self,e:WarehouseEvidence,target_type:str)->Dict[str,Any]:
        return migration_compatibility(e,target_type)

    def scenarios(self,e:WarehouseEvidence,candidates:Sequence[CandidateRecommendation])->Tuple[List[Scenario],Optional[str]]:
        by_knob={c.knob:c for c in candidates}; by_id={c.recommendation_id:c for c in candidates}
        steps=[]; pending_invalidated=set()
        for c in self._plan_order([by_knob[k] for k in PLAN_KNOB_RANK if by_knob[k].status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"}]):
            knob=c.knob
            pending = knob in pending_invalidated
            state = "PENDING_REEVALUATION" if pending else "READY_FOR_CANARY" if c.status=="TEST_REQUIRED" else "READY_FOR_HITL"
            gate = "RE_ANALYZE_AFTER_UPSTREAM_CANARY" if pending else self._gate(c)
            instruction = "Re-evaluate this knob using fresh post-change evidence before applying." if pending else ("Apply as canary, validate, then re-measure before downstream changes." if c.status=="TEST_REQUIRED" else "HITL approve and validate post-change evidence.")
            steps.append(PlanStep(len(steps)+1,c.recommendation_id,c.knob,c.current_config,c.recommended_config,c.status,gate,self.t.remeasure_days_after_invalidating_change if c.invalidates else 0,c.invalidates,c.dependencies,state,instruction,confidence=dict(c.confidence)))
            pending_invalidated.update(c.invalidates)
        plan_a_id=scenario_id(e.workspace_id,e.warehouse_id,"Plan A",[s.recommendation_id for s in steps])
        plan_a=Scenario(plan_a_id,e.warehouse_id,e.workspace_id,"Plan A — Optimize current warehouse type","Apply ordered tuning without changing warehouse type. Re-analyze downstream knobs after invalidating changes.",steps,self.bundle.estimate(e,steps,by_id),[],True,{"target_warehouse_type":e.current_epoch.config.warehouse_type,"assessment":"CURRENT_TYPE"},self._scenario_confidence(steps))

        type_c=by_knob["warehouse_type"]; bsteps=[]
        if type_c.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"}:
            bsteps=[PlanStep(1,type_c.recommendation_id,type_c.knob,type_c.current_config,type_c.recommended_config,type_c.status,self._gate(type_c),self.t.remeasure_days_after_invalidating_change,type_c.invalidates,type_c.dependencies,"READY_FOR_CANARY","Run a compatibility-gated A/B canary; successful migration invalidates current-type tuning and requires re-analysis.",confidence=dict(type_c.confidence))]
        # The type the migration was evaluated against: the recommended type, or the one a value gate held. A migration
        # that was never evaluated (too little evidence) has no target, so no checks run against the current type.
        held=(type_c.metadata.get("value_gate") or {}).get("held_change") or {}
        evaluated=type_c.recommended_config.get("warehouse_type") if bsteps else held.get("warehouse_type")
        target=str(evaluated).upper() if evaluated else None
        plan_b_id=scenario_id(e.workspace_id,e.warehouse_id,"Plan B",[s.recommendation_id for s in bsteps])
        pe=self.bundle.estimate(e,bsteps,by_id) if bsteps else self.bundle.estimate(e,[],by_id)
        if target is None:
            plan_b=Scenario(plan_b_id,e.warehouse_id,e.workspace_id,"Plan B — Migration not evaluated",
                "No warehouse-type migration was evaluated: "+(", ".join(type_c.blockers) or str(type_c.metadata.get("rationale") or "no target").rstrip("."))+". Re-run once the evidence gates pass.",
                [],pe,list(type_c.blockers),False,{"target_warehouse_type":None,"assessment":"NOT_EVALUATED","evidence_mode":"A_HITL_FALLBACK","checks":[]},self._scenario_confidence([]))
        else:
            plan_b=Scenario(plan_b_id,e.warehouse_id,e.workspace_id,f"Plan B — {target.title()} migration experiment",f"Canary {target} as a separate branch. The pre-canary cost range uses current-epoch resource bounds and target DBU/AWS pricing; successful migration invalidates current-type tuning and triggers re-analysis.",bsteps,pe,type_c.blockers if bsteps else list(type_c.blockers),False,self._compatibility(e,target),self._scenario_confidence(bsteps))
        return [plan_a,plan_b],plan_a.scenario_id


# ---------------------------------------------------------------------------
# Locked Reporter JSON Contract adapter / canonical JSON packaging
# ---------------------------------------------------------------------------


def _scalar_config(d: Mapping[str, Any]) -> Any:
    """Return the scalar from a one-key config mapping; preserve composite mappings."""
    if len(d) == 1:
        return next(iter(d.values()))
    return dict(d)


_ENTITY_QUERY_SOURCE_CATEGORIES = {"DASHBOARD", "JOB", "NOTEBOOK", "GENIE", "ALERT", "SQL_EDITOR", "OTHER", "UNKNOWN"}
_CLIENT_QUERY_SOURCE_CATEGORIES = {"POWER_BI", "TABLEAU", "LOOKER", "SQL_EDITOR"}


def _contract_confidence(value: Optional[Mapping[str, Any]], *, method: Optional[str] = None) -> rc.Confidence:
    """Translate internal evidence confidence to the locked reporter contract."""
    if not value or value.get("score") is None:
        return rc.Confidence(score=None, level=rc.ConfidenceLevel.UNKNOWN, components={}, method=method)
    score = clamp(safe_float(value.get("score")), 0.0, 1.0)
    level = rc.ConfidenceLevel.HIGH if score >= 0.80 else rc.ConfidenceLevel.MEDIUM if score >= 0.60 else rc.ConfidenceLevel.LOW
    components: Dict[str, Optional[float]] = {}
    for k, v in (value.get("components") or {}).items():
        components[str(k)] = None if v is None else round(clamp(safe_float(v), 0.0, 1.0), 6)
    return rc.Confidence(score=round(score, 6), level=level, components=components, method=method or value.get("method"))


def _contract_range(value: Optional[Range3]) -> Optional[rc.Range3]:
    if value is None:
        return None
    return rc.Range3(round(float(value.low), 2), round(float(value.base), 2), round(float(value.high), 2))


def _contract_component_ranges(
    dbu: Optional[Range3], aws: Optional[Range3], total: Optional[Range3]
) -> rc.ComponentRanges:
    return rc.ComponentRanges(_contract_range(dbu), _contract_range(aws), _contract_range(total))


def _step_projection(step: PlanStep) -> Optional[rc.StepProjection]:
    """Cumulative and own stateful savings for this step, or None when the chain could not price it."""
    if step.running_dbu_savings_range_365 is None and step.running_aws_savings_range_365 is None:
        return None
    total = step.running_estimated_savings_range_365 if step.running_aws_savings_range_365 is not None else None
    dbu_step, aws_step = step.step_dbu_savings_range_365, step.step_aws_savings_range_365
    step_total = None if dbu_step is None or aws_step is None else Range3(
        dbu_step.low + aws_step.low, dbu_step.base + aws_step.base, dbu_step.high + aws_step.high
    )
    return rc.StepProjection(
        pricing_basis=rc.PricingBasis(step.pricing_basis),
        running_savings_365d=_contract_component_ranges(
            step.running_dbu_savings_range_365, step.running_aws_savings_range_365, total
        ),
        step_savings_365d=None if dbu_step is None else _contract_component_ranges(dbu_step, aws_step, step_total),
    )


def _forward_cost_breakdown(e: WarehouseEvidence) -> rc.CostBreakdown:
    dbu = round(e.dbu_365d_baseline_cost, 2) if e.dbu_365d_baseline_cost is not None else None
    aws = round(e.aws_baseline_cost_365, 2) if e.aws_baseline_cost_365 is not None else None
    total = round(dbu + aws, 2) if dbu is not None and aws is not None else None
    if dbu is not None and aws is not None:
        availability = rc.CostAvailability.COMPLETE
    elif dbu is not None or aws is not None:
        availability = rc.CostAvailability.PARTIAL
    else:
        availability = rc.CostAvailability.UNAVAILABLE
    aws_basis = rc.CostBasis.UNAVAILABLE if aws is None else (
        rc.CostBasis.MODELED_CURRENT_EPOCH if e.current_epoch.config.warehouse_type == "SERVERLESS" else rc.CostBasis.MODELED_AWS_LIST_PRICE
    )
    return rc.CostBreakdown(
        dbu_cost=dbu,
        aws_cost=aws,
        total_cost=total,
        availability=availability,
        dbu_basis=rc.CostBasis.FORECAST if dbu is not None else rc.CostBasis.UNAVAILABLE,
        aws_basis=aws_basis,
    )


def _actual_cost_breakdown(e: WarehouseEvidence) -> rc.CostBreakdown:
    """Historical actual is authoritative for Databricks billing; AWS stays null without CUR/effective AWS billing."""
    dbu = round(e.current_cost_365, 2) if e.current_cost_365 is not None else None
    if e.current_epoch.config.warehouse_type == "SERVERLESS" and dbu is not None:
        return rc.CostBreakdown(
            dbu_cost=dbu, aws_cost=0.0, total_cost=dbu,
            availability=rc.CostAvailability.COMPLETE,
            dbu_basis=rc.CostBasis.OBSERVED_DATABRICKS_BILLING,
            aws_basis=rc.CostBasis.MODELED_CURRENT_EPOCH,
        )
    return rc.CostBreakdown(
        dbu_cost=dbu, aws_cost=None, total_cost=None,
        availability=rc.CostAvailability.PARTIAL if dbu is not None else rc.CostAvailability.UNAVAILABLE,
        dbu_basis=rc.CostBasis.OBSERVED_DATABRICKS_BILLING if dbu is not None else rc.CostBasis.UNAVAILABLE,
        aws_basis=rc.CostBasis.UNAVAILABLE,
    )


def _forecast_confidence(e: WarehouseEvidence, t: Thresholds) -> rc.Confidence:
    coverage = clamp(safe_float(e.cost_history_days) / max(1.0, float(t.forecast_long_days)), 0.0, 1.0)
    stability = cost_stability(e.cost_cv_84)
    score = round(0.65 * coverage + 0.35 * (stability or 0.0), 6)  # unknown stability earns no credit
    return _contract_confidence(
        {"score": score, "components": {"history_coverage": coverage, "cost_stability": stability}},
        method="current_epoch_billing_cycle_evidence" if (e.forecast_metadata or {}).get("forecast_model") == "CADENCE" else "current_epoch_runrate_evidence",
    )


def _workload_profile(e: WarehouseEvidence, t: Thresholds) -> rc.WorkloadProfile:
    ep = e.current_epoch
    q, _ = carried_query_view(ep.query)  # the same query evidence the decisions read
    profile = e.attribution.query_source if e.attribution else None
    categories = dict(profile.categories) if profile else {}
    percentages = dict(profile.percentages) if profile else {}

    query_sources: Dict[str, rc.QuerySourceStat] = {}
    for name in sorted(_ENTITY_QUERY_SOURCE_CATEGORIES):
        if name in categories:
            query_sources[name] = rc.QuerySourceStat(
                query_count=max(0, safe_int(categories.get(name))),
                query_pct=round(clamp(safe_float(percentages.get(name)), 0.0, 100.0), 6),
            )
    if not query_sources:
        query_sources["UNKNOWN"] = rc.QuerySourceStat(query_count=max(0, safe_int(q.get("queries_30"))), query_pct=100.0)

    primary = profile.primary_category if profile and profile.primary_category in query_sources else max(
        query_sources, key=lambda k: (query_sources[k].query_count, k)
    )

    clients: Dict[str, rc.ClientApplicationStat] = {}
    for name in sorted(_CLIENT_QUERY_SOURCE_CATEGORIES):
        count = safe_int(categories.get(name)) if name in categories else 0
        if count > 0:
            clients[name] = rc.ClientApplicationStat(query_count=count)

    queries_30 = max(0, safe_int(q.get("queries_30")))
    active_days = min(30, max(0, safe_int(q.get("active_days_30"))))
    per_day = round(queries_30 / active_days, 6) if active_days else 0.0

    if queries_30 < t.min_query_sample_size:
        latency_class = rc.WorkloadLatencyClass.UNKNOWN
        concurrency = rc.ConcurrencyPattern.UNKNOWN
    else:
        p95 = optional_float(q.get("p95_execution_ms_30"))
        latency_class = (
            rc.WorkloadLatencyClass.UNKNOWN if p95 is None
            else rc.WorkloadLatencyClass.INTERACTIVE if p95 <= 5_000
            else rc.WorkloadLatencyClass.BATCH if p95 >= 60_000
            else rc.WorkloadLatencyClass.MIXED
        )
        queued = optional_float(q.get("queued_query_pct_30"))
        concurrency = (
            rc.ConcurrencyPattern.UNKNOWN if queued is None
            else rc.ConcurrencyPattern.STEADY if queued <= t.queue_query_pct_safe
            else rc.ConcurrencyPattern.BURSTY if queued >= t.queue_query_pct_increase_max
            else rc.ConcurrencyPattern.MIXED
        )

    if active_days >= 25:
        activity_pattern = rc.ActivityPattern.CONTINUOUS
    elif active_days <= 8:
        activity_pattern = rc.ActivityPattern.SPORADIC
    elif primary == "JOB":
        activity_pattern = rc.ActivityPattern.SCHEDULED
    else:
        activity_pattern = rc.ActivityPattern.UNKNOWN

    return rc.WorkloadProfile(
        primary_query_source=primary,
        query_sources=query_sources,
        client_applications=clients,
        activity=rc.WorkloadActivity(queries_30d=queries_30, active_days_30d=active_days, queries_per_active_day=per_day),
        workload_characteristics=rc.WorkloadCharacteristics(
            latency_class=latency_class,
            concurrency_pattern=concurrency,
            activity_pattern=activity_pattern,
        ),
        multi_label=True,
    )


def _photon_config_observation(e: WarehouseEvidence) -> rc.PhotonConfigObservation:
    p = e.photon_observation
    if p is None:
        return rc.PhotonConfigObservation(
            status=rc.PhotonStatus.UNKNOWN,
            evidence_source="system.billing.usage.product_features.is_photon",
            confidence=rc.Confidence(None, rc.ConfidenceLevel.UNKNOWN, {}, "no_current_epoch_billing_feature_evidence"),
            enabled_usage_pct=None, known_feature_coverage_pct=None, freshness_days=None,
        )
    status = rc.PhotonStatus(p.status) if p.status in {x.value for x in rc.PhotonStatus} else rc.PhotonStatus.UNKNOWN
    return rc.PhotonConfigObservation(
        status=status,
        evidence_source=p.evidence_source,
        confidence=_contract_confidence(p.confidence, method="signed_current_epoch_dbu_feature_coverage"),
        enabled_usage_pct=None if p.enabled_usage_pct is None else round(float(p.enabled_usage_pct), 6),
        known_feature_coverage_pct=round(float(p.known_feature_coverage_pct), 6),
        freshness_days=p.freshness_days,
    )


def _technique_id(c: CandidateRecommendation) -> str:
    knob = c.knob
    cur = _scalar_config(c.current_config)
    rec = _scalar_config(c.recommended_config)
    if knob == "warehouse_size":
        if cur in SIZE_ORDER and rec in SIZE_ORDER and cur != rec:
            return "WH-SIZE-DOWNSIZE-001" if SIZE_ORDER.index(rec) < SIZE_ORDER.index(cur) else "WH-SIZE-UPSIZE-001"
        return "WH-SIZE-RIGHTSIZE-001"
    if knob == "max_clusters":
        if isinstance(cur, int) and isinstance(rec, int) and rec < cur:
            return "WH-MAX-CLUSTER-REDUCE-001"
        if isinstance(cur, int) and isinstance(rec, int) and rec > cur:
            return "WH-MAX-CLUSTER-INCREASE-001"
        return "WH-MAX-CLUSTER-ASSESS-001"
    if knob == "min_clusters":
        if isinstance(cur, int) and isinstance(rec, int) and rec < cur:
            return "WH-MIN-CLUSTER-REDUCE-001"
        if isinstance(cur, int) and isinstance(rec, int) and rec > cur:
            return "WH-MIN-CLUSTER-INCREASE-001"
        return "WH-MIN-CLUSTER-ASSESS-001"
    if knob == "auto_stop_minutes":
        if isinstance(cur, int) and isinstance(rec, int) and rec < cur:
            return "WH-AUTOSTOP-REDUCE-001"
        return "WH-AUTOSTOP-ASSESS-001"
    if knob == "warehouse_type":
        if rec == "SERVERLESS" and cur != rec:
            return "WH-SERVERLESS-MIGRATE-001"
        if cur == "SERVERLESS" and rec in {"PRO", "CLASSIC"}:
            return "WH-PRO-MIGRATE-001"
        return "WH-WAREHOUSE-TYPE-ASSESS-001"
    raise ValueError(f"No technique catalog mapping for knob {knob!r}")


def _objectives(c: CandidateRecommendation) -> List[rc.Objective]:
    objective = str(c.objective or "").upper()
    if c.knob == "warehouse_type":
        return [rc.Objective.MIGRATION, rc.Objective.COST_AND_PERFORMANCE, rc.Objective.OPERATIONS]
    mapping = {
        "COST": [rc.Objective.COST],
        "PERFORMANCE": [rc.Objective.PERFORMANCE],
        "PERFORMANCE_AND_COST": [rc.Objective.COST_AND_PERFORMANCE],
        "COST_AND_PERFORMANCE": [rc.Objective.COST_AND_PERFORMANCE],
        "COST_GUARDRAIL": [rc.Objective.COST_GUARDRAIL],
        "STABILITY": [rc.Objective.STABILITY],
        "RELIABILITY": [rc.Objective.RELIABILITY],
        "OPERATIONS": [rc.Objective.OPERATIONS],
        "PERFORMANCE_AND_OPERATIONS": [rc.Objective.PERFORMANCE, rc.Objective.OPERATIONS],
    }
    return mapping.get(objective, [rc.Objective.OPERATIONS])


def _recommendation_status(value: str) -> rc.RecommendationStatus:
    try:
        return rc.RecommendationStatus(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported recommendation status for locked contract: {value}") from exc


def _estimation_status(c: CandidateRecommendation) -> rc.EstimationStatus:
    if c.status in {"NO_CHANGE", "NOT_APPLICABLE"}:
        return rc.EstimationStatus.NO_DELTA_MODELED
    if c.total_365d_estimated_cost_range is None and c.dbu_365d_estimated_cost_range is None:
        # Blocked, or a change whose estimate was withheld (e.g. no published Serverless DBU rate): nothing was priced.
        return rc.EstimationStatus.UNAVAILABLE
    if (c.metadata.get("bundle_effect") or {}).get("type") == "no_cost_change":
        # A guardrail/stability change (e.g. tightening max clusters) is deliberately given no priced delta.
        return rc.EstimationStatus.NO_DELTA_MODELED
    if c.knob == "warehouse_type" and c.status == "TEST_REQUIRED":
        return rc.EstimationStatus.PRE_CANARY_MODELED_RANGE
    if c.status == "TEST_REQUIRED":
        return rc.EstimationStatus.PROVISIONAL_MODELED_RANGE
    return rc.EstimationStatus.MODELED_RANGE


def _dependency_refs(values: Sequence[str]) -> List[rc.DependencyRef]:
    refs = []
    seen = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        refs.append(rc.DependencyRef(rc.DependencyType.KNOB, value, None))
    return refs


def _tunable_knobs(values: Sequence[str]) -> List[rc.TunableKnob]:
    out = []
    for value in values:
        try:
            knob = rc.TunableKnob(value)
        except ValueError:
            continue
        if knob not in out:
            out.append(knob)
    return out


def _add_evidence(
    registry: Dict[str, rc.EvidenceRecord],
    *,
    e: WarehouseEvidence,
    contract_epoch_id: str,
    metric: str,
    value: Any,
    unit: str,
    source: str,
    source_field: str,
    observation_window: str,
    threshold: Any,
    comparator: str,
    interpretation: str,
    epoch_scope: str = "CURRENT",
) -> str:
    evidence_id = rc.make_evidence_id(
        workspace_id=e.workspace_id,
        warehouse_id=e.warehouse_id,
        epoch_id=contract_epoch_id,
        metric=metric,
        value=value,
        unit=unit,
        source=source,
        source_field=source_field,
        observation_window=observation_window,
        threshold=threshold,
        comparator=comparator,
    )
    record = rc.EvidenceRecord(
        evidence_id=evidence_id,
        metric=metric,
        value=value,
        unit=unit,
        source=source,
        source_field=source_field,
        epoch_scope=epoch_scope,
        observation_window=observation_window,
        threshold=threshold,
        comparator=comparator,
        interpretation=interpretation,
    )
    if evidence_id in registry and registry[evidence_id] != record:
        raise ValueError(f"Evidence ID collision for {evidence_id}")
    registry[evidence_id] = record
    return evidence_id


def _build_evidence_registry(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    scenarios: Sequence[Scenario],
    contract_epoch_id: str,
) -> Tuple[Dict[str, rc.EvidenceRecord], Dict[str, str], Dict[Tuple[str, str], str], List[str]]:
    """Build one deduplicated evidence registry and return old-finding/check mappings plus shared cost refs."""
    registry: Dict[str, rc.EvidenceRecord] = {}
    finding_map: Dict[str, str] = {}
    compatibility_map: Dict[Tuple[str, str], str] = {}

    for c in candidates:
        for f in c.findings:
            eid = _add_evidence(
                registry, e=e, contract_epoch_id=contract_epoch_id,
                metric=f.metric, value=f.value, unit=f.unit or "value",
                source=f.source_table, source_field=f.source_field,
                observation_window=f.window or "current_epoch", epoch_scope=f.epoch_scope,
                threshold=f.threshold, comparator=f.comparator or "",
                interpretation=f.interpretation,
            )
            finding_map[f.finding_id] = eid

    # Economics are decision-relevant and therefore part of the evidence fingerprint.
    shared_cost_refs = [
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="actual_dbu_cost_365d", value=round_or_none(e.current_cost_365, 2), unit=e.currency,
            source="system.billing.usage", source_field="usage_quantity × historical effective list price",
            observation_window="trailing_365d", threshold=None, comparator="",
            interpretation="Observed trailing-365-day Databricks SQL list cost attributed to the warehouse.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="forward_dbu_cost_365d", value=round_or_none(e.dbu_365d_baseline_cost, 2), unit=e.currency,
            source="derived", source_field=MODEL_VERSION,
            observation_window="forward_365d", threshold=None, comparator="",
            interpretation="No-change forward DBU cost forecast anchored to the current configuration epoch.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="forward_aws_cost_365d", value=None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365, 2), unit=e.currency,
            source="derived", source_field="aws_current_epoch_cost_model",
            observation_window="forward_365d", threshold=None, comparator="",
            interpretation="Forward customer AWS infrastructure cost model for the current configuration epoch; null when unavailable.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="cost_history_days", value=e.cost_history_days, unit="days",
            source="system.billing.usage", source_field="usage_date",
            observation_window="current_epoch", threshold=None, comparator="",
            interpretation="Calendar-day coverage available to the forward cost model.",
        ),
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="billed_cost_days", value=ready_billed_days(e), unit="days",
            source="system.billing.usage", source_field="usage_date/usage_quantity",
            observation_window="current_epoch", threshold=rc.REEVALUATION_READY_BILLED_DAYS, comparator=">= readiness",
            interpretation="Settled days of the current epoch with billed usage: the current configuration is ready once it reaches the readiness count.",
        ),
    ]
    # Cost-basis quality signal: package evidence only. It is not a shared cost ref, so it never enters candidate
    # evidence refs (and their IDs); no decision, confidence or economics reads it.
    _add_evidence(
        registry, e=e, contract_epoch_id=contract_epoch_id,
        metric="billing_correction_dbu_share_365d", value=round_or_none((e.forecast_metadata or {}).get("billing_correction_dbu_share_365"), 6), unit="ratio",
        source="system.billing.usage", source_field="record_type",
        observation_window="trailing_365d", threshold=None, comparator="",
        interpretation="Retracted DBU (RETRACTION records) per originally billed DBU in the settled trailing window; can exceed 1 when a day is corrected more than once. A cost-basis quality signal, not used in decisions; null when not collected.",
    )

    # The channel is observed, never recommended: a Preview warehouse gets an informational record only (package
    # evidence; no candidate references it, so no decision or ID depends on it).
    if e.current_epoch.config.warehouse_channel == "PREVIEW":
        _add_evidence(
            registry, e=e, contract_epoch_id=contract_epoch_id,
            metric="warehouse_channel", value="PREVIEW", unit="state",
            source="system.compute.warehouses", source_field="warehouse_channel",
            observation_window="current_epoch", threshold=None, comparator="",
            interpretation="Informational: the warehouse runs on the Preview channel. No recommendation is made; confirm no workload depends on Preview-only behavior before any move to Current.",
        )

    for s in scenarios:
        for check in (s.compatibility or {}).get("checks", []) or []:
            name = str(check.get("check") or "compatibility_check")
            status = str(check.get("status") or "UNKNOWN")
            source = str(check.get("source") or (s.compatibility or {}).get("evidence_mode") or "compatibility")
            details = check.get("evidence")
            eid = _add_evidence(
                registry, e=e, contract_epoch_id=contract_epoch_id,
                metric=f"compatibility.{name}", value={"status": status, "evidence": details}, unit="state",
                source=source, source_field=name, observation_window="point_in_time",
                threshold="PASS", comparator="compatibility_check",
                interpretation=f"Compatibility check {name} returned {status}.",
            )
            compatibility_map[(s.name, name)] = eid

    return registry, finding_map, compatibility_map, shared_cost_refs


def _candidate_contracts(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    contract_epoch_id: str,
    evidence: Dict[str, rc.EvidenceRecord],
    finding_map: Dict[str, str],
    shared_cost_refs: Sequence[str],
) -> Tuple[List[rc.CandidateRecommendation], Dict[str, rc.CandidateRecommendation]]:
    baseline = _forward_cost_breakdown(e)
    out: List[rc.CandidateRecommendation] = []
    by_old_id: Dict[str, rc.CandidateRecommendation] = {}
    for c in candidates:
        refs = []
        for f in c.findings:
            mapped = finding_map.get(f.finding_id)
            if mapped and mapped not in refs:
                refs.append(mapped)
        for cost_ref in shared_cost_refs:
            if cost_ref not in refs:
                refs.append(cost_ref)
        technique = _technique_id(c)
        current_value = _scalar_config(c.current_config)
        recommended_value = _scalar_config(c.recommended_config)
        candidate_id = rc.make_candidate_id(
            workspace_id=e.workspace_id,
            warehouse_id=e.warehouse_id,
            epoch_id=contract_epoch_id,
            technique_id=technique,
            knob=c.knob,
            current_value=current_value,
            recommended_value=recommended_value,
            evidence_refs=refs,
            policy_version=POLICY_VERSION,
            analyzer_version=ANALYZER_VERSION,
            optimizer_version=OPTIMIZER_VERSION,
            estimator_version=ESTIMATOR_VERSION,
            reviewer_version=REVIEWER_VERSION,
            decision_graph_version=DECISION_VERSION,
        )
        item = rc.CandidateRecommendation(
            candidate_id=candidate_id,
            technique_id=technique,
            knob=rc.TunableKnob(c.knob),
            status=_recommendation_status(c.status),
            current_value=current_value,
            recommended_value=recommended_value,
            objectives=_objectives(c),
            rationale=str(c.metadata.get("rationale") or f"Deterministic {c.knob} policy assessment."),
            evidence_refs=refs,
            blockers=list(c.blockers),
            dependencies=_dependency_refs(c.dependencies),
            invalidates=_tunable_knobs(c.invalidates),
            confidence=_contract_confidence(c.confidence, method="deterministic_evidence_score"),
            independent_economics=rc.IndependentEconomics(
                is_additive=False,
                estimation_status=_estimation_status(c),
                baseline_365d=baseline,
                estimated_cost_365d=_contract_component_ranges(
                    c.dbu_365d_estimated_cost_range, c.aws_365d_estimated_cost_range, c.total_365d_estimated_cost_range
                ),
                savings_365d=_contract_component_ranges(
                    c.dbu_365d_savings_range, c.aws_365d_savings_range, c.total_365d_savings_range
                ),
            ),
        )
        out.append(item)
        by_old_id[c.recommendation_id] = item
    return out, by_old_id


def _compatibility_contract(
    s: Scenario,
    compatibility_map: Mapping[Tuple[str, str], str],
) -> rc.Compatibility:
    comp = s.compatibility or {}
    assessment = str(comp.get("assessment") or "CURRENT_TYPE")
    # A planner FAIL (a check failed) is incompatible and a migration whose every check passed is compatible. An empty
    # alternative (no migration step) is never COMPATIBLE: it would hash to the same scenario ID as an empty recommended
    # plan, which is always COMPATIBLE. A migration that was not evaluated has no checks: UNKNOWN.
    migrates = any(st.knob == "warehouse_type" for st in s.steps)
    if assessment in {"CURRENT_TYPE", "COMPATIBLE"} or (assessment == "PASS" and migrates):
        status = rc.CompatibilityStatus.COMPATIBLE
    elif assessment in {"INCOMPATIBLE", "BLOCKED", "FAIL"}:
        status = rc.CompatibilityStatus.INCOMPATIBLE
    elif assessment == "NOT_EVALUATED":
        status = rc.CompatibilityStatus.UNKNOWN
    else:
        status = rc.CompatibilityStatus.REQUIRES_VALIDATION
    mode_raw = str(comp.get("evidence_mode") or ("SYSTEM_TABLE_ONLY" if assessment == "CURRENT_TYPE" else "A_HITL_FALLBACK"))
    try:
        mode = rc.CompatibilityMode(mode_raw)
    except ValueError:
        mode = rc.CompatibilityMode.A_HITL_FALLBACK

    checks: List[rc.CompatibilityCheck] = []
    for check in comp.get("checks", []) or []:
        name = str(check.get("check") or "compatibility_check")
        raw_status = str(check.get("status") or "UNKNOWN")
        status_map = {
            "PASS": rc.CheckStatus.PASS,
            "FAIL": rc.CheckStatus.FAIL,
            "UNKNOWN": rc.CheckStatus.UNKNOWN,
            "NOT_APPLICABLE": rc.CheckStatus.NOT_APPLICABLE,
            "PASS_WITH_LIMITATION": rc.CheckStatus.UNKNOWN,
        }
        check_status = status_map.get(raw_status, rc.CheckStatus.UNKNOWN)
        eid = compatibility_map.get((s.name, name))
        checks.append(rc.CompatibilityCheck(
            check=name,
            status=check_status,
            requires_hitl=check_status == rc.CheckStatus.UNKNOWN or raw_status == "PASS_WITH_LIMITATION",
            evidence_refs=[eid] if eid else [],
            details=None if check.get("evidence") is None else canonical_json(check.get("evidence")),
        ))
    return rc.Compatibility(status=status, mode=mode, checks=checks)


# Planner step states -> locked contract states. READY_FOR_HITL (apply after human approval) is the planner's name for
# the contract's READY; an unmapped state is a bug and must not silently degrade to READY.
_CONTRACT_EXECUTION_STATE = {
    "READY": rc.ExecutionState.READY,
    "READY_FOR_HITL": rc.ExecutionState.READY,
    "READY_FOR_CANARY": rc.ExecutionState.READY_FOR_CANARY,
    "PENDING_REEVALUATION": rc.ExecutionState.PENDING_REEVALUATION,
    "BLOCKED": rc.ExecutionState.BLOCKED,
}


def _scenario_estimation_status(value: str) -> rc.EstimationStatus:
    if value == "NO_CHANGE":  # a plan with no steps changes nothing: a known zero, not an unavailable estimate
        return rc.EstimationStatus.NO_DELTA_MODELED
    try:
        return rc.EstimationStatus(value)
    except ValueError:
        return rc.EstimationStatus.UNAVAILABLE


def _scenario_contracts(
    e: WarehouseEvidence,
    scenarios: Sequence[Scenario],
    contract_epoch_id: str,
    candidates_by_old_id: Mapping[str, rc.CandidateRecommendation],
    candidate_contracts: Sequence[rc.CandidateRecommendation],
    compatibility_map: Mapping[Tuple[str, str], str],
) -> Tuple[List[rc.Scenario], Dict[str, rc.Scenario]]:
    baseline = _forward_cost_breakdown(e)
    out: List[rc.Scenario] = []
    by_old_id: Dict[str, rc.Scenario] = {}

    for s in scenarios:
        compatibility = _compatibility_contract(s, compatibility_map)
        ordered_candidates = [candidates_by_old_id[st.recommendation_id] for st in s.steps if st.recommendation_id in candidates_by_old_id]
        candidate_refs = [c.candidate_id for c in ordered_candidates]
        scenario_type = rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION if any(c.knob == rc.TunableKnob.WAREHOUSE_TYPE for c in ordered_candidates) else rc.ScenarioType.IN_PLACE_OPTIMIZATION
        scenario_id = rc.make_scenario_id(
            workspace_id=e.workspace_id,
            warehouse_id=e.warehouse_id,
            epoch_id=contract_epoch_id,
            scenario_type=scenario_type.value,
            ordered_candidate_ids=candidate_refs,
            compatibility_status=compatibility.status.value,
            policy_version=POLICY_VERSION,
            bundle_estimator_version=BUNDLE_MODEL_VERSION,
            decision_graph_version=DECISION_VERSION,
        )
        plan_steps: List[rc.PlanStep] = []
        next_step = 1
        if scenario_type == rc.ScenarioType.WAREHOUSE_TYPE_MIGRATION and compatibility.status == rc.CompatibilityStatus.REQUIRES_VALIDATION:
            plan_steps.append(rc.PlanStep(
                step=next_step,
                step_type=rc.PlanStepType.COMPATIBILITY_VALIDATION,
                candidate_id=None, technique_id=None, knob=None,
                action="VALIDATE_WAREHOUSE_TYPE_COMPATIBILITY",
                current_value=None, recommended_value=None,
                execution_state=rc.ExecutionState.REQUIRED,
                confidence=_contract_confidence(s.confidence, method="compatibility_and_scenario_evidence"),
                validation=rc.ValidationGate(rc.ValidationType.COMPATIBILITY, [], None, "CONTINUE_IF_PASS"),
                dependencies=[], invalidates=[],
            ))
            next_step += 1

        for st in s.steps:
            c = candidates_by_old_id.get(st.recommendation_id)
            if c is None:
                continue
            validation_raw = str(st.validation_gate or "")
            if "CANARY" in validation_raw:
                validation_type = rc.ValidationType.A_B_CANARY
                success_metrics = ["p95_execution_ms", "p99_execution_ms", "failure_rate", "queue_wait", "dbu_cost", "aws_cost"]
            elif "COMPAT" in validation_raw:
                validation_type = rc.ValidationType.COMPATIBILITY
                success_metrics = []
            elif "RE_ANALYZE" in validation_raw or st.execution_state == "PENDING_REEVALUATION":
                validation_type = rc.ValidationType.HITL
                success_metrics = []
            else:
                validation_type = rc.ValidationType.NONE
                success_metrics = []
            try:
                state = _CONTRACT_EXECUTION_STATE[st.execution_state]
            except KeyError:
                raise ValueError(f"Planner execution state {st.execution_state!r} has no locked-contract mapping") from None
            if compatibility.status == rc.CompatibilityStatus.INCOMPATIBLE:
                state = rc.ExecutionState.BLOCKED  # a failed compatibility check: no canary until it is resolved
            plan_steps.append(rc.PlanStep(
                step=next_step,
                step_type=rc.PlanStepType.CANDIDATE,
                candidate_id=c.candidate_id,
                technique_id=c.technique_id,
                knob=c.knob,
                action=None,
                current_value=c.current_value,
                recommended_value=c.recommended_value,
                execution_state=state,
                confidence=c.confidence,
                validation=rc.ValidationGate(
                    validation_type=validation_type,
                    success_metrics=success_metrics,
                    remeasure_days=st.remeasure_days if validation_type == rc.ValidationType.REMEASURE else None,
                    action_after=None,
                ),
                dependencies=_dependency_refs(st.dependencies),
                invalidates=_tunable_knobs(st.invalidates),
                projected_economics=_step_projection(st),
            ))
            next_step += 1

        if any(step.invalidates for step in plan_steps if step.step_type == rc.PlanStepType.CANDIDATE):
            plan_steps.append(rc.PlanStep(
                step=next_step,
                step_type=rc.PlanStepType.REMEASURE,
                candidate_id=None, technique_id=None, knob=None,
                action="REMEASURE_AND_REGENERATE",
                current_value=None, recommended_value=None,
                execution_state=rc.ExecutionState.REQUIRED,
                confidence=rc.Confidence(1.0, rc.ConfidenceLevel.HIGH, {"policy_gate": 1.0}, "policy_gate"),
                validation=rc.ValidationGate(
                    rc.ValidationType.REMEASURE, [],
                    remeasure_days=max(1, max((st.remeasure_days for st in s.steps if st.invalidates), default=7)),
                    action_after="REGENERATE_RECOMMENDATIONS",
                ),
                dependencies=[], invalidates=[],
            ))

        scenario_evidence_refs: List[str] = []
        for c in ordered_candidates:
            for ref in c.evidence_refs:
                if ref not in scenario_evidence_refs:
                    scenario_evidence_refs.append(ref)
        for check in compatibility.checks:
            for ref in check.evidence_refs:
                if ref not in scenario_evidence_refs:
                    scenario_evidence_refs.append(ref)

        pe = s.portfolio_estimate
        ct = pe.contingent
        economics = rc.ScenarioEconomics(
            estimation_status=_scenario_estimation_status(pe.estimate_status),
            baseline_365d=baseline,
            estimated_cost_365d=_contract_component_ranges(
                pe.dbu_365d_estimated_cost_range, pe.aws_365d_estimated_cost_range, pe.total_365d_estimated_cost_range
            ),
            savings_365d=_contract_component_ranges(
                pe.dbu_365d_savings_range, pe.aws_365d_savings_range, pe.total_365d_savings_range
            ),
            method=pe.method,
            is_additive=False,
            contingent_estimated_cost_365d=_contract_component_ranges(
                ct.dbu_365d_estimated_cost_range, ct.aws_365d_estimated_cost_range, ct.total_365d_estimated_cost_range
            ) if ct is not None else None,
            contingent_savings_365d=_contract_component_ranges(
                ct.dbu_365d_savings_range, ct.aws_365d_savings_range, ct.total_365d_savings_range
            ) if ct is not None else None,
        )
        item = rc.Scenario(
            scenario_id=scenario_id,
            scenario_name=s.name,
            scenario_type=scenario_type,
            status=rc.ScenarioStatus.RECOMMENDED if s.recommended else rc.ScenarioStatus.ALTERNATIVE,
            candidate_refs=candidate_refs,
            evidence_refs=scenario_evidence_refs,
            confidence=_contract_confidence(s.confidence, method="minimum_step_confidence"),
            compatibility=compatibility,
            portfolio_economics=economics,
            execution_plan=plan_steps,
        )
        out.append(item)
        by_old_id[s.scenario_id] = item
    return out, by_old_id


def _overall_status(candidates: Sequence[rc.CandidateRecommendation], scenarios: Sequence[rc.Scenario]) -> rc.AssessmentStatus:
    """Package status from the recommended plan; the migration experiment alone never makes a warehouse optimizable.

    Steps to apply in the recommended plan: OPTIMIZATION_AVAILABLE (REVIEW_REQUIRED when every step is a review).
    An empty recommended plan with a compatible-or-unvalidated alternative whose priced base savings are positive:
    REVIEW_REQUIRED. Otherwise
    the most severe reason nothing ships: BLOCKED, then INSUFFICIENT_EVIDENCE, else NO_CHANGE.
    """
    S = rc.RecommendationStatus
    by_id = {c.candidate_id: c.status for c in candidates}

    def step_statuses(s: rc.Scenario) -> set:
        return {by_id[x.candidate_id] for x in s.execution_plan if x.candidate_id in by_id}

    plan = set().union(*(step_statuses(s) for s in scenarios if s.status == rc.ScenarioStatus.RECOMMENDED))
    if plan & {S.ACTIONABLE, S.TEST_REQUIRED}:
        return rc.AssessmentStatus.OPTIMIZATION_AVAILABLE
    if S.REVIEW in plan:
        return rc.AssessmentStatus.REVIEW_REQUIRED
    for s in scenarios:
        total = s.portfolio_economics.savings_365d.total
        if (s.status == rc.ScenarioStatus.ALTERNATIVE and step_statuses(s) and total is not None and total.base > 0
                and s.compatibility.status != rc.CompatibilityStatus.INCOMPATIBLE):
            return rc.AssessmentStatus.REVIEW_REQUIRED
    statuses = set(by_id.values())
    if S.BLOCKED in statuses:
        return rc.AssessmentStatus.BLOCKED
    if S.INSUFFICIENT_EVIDENCE in statuses:
        return rc.AssessmentStatus.INSUFFICIENT_EVIDENCE
    return rc.AssessmentStatus.NO_CHANGE


def _assessment_contract(e: WarehouseEvidence, candidates: Sequence[rc.CandidateRecommendation], scenarios: Sequence[rc.Scenario], t: Thresholds) -> rc.Assessment:
    counts = {status: 0 for status in rc.RecommendationStatus}
    for c in candidates:
        counts[c.status] += 1
    overall = _overall_status(candidates, scenarios)

    q, _ = carried_query_view(e.current_epoch.query)
    query_ok = safe_int(q.get("queries_30")) >= t.min_query_sample_size
    cost_ok = e.cost_history_days >= t.min_cost_history_days
    epoch_ok = ready_billed_days(e) >= t.readiness_min_billed_days
    source_flags = [True, bool(q), bool(e.current_epoch.events), e.cost_history_days > 0, bool(e.sql_sku_prices)]
    coverage = round(100.0 * sum(1 for x in source_flags if x) / len(source_flags), 6)
    score = round(0.25 * float(query_ok) + 0.25 * float(cost_ok) + 0.20 * float(epoch_ok) + 0.30 * coverage / 100.0, 6)
    quality_status = rc.EvidenceQualityStatus.SUFFICIENT if query_ok and cost_ok and epoch_ok and coverage >= 80 else rc.EvidenceQualityStatus.PARTIAL if coverage >= 50 else rc.EvidenceQualityStatus.INSUFFICIENT
    return rc.Assessment(
        overall_status=overall,
        summary=rc.AssessmentSummary(
            candidate_count=len(candidates),
            actionable_count=counts[rc.RecommendationStatus.ACTIONABLE],
            test_required_count=counts[rc.RecommendationStatus.TEST_REQUIRED],
            review_count=counts[rc.RecommendationStatus.REVIEW],
            blocked_count=counts[rc.RecommendationStatus.BLOCKED],
            no_change_count=counts[rc.RecommendationStatus.NO_CHANGE],
            not_applicable_count=counts[rc.RecommendationStatus.NOT_APPLICABLE],
            insufficient_evidence_count=counts[rc.RecommendationStatus.INSUFFICIENT_EVIDENCE],
        ),
        evidence_quality=rc.EvidenceQuality(
            status=quality_status,
            confidence=_contract_confidence({"score": score, "components": {"query_sample": float(query_ok), "cost_history": float(cost_ok), "epoch_maturity": float(epoch_ok), "source_coverage": coverage / 100.0}}, method="minimum_source_and_epoch_coverage"),
            query_sample_sufficient=query_ok,
            cost_history_sufficient=cost_ok,
            epoch_mature=epoch_ok,
            source_coverage_pct=coverage,
        ),
    )


def _economics_point_from_scenario(s: rc.Scenario, bound: rc.BoundName, *, savings: bool) -> rc.EconomicsPoint:
    ranges = s.portfolio_economics.savings_365d if savings else s.portfolio_economics.estimated_cost_365d
    attr = {rc.BoundName.LOW: "low", rc.BoundName.BASE: "base", rc.BoundName.HIGH: "high"}[bound]
    # Cost range has inverse relation to savings range: LOW savings corresponds HIGH cost.
    if not savings:
        attr = {rc.BoundName.LOW: "high", rc.BoundName.BASE: "base", rc.BoundName.HIGH: "low"}[bound]
    def get(r: Optional[rc.Range3]) -> Optional[float]:
        return None if r is None else round(float(getattr(r, attr)), 2)
    return rc.EconomicsPoint(get(ranges.dbu), get(ranges.aws), get(ranges.total))


def _package_economics(
    e: WarehouseEvidence,
    scenarios: Sequence[rc.Scenario],
    recommended_scenario_id: Optional[str],
) -> rc.PackageEconomics:
    baseline = _forward_cost_breakdown(e)
    own_priced = [
        s for s in scenarios
        if s.status not in {rc.ScenarioStatus.BLOCKED, rc.ScenarioStatus.INSUFFICIENT_EVIDENCE}
        and s.portfolio_economics.savings_365d.total is not None
    ]
    # The envelope's base is the recommended scenario, so nothing is summarized unless that scenario is itself priced.
    assessable = own_priced if recommended_scenario_id and any(s.scenario_id == recommended_scenario_id for s in own_priced) else []
    included = [s.scenario_id for s in assessable]
    own_priced_ids = {s.scenario_id for s in own_priced}

    def exclusion_reason(s: rc.Scenario) -> str:
        if s.scenario_id not in own_priced_ids:
            return "SCENARIO_ECONOMICS_UNAVAILABLE"  # this scenario has no authoritative price
        return "RECOMMENDED_SCENARIO_ECONOMICS_UNAVAILABLE" if recommended_scenario_id else "NO_RECOMMENDED_SCENARIO"

    excluded = [rc.ExcludedScenario(s.scenario_id, exclusion_reason(s)) for s in scenarios if s.scenario_id not in included]
    if not assessable or recommended_scenario_id is None:
        return rc.PackageEconomics(
            currency=e.currency,
            baseline_365d=baseline,
            estimated_savings_365d=None,
            estimated_cost_365d=None,
            scenario_envelope=rc.ScenarioEnvelope(None, None, None),
            included_scenario_ids=[],
            excluded_scenarios=excluded,
            semantics=rc.PackageEconomicsSemantics("SCENARIO_ENVELOPE", False, False),
        )

    low_s = min(assessable, key=lambda s: s.portfolio_economics.savings_365d.total.low)  # type: ignore[union-attr]
    high_s = max(assessable, key=lambda s: s.portfolio_economics.savings_365d.total.high)  # type: ignore[union-attr]
    base_s = next(s for s in assessable if s.scenario_id == recommended_scenario_id)
    low = round(low_s.portfolio_economics.savings_365d.total.low, 2)  # type: ignore[union-attr]
    base = round(base_s.portfolio_economics.savings_365d.total.base, 2)  # type: ignore[union-attr]
    high = round(high_s.portfolio_economics.savings_365d.total.high, 2)  # type: ignore[union-attr]
    savings_range = rc.Range3(low, base, high)
    total_baseline = baseline.total_cost
    estimated_cost = None if total_baseline is None else rc.Range3(
        round(total_baseline - high, 2), round(total_baseline - base, 2), round(total_baseline - low, 2)
    )
    envelope = rc.ScenarioEnvelope(
        minimum_savings=rc.ScenarioEnvelopePoint(
            low_s.scenario_id, rc.BoundName.LOW,
            _economics_point_from_scenario(low_s, rc.BoundName.LOW, savings=True),
            _economics_point_from_scenario(low_s, rc.BoundName.LOW, savings=False),
        ),
        recommended_base=rc.ScenarioEnvelopePoint(
            base_s.scenario_id, rc.BoundName.BASE,
            _economics_point_from_scenario(base_s, rc.BoundName.BASE, savings=True),
            _economics_point_from_scenario(base_s, rc.BoundName.BASE, savings=False),
        ),
        maximum_savings=rc.ScenarioEnvelopePoint(
            high_s.scenario_id, rc.BoundName.HIGH,
            _economics_point_from_scenario(high_s, rc.BoundName.HIGH, savings=True),
            _economics_point_from_scenario(high_s, rc.BoundName.HIGH, savings=False),
        ),
    )
    return rc.PackageEconomics(
        currency=e.currency,
        baseline_365d=baseline,
        estimated_savings_365d=savings_range,
        estimated_cost_365d=estimated_cost,
        scenario_envelope=envelope,
        included_scenario_ids=included,
        excluded_scenarios=excluded,
        semantics=rc.PackageEconomicsSemantics("SCENARIO_ENVELOPE", False, False),
    )


def compute_reevaluation(e: WarehouseEvidence, thresholds: Thresholds, *, start_time: str, epoch_age: int) -> Optional[rc.ReevaluationWindow]:
    """Gates that currently hide the next step; None once both are clear (contract 2.0.0).

    Pure function of the epoch start, the analysis time, the settled billed days and the cost-history days. The contract
    validator embeds the same gate lengths, so a policy change to them must travel with a contract version bump.
    """
    if (thresholds.readiness_min_billed_days, thresholds.min_economics_cost_history_days) != (
        rc.REEVALUATION_READY_BILLED_DAYS, rc.REEVALUATION_COST_HISTORY_DAYS
    ):
        raise ValueError("Re-evaluation gate thresholds differ from the locked contract constants; bump the contract version")
    codes = []
    if e.cost_history_days < thresholds.min_economics_cost_history_days:
        codes.append("COST_HISTORY_INSUFFICIENT")
    if ready_billed_days(e) < thresholds.readiness_min_billed_days:
        codes.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
    if not codes:
        return None
    return rc.ReevaluationWindow(
        status=rc.ReevaluationStatus.WAITING_FOR_EVIDENCE,
        earliest_reevaluation_time=rc.expected_earliest_reevaluation_time(start_time, sorted(codes), _iso_z(e.analysis_time)),
        blocking_codes=sorted(codes),
        method=(
            f"Later of the active gates: the next settled day, 00:00Z of the analysis date + 1 day (readiness: "
            f"{rc.REEVALUATION_READY_BILLED_DAYS} settled billed days in the current epoch) and 00:00Z of epoch start date + "
            f"{rc.REEVALUATION_COST_HISTORY_DAYS} days (cost-history floor). Earliest only: billing ingestion lag, days "
            "without billing and query-sample gates can delay it."
        ),
    )


def derive_observed_change(e: WarehouseEvidence) -> Optional[rc.ObservedConfigChange]:
    """Config diff between the immediately preceding epoch and the current one (authoritative, unmeasured; contract 1.2.0)."""
    cur = e.current_epoch
    cur_start = parse_datetime(cur.epoch_start)
    prior = [x for x in e.epochs if not x.is_current and (parse_datetime(x.epoch_start) or cur_start) < cur_start]
    if not prior:
        return None
    prev = max(prior, key=lambda x: parse_datetime(x.epoch_start))
    changes = [
        rc.ConfigChange(rc.TunableKnob(knob), getattr(prev.config, knob), getattr(cur.config, knob))
        for knob in sorted(("warehouse_type", "warehouse_channel", "warehouse_size", "min_clusters", "max_clusters", "auto_stop_minutes"))
        if getattr(prev.config, knob) != getattr(cur.config, knob)
    ]
    if not changes:
        return None
    pc = prev.config
    previous_epoch_id = rc.make_epoch_id(
        workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, start_time=_iso_z(prev.epoch_start),
        warehouse_type=pc.warehouse_type, warehouse_size=pc.warehouse_size, min_clusters=pc.min_clusters,
        max_clusters=pc.max_clusters, auto_stop_minutes=pc.auto_stop_minutes, warehouse_channel=pc.warehouse_channel,
    )
    return rc.ObservedConfigChange(
        previous_epoch_id=previous_epoch_id, previous_epoch_start=_iso_z(prev.epoch_start), changed_at=_iso_z(cur.epoch_start),
        changes=sorted(changes, key=lambda c: c.knob.value), measurement_status=rc.MeasurementStatus.NOT_MEASURED,
    )


def _recommended_plan(
    scenarios: Sequence[rc.Scenario],
    reevaluation: Optional[rc.ReevaluationWindow] = None,
    candidates: Sequence[rc.CandidateRecommendation] = (),
) -> rc.RecommendedPlan:
    recommended = next((s for s in scenarios if s.status == rc.ScenarioStatus.RECOMMENDED), None)
    if recommended is None:
        return rc.RecommendedPlan(None, "NO_RECOMMENDED_SCENARIO", "REVIEW_EVIDENCE", True, "No scenario currently satisfies the deterministic selection policy.", reevaluation)
    first = recommended.execution_plan[0] if recommended.execution_plan else None
    ready = any(x.execution_state in (rc.ExecutionState.READY, rc.ExecutionState.READY_FOR_CANARY) for x in recommended.execution_plan)
    if reevaluation is not None and not ready:
        young = "CURRENT_CONFIG_EPOCH_TOO_NEW" in reevaluation.blocking_codes
        if young:
            reason = "CURRENT_EPOCH_IMMATURE"
            summary = (
                f"{recommended.scenario_name} has no step ready yet: the current configuration is not ready (fewer than "
                f"{rc.REEVALUATION_READY_BILLED_DAYS} settled billed days). "
                f"Earliest re-evaluation is {reevaluation.earliest_reevaluation_time} (gates: {', '.join(reevaluation.blocking_codes)})."
            )
        else:
            # Epoch is mature; only billing history is short. The earliest time can already be past: the missing days have
            # elapsed but are not yet ingested (billing typically lags ~12h), so the next run after ingestion clears it.
            reason = "BILLING_HISTORY_ACCRUING"
            summary = (
                f"{recommended.scenario_name} has no step ready yet: the current epoch is mature, but fewer than "
                f"{rc.REEVALUATION_COST_HISTORY_DAYS} days of billing history are available. Re-run once billing ingestion has caught up "
                f"(earliest {reevaluation.earliest_reevaluation_time}, gates: {', '.join(reevaluation.blocking_codes)})."
            )
        return rc.RecommendedPlan(
            scenario_id=recommended.scenario_id,
            selection_reason_code=reason,
            next_action="WAIT_FOR_REEVALUATION",
            requires_hitl=True,
            decision_summary=summary,
            reevaluation=reevaluation,
        )
    if first is None:
        next_action = "REVIEW_SCENARIO"
    elif first.step_type == rc.PlanStepType.COMPATIBILITY_VALIDATION:
        next_action = "VALIDATE_COMPATIBILITY"
    elif first.step_type == rc.PlanStepType.CANDIDATE and first.execution_state == rc.ExecutionState.READY_FOR_CANARY:
        next_action = f"RUN_{first.knob.value.upper()}_CANARY" if first.knob else "RUN_CANARY"
    else:
        next_action = "REVIEW_AND_EXECUTE_FIRST_READY_STEP"
    reason = "CURRENT_TYPE_OPTIMIZATION_HAS_VALIDATED_EVIDENCE" if recommended.scenario_type == rc.ScenarioType.IN_PLACE_OPTIMIZATION else "MIGRATION_SCENARIO_SELECTED_BY_POLICY"
    return rc.RecommendedPlan(
        scenario_id=recommended.scenario_id,
        selection_reason_code=reason,
        next_action=next_action,
        requires_hitl=True,
        decision_summary=f"{recommended.scenario_name} is the deterministic execution path; apply validation gates and regenerate after invalidating changes."
                         + _rollback_sequencing_note(recommended, candidates) + _canary_sequencing_note(recommended, candidates),
        reevaluation=reevaluation,
    )


def _rollback_knobs(scenario: rc.Scenario, candidates: Sequence[rc.CandidateRecommendation]) -> List[str]:
    """Knobs of the scenario's canary rollback steps, in plan order."""
    rollback_ids = {c.candidate_id for c in candidates if "CANARY_GUARDRAIL_BREACHED" in c.blockers}
    return [x.knob.value for x in scenario.execution_plan
            if x.step_type == rc.PlanStepType.CANDIDATE and x.knob is not None and x.candidate_id in rollback_ids]


def _rollback_sequencing_note(scenario: rc.Scenario, candidates: Sequence[rc.CandidateRecommendation]) -> str:
    """A canary rollback goes before every other step: a change applied first starts a new configuration, and the next
    run no longer sees the change being rolled back."""
    knobs = _rollback_knobs(scenario, candidates)
    if not knobs:
        return ""
    return (f" Apply the canary rollback ({', '.join(knobs)}) before any other step: a change applied first starts a new "
            "configuration, and the next run no longer sees the change being rolled back.")


def _canary_sequencing_note(scenario: rc.Scenario, candidates: Sequence[rc.CandidateRecommendation] = ()) -> str:
    """When approval-only steps and canaries are both ready: approval-only steps go first, canaries start once those
    have run for the stability period, so a canary's cost reading is not mixed with another change's saving. A canary
    rollback has its own sentence (it goes before everything), so it is not listed again here."""
    rollbacks = _rollback_knobs(scenario, candidates)
    steps = [x for x in scenario.execution_plan if x.step_type == rc.PlanStepType.CANDIDATE and x.knob is not None]
    hitl = [x.knob.value for x in steps if x.execution_state == rc.ExecutionState.READY and x.knob.value not in rollbacks]
    canary = [x.knob.value for x in steps if x.execution_state == rc.ExecutionState.READY_FOR_CANARY]
    if not canary or not (hitl or rollbacks):
        return ""
    if not hitl:
        return (f" Start the {', '.join(canary)} canary once the next run shows the restored configuration ready "
                f"({rc.REEVALUATION_READY_BILLED_DAYS} settled billed days), so the canary's cost reading is not mixed with the rollback.")
    lead = f" Then apply the ready no-canary steps ({', '.join(hitl)});" if rollbacks else f" Apply the ready no-canary steps ({', '.join(hitl)}) first;"
    return (f"{lead} start the {', '.join(canary)} canary once the next run shows "
            f"their new configuration ready ({rc.REEVALUATION_READY_BILLED_DAYS} settled billed days), so the canary's cost reading is not "
            "mixed with their savings.")


def _build_locked_recommendation_package(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    scenarios: Sequence[Scenario],
    thresholds: Thresholds,
) -> rc.RecommendationPackage:
    """Build one immutable Reporter JSON Contract package from reviewed decisions."""
    ep = e.current_epoch
    start_time = _iso_z(ep.epoch_start)
    contract_epoch_id = rc.make_epoch_id(
        workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, start_time=start_time,
        warehouse_type=ep.config.warehouse_type, warehouse_size=ep.config.warehouse_size,
        min_clusters=ep.config.min_clusters, max_clusters=ep.config.max_clusters,
        auto_stop_minutes=ep.config.auto_stop_minutes, warehouse_channel=ep.config.warehouse_channel,
    )
    evidence_registry, finding_map, compatibility_map, shared_cost_refs = _build_evidence_registry(
        e, candidates, scenarios, contract_epoch_id
    )
    candidate_contracts, candidates_by_internal_id = _candidate_contracts(
        e, candidates, contract_epoch_id, evidence_registry, finding_map, shared_cost_refs
    )
    scenario_contracts, _ = _scenario_contracts(
        e, scenarios, contract_epoch_id, candidates_by_internal_id, candidate_contracts, compatibility_map
    )
    reevaluation = compute_reevaluation(e, thresholds, start_time=start_time, epoch_age=epoch_age_days(ep, e.analysis_time))
    recommended = _recommended_plan(scenario_contracts, reevaluation, candidate_contracts)
    package_economics = _package_economics(e, scenario_contracts, recommended.scenario_id)
    evidence_fingerprint = rc.make_evidence_fingerprint(
        workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, epoch_id=contract_epoch_id,
        evidence_records=[rc.to_primitive(x) for x in evidence_registry.values()],
    )
    current_age = epoch_age_days(ep, e.analysis_time)
    actual = _actual_cost_breakdown(e)
    forward = _forward_cost_breakdown(e)
    forecast_assumptions = [
        "Current configuration epoch is the decision baseline.",
        ("Forward DBU cost averages whole run-to-run billing cycles of this periodic current-epoch workload."
         if (e.forecast_metadata or {}).get("forecast_model") == "CADENCE" else
         "Forward DBU cost uses the deterministic current-epoch run-rate model."),
        "AWS forward cost is modeled from observed current-epoch resource time and the configured regional price registry when available.",
        "DBU cost is priced at the account's list price; negotiated discounts lower real savings in proportion.",
        "AWS cost is priced at on-demand EC2 rates; Spot, Reserved Instances and Savings Plans are not modeled.",
    ]
    source_regions = sorted({x for x in e.source_region.split(",") if x}) or ["unknown"]
    provenance = rc.Provenance(
        contract_version=rc.CONTRACT_VERSION, policy_version=POLICY_VERSION, analyzer_version=ANALYZER_VERSION,
        optimizer_version=OPTIMIZER_VERSION, modeler_version=MODEL_VERSION, estimator_version=ESTIMATOR_VERSION,
        bundle_estimator_version=BUNDLE_MODEL_VERSION, reviewer_version=REVIEWER_VERSION,
        decision_graph_version=DECISION_VERSION, technique_catalog_version=TECHNIQUE_CATALOG_VERSION,
        evidence_fingerprint=evidence_fingerprint,
        source_tables=[
            "system.compute.warehouses", "system.compute.warehouse_events", "system.query.history",
            "system.billing.usage", "system.billing.list_prices",
        ],
        source_regions=source_regions, generated_at=_iso_z(e.analysis_time), package_hash_full_sha256=None,
    )
    package = rc.RecommendationPackage(
        recommendation_package_id="PKG-" + "0" * rc.HASH_HEX_LENGTH,
        contract_version=rc.CONTRACT_VERSION,
        warehouse=rc.WarehouseIdentity(
            workspace_id=e.workspace_id, warehouse_id=e.warehouse_id, warehouse_name=e.warehouse_name,
            owner=e.attribution.owner if e.attribution else None, region=e.source_region,
            current_epoch=rc.CurrentEpoch(contract_epoch_id, start_time, current_age, True),
        ),
        current_config=rc.CurrentConfig(
            warehouse_type=ep.config.warehouse_type, warehouse_size=ep.config.warehouse_size,
            min_clusters=ep.config.min_clusters, max_clusters=ep.config.max_clusters,
            auto_stop_minutes=ep.config.auto_stop_minutes, warehouse_channel=ep.config.warehouse_channel,
            photon=_photon_config_observation(e),
        ),
        workload_profile=_workload_profile(e, thresholds),
        cost_baseline=rc.CostBaseline(
            currency=e.currency, actual_365d=actual, forward_365d=forward,
            forecast=rc.ForecastInfo(MODEL_VERSION, _forecast_confidence(e, thresholds), forecast_assumptions),
        ),
        assessment=_assessment_contract(e, candidate_contracts, scenario_contracts, thresholds),
        evidence=evidence_registry, candidates=candidate_contracts, scenarios=scenario_contracts,
        recommended_plan=recommended, package_economics=package_economics,
        realization=rc.Realization(
            status=rc.RealizationStatus.NOT_APPLIED, realization_records=[],
            aggregate=rc.RealizationAggregate(0, 0, 0, e.currency, 0.0, 0.0),
        ),
        provenance=provenance,
        observed_change=derive_observed_change(e),
        carried_forward_plan=None,  # Stage 2 (carried-forward preview) is reserved and not produced yet
    )
    package = dataclasses.replace(package, recommendation_package_id=package.expected_package_id())
    package = dataclasses.replace(
        package, provenance=dataclasses.replace(package.provenance, package_hash_full_sha256=package.full_package_hash())
    )
    package.validate()
    return package


def optimize_warehouse(w: WarehouseAnalysis, thresholds: Thresholds) -> Tuple[List[CandidateRecommendation], List[Scenario], Optional[str]]:
    """Run the deterministic decision pipeline for one analyzed, supported warehouse."""
    e, analyses = w.evidence, w.analyses
    optimizer = Optimizer(thresholds)
    estimator = Estimator(thresholds)
    reviewer = Reviewer(thresholds)
    planner = ScenarioPlanner(thresholds)
    decisions = optimizer.optimize(e, analyses)
    candidates = estimator.candidates(e, analyses, decisions)
    reviewer.review(e, candidates)
    scenarios, recommended = planner.scenarios(e, candidates)
    reviewer.review_scenarios(e, candidates, scenarios, recommended)
    return candidates, scenarios, recommended


def evaluate_analysis(
    analysis: FleetAnalysis, thresholds: Thresholds, batch_size: int = 100, governance: Optional[GovernanceResult] = None
) -> List[WarehouseOptimization]:
    """Evaluate the eligible warehouses through both reviewer gates without constructing reporter objects."""
    evidence = analysis.evidence
    governance = governance if governance is not None else governor.govern(analysis)
    LOGGER.info("pipeline.start warehouses=%d batch_size=%d", len(evidence), batch_size)
    for excluded in governance.exclusions:
        LOGGER.warning(
            "warehouse.excluded workspace_id=%s warehouse_id=%s warehouse_type=%s reason=%s",
            excluded["workspace_id"], excluded["warehouse_id"], excluded["warehouse_type"], excluded["reason_code"],
        )
    ordered = governance.eligible
    results: List[WarehouseOptimization] = []
    batch = max(1, int(batch_size))
    for offset in range(0, len(ordered), batch):
        chunk = ordered[offset:offset + batch]
        LOGGER.info("pipeline.batch.start batch_index=%d start=%d size=%d", offset // batch + 1, offset, len(chunk))
        for w in chunk:
            e = w.evidence
            LOGGER.info(
                "warehouse.start workspace_id=%s warehouse_id=%s name=%s epoch_id=%s",
                e.workspace_id, e.warehouse_id, e.warehouse_name, e.current_epoch_id,
            )
            candidates, scenarios, recommended = optimize_warehouse(w, thresholds)
            status_counts: Dict[str, int] = {}
            for candidate in candidates:
                status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1
            LOGGER.info(
                "warehouse.decisions workspace_id=%s warehouse_id=%s statuses=%s scenarios=%d recommended_scenario=%s",
                e.workspace_id, e.warehouse_id, canonical_json(status_counts), len(scenarios), recommended or "NONE",
            )
            results.append(WarehouseOptimization(e.warehouse_id, e.workspace_id, e, candidates, scenarios, recommended))
        LOGGER.info("pipeline.batch.done batch_index=%d processed=%d", offset // batch + 1, len(chunk))
    return results


def evaluate_warehouses(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[WarehouseOptimization]:
    """Analyze and evaluate warehouses from evidence already in hand (the CLI runs analyzer.run first)."""
    return evaluate_analysis(FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds)), thresholds, batch_size)


def build_packages_from_analysis(
    analysis: FleetAnalysis, thresholds: Thresholds, batch_size: int = 100, governance: Optional[GovernanceResult] = None
) -> List[rc.RecommendationPackage]:
    """Run the deterministic decision pipeline on an analyzed fleet and directly assemble locked public packages."""
    evidence = analysis.evidence
    optimizations = evaluate_analysis(analysis, thresholds, batch_size, governance)
    evidence_by_key = {(e.workspace_id, e.warehouse_id): e for e in evidence}
    packages: List[rc.RecommendationPackage] = []
    for result in optimizations:
        e = evidence_by_key[(result.workspace_id, result.warehouse_id)]
        package = _build_locked_recommendation_package(e, result.candidates, result.scenarios, thresholds)
        packages.append(package)
        LOGGER.info(
            "contract.v1.package.valid workspace_id=%s warehouse_id=%s package_id=%s candidates=%d scenarios=%d",
            e.workspace_id, e.warehouse_id, package.recommendation_package_id, len(package.candidates), len(package.scenarios),
        )
    LOGGER.info(
        "pipeline.done warehouses=%d packages=%d candidates=%d scenarios=%d",
        len(evidence), len(packages), sum(len(p.candidates) for p in packages), sum(len(p.scenarios) for p in packages),
    )
    return packages


def build_recommendation_packages(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[rc.RecommendationPackage]:
    """Analyze evidence already in hand and assemble locked public packages (the CLI runs analyzer.run first)."""
    return build_packages_from_analysis(FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds)), thresholds, batch_size)


def validate_locked_package_schema(payload: Mapping[str, Any]) -> None:
    """Validate the serialized package against the locked Draft 2020-12 schema."""
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:
        raise RuntimeError(f"jsonschema>=4.23 is required to validate Reporter Contract v{rc.CONTRACT_VERSION} output") from exc
    schema = json.loads(_CONTRACT_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(dict(payload))


def _safe_artifact_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))
    return text or "unknown"


def package_locked_contract(
    output_dir: Path,
    packages: Sequence[rc.RecommendationPackage],
    evidence: Sequence[WarehouseEvidence],
    *,
    generate_workbook: bool = True,
    unseen_billed_warehouses: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Write locked JSON packages plus the Workbook Contract projection.

    Every packages/*.json file remains the source of truth. workbook.xlsx is a
    presentation-only projection and is never read back into the decision engine.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    packages_dir = output_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("contract.v1.package.start output_dir=%s packages=%d", output_dir, len(packages))
    entries = []
    result: Dict[str, str] = {}
    for package in packages:
        payload = package.to_dict()
        validate_locked_package_schema(payload)
        name = f"{_safe_artifact_name(package.warehouse.workspace_id)}__{_safe_artifact_name(package.warehouse.warehouse_id)}.json"
        path = packages_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        entries.append({
            "recommendation_package_id": package.recommendation_package_id,
            "workspace_id": package.warehouse.workspace_id,
            "warehouse_id": package.warehouse.warehouse_id,
            "warehouse_name": package.warehouse.warehouse_name,
            "epoch_id": package.warehouse.current_epoch.epoch_id,
            "package_hash_full_sha256": package.provenance.package_hash_full_sha256,
            "path": f"packages/{name}",
        })
        result[f"package:{package.warehouse.workspace_id}:{package.warehouse.warehouse_id}"] = str(path)

    evidence_path = output_dir / "evidence.json"
    save_evidence(evidence_path, evidence)
    from publisher.contracts import workbook_contract as wc
    workbook_path: Optional[Path] = None
    if generate_workbook:
        LOGGER.info("workbook.v1.start packages=%d", len(packages))
        try:
            from publisher.reporter.workbook_reporter import write_workbook
        except ImportError as exc:
            raise RuntimeError("Workbook generation requires openpyxl (pip install openpyxl). Use --no-workbook only when producing JSON packages without it.") from exc
        workbook_path = write_workbook(output_dir / "workbook.xlsx", packages, unsupported_warehouse_exclusions(evidence))
        result["workbook"] = str(workbook_path)
        LOGGER.info("workbook.v1.done path=%s", workbook_path)
    manifest_path = output_dir / "manifest.json"
    exclusions = unsupported_warehouse_exclusions(evidence)
    manifest = {
        "publisher_version": PUBLISHER_VERSION,
        "reporter_contract_version": rc.CONTRACT_VERSION,
        "generated_at": max((p.provenance.generated_at for p in packages), default=dt.datetime.now(dt.timezone.utc).isoformat()),
        "package_count": len(packages),
        "packages": entries,
        "excluded_warehouse_count": len(exclusions),
        "excluded_warehouses": exclusions,
        # Live runs only: billed warehouses with no configuration snapshot (None = not checked: demo or replay).
        "unseen_billed_warehouse_check": "NOT_CHECKED" if unseen_billed_warehouses is None else "CHECKED",
        "unseen_billed_warehouse_count": None if unseen_billed_warehouses is None else len(unseen_billed_warehouses),
        "unseen_billed_warehouses": [dict(x) for x in unseen_billed_warehouses or []],
        "canonical_json_semantics": f"Each packages/*.json file is exactly one Reporter JSON Contract v{rc.CONTRACT_VERSION} RecommendationPackage.",
        "workbook_contract_version": wc.WORKBOOK_CONTRACT_VERSION,
        "workbook_status": "GENERATED" if workbook_path is not None else "SKIPPED",
        "workbook_file": None if workbook_path is None else workbook_path.name,
        "evidence_file": evidence_path.name,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["manifest"] = str(manifest_path)
    result["evidence"] = str(evidence_path)
    LOGGER.info("contract.v1.package.done packages=%d", len(packages))
    return result

# ---------------------------------------------------------------------------
# Demo / evidence persistence / merge
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv:Optional[Sequence[str]]=None)->argparse.Namespace:
    from publisher.contracts import workbook_contract as wc
    p=argparse.ArgumentParser(description=f"Deterministic Databricks SQL Warehouse optimizer v{PUBLISHER_VERSION} (Reporter Contract v{rc.CONTRACT_VERSION} + Workbook Contract v{wc.WORKBOOK_CONTRACT_VERSION})")
    p.add_argument("--output-dir",default="./warehouse_optimizer_out")
    p.add_argument("--demo",action="store_true")
    p.add_argument("--from-evidence",action="append",default=[])
    p.add_argument("--profile",default=os.getenv("DATABRICKS_CONFIG_PROFILE","DEFAULT"))
    p.add_argument("--execution-kind",choices=("warehouse","cluster"),default=None,help="Collector SQL execution target kind")
    p.add_argument("--execution-id",default=None,help="SQL warehouse ID or all-purpose cluster ID")
    p.add_argument("--sql-warehouse-id",default=os.getenv("DATABRICKS_SQL_WAREHOUSE_ID"),help="Convenience alias for a SQL warehouse execution target")
    p.add_argument("--serverless-warehouse-id",default=os.getenv("DATABRICKS_SERVERLESS_WAREHOUSE_ID"),help="Convenience alias for a Serverless SQL warehouse execution target")
    p.add_argument("--all-purpose-cluster-id",default=os.getenv("DATABRICKS_ALL_PURPOSE_CLUSTER_ID"),help="Convenience alias for an all-purpose compute execution target")
    p.add_argument("--workspace-id",default=os.getenv("DATABRICKS_WORKSPACE_ID"),help="Workspace/org ID; optional for warehouse, used to construct all-purpose cluster HTTP path")
    p.add_argument("--config-file",default=os.getenv("DATABRICKS_CONFIG_FILE"))
    p.add_argument("--source-region",default=os.getenv("DATABRICKS_REGION","unknown"),help="AWS region of the workspace; Pro/Classic AWS cost uses its registry prices")
    p.add_argument("--allow-missing-aws-price",action="store_true",help="Live mode: publish even when --source-region is missing from the AWS price registry (Pro/Classic AWS cost unavailable)")
    p.add_argument("--billing-ingested-through",default=None,help="YYYY-MM-DD: only billing records ingested on or before this date (reproduces an earlier collection)")
    default_registry=str(REPO_ROOT / "pricing" / "aws_ec2_on_demand.json")
    p.add_argument("--aws-price-registry",default=os.getenv("AWS_PRICE_REGISTRY",default_registry))
    p.add_argument("--batch-size",type=int,default=int(os.getenv("WAREHOUSE_BATCH_SIZE","100")),help="Downstream analysis/report batch size; collector SQL is set-based and aggregated in Databricks")
    p.add_argument("--compatibility-api-mode",choices=("auto","off"),default=os.getenv("COMPATIBILITY_API_MODE","auto"),help="auto=B read-only workspace compatibility API when available, else A HITL fallback")
    p.add_argument("--log-level",choices=("DEBUG","INFO","WARNING","ERROR"),default=os.getenv("PUBLISHER_LOG_LEVEL","INFO"))
    p.add_argument("--demo-count",type=int,default=len(DEMO_FLEET),help=f"Synthetic collector warehouse count for --demo (1..{len(DEMO_FLEET)}, one per golden scenario)")
    p.add_argument("--no-workbook",action="store_true",help="Skip workbook.xlsx generation; locked JSON packages are still emitted")
    p.add_argument("--print-json",action="store_true")
    return p.parse_args(argv)


def main(argv:Optional[Sequence[str]]=None)->int:
    args=parse_args(argv); t=Thresholds(); out=Path(args.output_dir)
    log_path=setup_logging(out,args.log_level)
    LOGGER.info("policy.versions policy=%s analyzer=%s optimizer=%s estimator=%s bundle=%s",POLICY_VERSION,ANALYZER_VERSION,OPTIMIZER_VERSION,ESTIMATOR_VERSION,BUNDLE_MODEL_VERSION)
    context=RunContext(request=request_from_args(args),thresholds=t)
    analysis=analyzer.run(context)
    evidence=analysis.evidence; unseen=analysis.unseen_billed_warehouses
    governance=governor.run(context,analysis)
    packages=build_packages_from_analysis(analysis,t,args.batch_size,governance)
    files=package_locked_contract(out,packages,evidence,generate_workbook=not args.no_workbook,unseen_billed_warehouses=unseen)
    candidate_count=sum(len(pkg.candidates) for pkg in packages)
    scenario_count=sum(len(pkg.scenarios) for pkg in packages)
    excluded_count=len(governance.exclusions)
    LOGGER.info("publisher.done warehouses=%d excluded=%d packages=%d candidates=%d scenarios=%d log=%s",len(evidence)-excluded_count,excluded_count,len(packages),candidate_count,scenario_count,log_path)
    if args.print_json:
        if len(packages) != 1:
            raise SystemExit("--print-json requires exactly one warehouse package; use the packages/ directory for multi-warehouse runs.")
        print(json.dumps(packages[0].to_dict(),indent=2,sort_keys=True))
    else:
        print(f"Analyzed {len(evidence)-excluded_count} warehouse(s); emitted {len(packages)} locked Reporter Contract v{rc.CONTRACT_VERSION} package(s), {candidate_count} candidates, and {scenario_count} scenarios; excluded {excluded_count} unsupported warehouse(s) (see manifest).")
        for k,v in files.items(): print(f"{k}: {v}")
        print(f"log: {log_path}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
