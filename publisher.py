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

from contracts import reporter_contract as rc

PUBLISHER_VERSION = "2.10.0"
CONTRACT_VERSION = rc.CONTRACT_VERSION
POLICY_VERSION = "sql-warehouse-policy-v2.10.0"
ANALYZER_VERSION = "sql-warehouse-analyzer-v2.10.0"
OPTIMIZER_VERSION = "sql-warehouse-optimizer-v2.10.0"
MODEL_VERSION = "epoch-runrate-v2.10.0"
ESTIMATOR_VERSION = "counterfactual-v2.10.0"
BUNDLE_MODEL_VERSION = "stateful-resource-bundle-v2.10.0"
REVIEWER_VERSION = "recommendation-reviewer-v2.10.0"
DECISION_VERSION = "warehouse-decision-graph-v2.10.0"
TECHNIQUE_CATALOG_VERSION = "technique-catalog-v2"
GIB = 1024 ** 3
LOGGER = logging.getLogger("publisher")

SIZE_ORDER = [
    "2X_SMALL", "X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE",
    "2X_LARGE", "3X_LARGE", "4X_LARGE", "5X_LARGE",
]

# Published SQL Serverless DBU/hour schedule by nominal warehouse size. The
# production cost model uses account pricing from system.billing.list_prices;
# this table is only the documented DBU consumption rate per cluster-hour.
SQL_DBU_PER_CLUSTER_HOUR = {
    "2X_SMALL": 4.0, "X_SMALL": 6.0, "SMALL": 12.0, "MEDIUM": 24.0,
    "LARGE": 40.0, "X_LARGE": 80.0, "2X_LARGE": 144.0,
    "3X_LARGE": 272.0, "4X_LARGE": 528.0,
}

# Databricks-documented AWS topology for Pro/Classic SQL warehouses. All workers
# use i3.2xlarge; the driver type and worker count vary by warehouse size.
AWS_SQL_WAREHOUSE_TOPOLOGY = {
    "2X_SMALL": {"driver": "i3.2xlarge", "workers": 1},
    "X_SMALL": {"driver": "i3.2xlarge", "workers": 2},
    "SMALL": {"driver": "i3.4xlarge", "workers": 4},
    "MEDIUM": {"driver": "i3.8xlarge", "workers": 8},
    "LARGE": {"driver": "i3.8xlarge", "workers": 16},
    "X_LARGE": {"driver": "i3.16xlarge", "workers": 32},
    "2X_LARGE": {"driver": "i3.16xlarge", "workers": 64},
    "3X_LARGE": {"driver": "i3.16xlarge", "workers": 128},
    "4X_LARGE": {"driver": "i3.16xlarge", "workers": 256},
    "5X_LARGE": {"driver": "i3.16xlarge", "workers": 512},
}

SUPPORTED_WAREHOUSE_TYPES = frozenset({"CLASSIC", "PRO", "SERVERLESS"})

ACTIVE_KNOBS = (
    "warehouse_size",
    "max_clusters",
    "min_clusters",
    "auto_stop_minutes",
    "warehouse_type",
)

@dataclass(frozen=True)
class Thresholds:
    """Central deterministic policy, evidence, confidence, and estimator thresholds."""
    # Evidence sufficiency / epoch stability
    min_query_sample_size: int = 100
    strong_query_sample_size: int = 500
    min_cost_history_days: int = 14
    # Hard floor: below this many current-epoch billing days no forecast-dependent estimate is published and a
    # cost-judged migration is INSUFFICIENT_EVIDENCE. It equals the config-epoch maturity gate, so it adds no meaningful
    # wait between plan steps (cost history restarts with each applied change). It can trail epoch age by up to a day
    # when the billing watermark has not yet covered the latest day.
    min_economics_cost_history_days: int = 7
    forecast_cadence_gap_days: int = 7  # billing with a longer gap (and median cycle) is a periodic workload
    forecast_min_cycles: int = 2  # whole run-to-run cycles the cadence forecast needs
    current_config_stability_days: int = 7

    # Photon inference from signed DBU billing evidence in the current warehouse epoch.
    photon_min_feature_coverage_pct: float = 90.0
    photon_enabled_usage_pct: float = 95.0
    photon_disabled_usage_pct: float = 5.0
    photon_min_epoch_dbu: float = 1.0
    photon_freshness_days: int = 14

    # Queue / concurrency
    queue_query_pct_increase_max: float = 5.0
    queue_wait_p95_ms_increase_max: int = 30_000
    queue_query_pct_safe: float = 0.5
    queue_wait_p95_ms_safe: int = 5_000

    # Size / spill / latency
    spill_query_pct_upsize: float = 2.0
    spill_p95_bytes_upsize: int = GIB
    # Severity percentiles are taken among affected queries; rules that add capacity/cost need a p95
    # backed by at least this many affected queries (below ~20 samples a p95 is just the maximum).
    severity_min_affected_queries: int = 20
    spill_query_pct_downsize_safe: float = 0.1
    # A few statements carrying most spilled bytes are a rewrite, not an upsize for every query.
    spill_concentration_top_sources: int = 3
    spill_concentration_hold_pct: float = 80.0
    # Failed/cancelled share of queries; above it, low spill/queue/latency is not trustworthy evidence of headroom.
    failure_cancel_pct_downsize_safe: float = 5.0
    p95_execution_ms_downsize_safe: int = 5_000
    p99_execution_ms_downsize_safe: int = 30_000
    # Batch profile: a warehouse whose recent queries come mostly from jobs is judged on run days, not on interactive
    # latency (a 10-minute ETL query is normal), for a downsize canary only.
    batch_job_query_share_pct: float = 80.0
    batch_min_active_days: int = 20

    # Min / max clusters
    time_at_min_pct_reduce: float = 25.0
    time_at_max_pct_block_min_reduce: float = 20.0
    time_at_max_pct_safe_reduce: float = 1.0
    # Saturation = real time at the configured ceiling. A peak above max is not evidence (cluster recycling can briefly exceed it).
    time_at_max_pct_saturated: float = 5.0
    # A reduced ceiling must leave growth room above the observed peak: max(clusters, ceil(pct x peak)).
    max_cluster_headroom_clusters: int = 1
    max_cluster_headroom_pct: float = 25.0
    # Concurrency-derived cluster targets (Pro/Classic add one cluster per this many concurrent queries, documented).
    queries_per_cluster_pro_classic: int = 10
    max_cluster_target_percentile: float = 99.5  # of busy minutes' running + queued queries (recent window and whole epoch)
    max_cluster_target_headroom_clusters: int = 1
    min_cluster_busy_start_percentile: float = 95.0  # of busy periods' peak demand in their first scale-up-lag minutes
    scale_up_lag_minutes_pro_classic: int = 5
    scale_up_lag_minutes_serverless: int = 1
    concurrency_min_busy_minutes: int = 200  # a P99.5 needs at least this many busy minutes
    # Long windows (the whole current epoch, older epochs) measure peak demand as the level reached in at least this
    # many busy minutes: a percentile over months dilutes a two-hour quarter-end peak below its tail.
    peak_demand_min_minutes: int = 30
    min_cluster_min_busy_periods: int = 20  # a P95 of busy-period starts needs at least this many periods
    max_clusters_supported_limit: int = 40
    min_clusters_supported_limit: int = 30

    # Auto-stop
    min_cluster_minutes_for_cost_model: float = 1.0
    # Auto-stop values evaluated: every step from the type's floor up to the maximum (minutes). Pro/Classic never go
    # below 10; Serverless values below 5 set through the API are left alone.
    auto_stop_floor_pro_classic: int = 10
    auto_stop_floor_serverless: int = 5
    auto_stop_step_minutes: int = 5
    auto_stop_max_minutes: int = 1440
    # A value covers an idle gap (keeps the warehouse up until the next query) only when it is at least this much longer
    # than the gap, so a small schedule drift does not turn every gap into a restart.
    auto_stop_bridge_margin_pct: float = 10.0
    # A gap counts as running throughout (a lower auto-stop adds a restart there) when the warehouse ran at least this
    # fraction of it; the slack absorbs event-time rounding.
    running_idle_full_window_fraction: float = 0.99
    # A gap counts as kept alive (by sessions, result fetching or keep-alive, not queries) when the warehouse ran this
    # many minutes past the current auto-stop; below it, the stop event merely trailed the auto-stop.
    kept_alive_gap_tolerance_minutes: float = 2.0
    # A gap the warehouse stopped in before the next query is kept alive only when it ran longer than this past the
    # auto-stop (a session held it open); shorter overruns are a late stop event.
    kept_alive_stopped_gap_minutes: float = 10.0
    # Cost of one restart in cluster-minutes per cluster started, from the documented start times: Pro/Classic about 4
    # minutes, Serverless 2-6 seconds (upper end). Whether starting time is billed is not documented, so this is a
    # priced assumption.
    restart_cost_cluster_minutes_pro_classic: float = 4.0
    restart_cost_cluster_minutes_serverless: float = 0.1
    # Cold-start budget: restarts a lower auto-stop adds, as a share of queries over the same window (Pro/Classic only;
    # nonprod warehouses by tag get the larger budget).
    cold_start_added_share_max_pct: float = 2.0
    cold_start_added_share_max_pct_nonprod: float = 10.0
    # Among auto-stop values whose net saving is within this share of the best, the smallest change is chosen.
    auto_stop_near_best_pct: float = 2.0
    # Nonprod profile: a warehouse tag whose key (case-insensitive) is one of these keys and whose value is one of these
    # values. Staging and UAT stay production.
    nonprod_tag_keys: Tuple[str, ...] = ("env", "environment", "stage", "tier")
    nonprod_tag_values: Tuple[str, ...] = ("dev", "development", "test", "testing", "qa", "sandbox", "nonprod", "non-prod")
    # Non-work activity flags (observations; nothing is priced from them): running time after the current auto-stop
    # with no query running at or above this share of running time; and a warehouse running at least this share of
    # the clock while queries are busy at most this share of its running time, with no auto-stop value removing it.
    kept_alive_flag_pct: float = 10.0
    always_on_running_pct: float = 90.0
    always_on_busy_pct_max: float = 10.0

    # Forecast
    forecast_recent_days: int = 28
    forecast_long_days: int = 84
    billing_settle_hours: int = 24  # a UTC day's billing counts only this long after the day ends
    pressure_window_days: int = 30  # recent window for capacity facts (time at min/max, observed peak)
    forecast_cv_min_days: int = 7
    forecast_weight_28_long: float = 0.65
    forecast_weight_28_mid: float = 0.75
    trend_adjustment_weight: float = 0.25
    trend_ratio_floor: float = 0.75
    trend_ratio_ceiling: float = 1.25

    # Scenario uncertainty / realization
    realization_conservative: float = 0.50
    realization_base: float = 0.75
    realization_optimistic: float = 1.00
    min_cluster_savings_fraction_cap: float = 0.50
    # Time at the min-cluster floor shows the autoscaler sat at the floor, not that a floor cluster was
    # spare. The time-at-min figure is therefore an upper bound on removable capacity (optimistic = 100%),
    # the floor capacity may have been needed (conservative = 0%), and base is the midpoint (audit H4).
    min_cluster_realization_optimistic: float = 1.00
    min_cluster_realization_base: float = 0.50
    min_cluster_realization_conservative: float = 0.00
    max_cluster_base_uplift_weight: float = 0.50
    downsize_runtime_multiplier_conservative: float = 2.0  # fully linear slowdown: one size down halves the workers
    downsize_runtime_multiplier_base: float = 1.30
    downsize_runtime_multiplier_optimistic: float = 1.10
    upsize_runtime_multiplier_conservative: float = 1.00
    upsize_runtime_multiplier_base: float = 0.70
    upsize_runtime_multiplier_optimistic: float = 0.50
    # Pro/Classic DBU per cluster-hour by size is not documented first-party: a workspace's billed rates price a size
    # change only when they rise with size and the pair ratio is within this band of the published size ratio.
    size_rate_published_band: float = 0.25
    # A change made to save money ships only when its own base modeled savings are positive and at least this much a
    # year (warehouse currency).
    cost_change_min_annual_savings: float = 500.0
    # Migration is priced relative to billing: the same cost model applied to the current warehouse must land within
    # this band of its billed DBU forecast, or the migration estimate is withheld.
    migration_reconciliation_min: float = 0.70
    migration_reconciliation_max: float = 1.30
    cost_factor_floor: float = 0.05
    cost_factor_ceiling: float = 3.00
    confidence_coverage_days: float = 90.0
    confidence_epoch_maturity_days: float = 30.0
    confidence_base_weight: float = 0.10
    confidence_coverage_weight: float = 0.25
    confidence_sample_weight: float = 0.25
    confidence_stability_weight: float = 0.15
    confidence_epoch_weight: float = 0.15
    confidence_signal_weight: float = 0.10
    confidence_high_threshold: float = 0.80
    confidence_medium_threshold: float = 0.60
    confidence_signal_change: float = 1.00
    confidence_signal_no_change: float = 0.70
    confidence_cap_test_required: float = 0.78
    confidence_cap_actionable: float = 0.93
    confidence_cap_default: float = 0.79  # every status but ACTIONABLE/TEST_REQUIRED: a decision that changes nothing is never HIGH
    confidence_cap_insufficient_evidence: float = 0.59  # below the MEDIUM cut-off: a candidate without enough evidence is LOW
    # Full credit of the sample component for the knobs that do not decide on a query count (5x their evidence minimum).
    confidence_strong_busy_minutes: int = 1000
    confidence_strong_busy_periods: int = 100
    # Serverless scales differently from the documented Pro/Classic rule, so its queue-based ceiling increase is shown
    # at low confidence (below confidence_medium_threshold).
    confidence_cap_serverless_ceiling_increase: float = 0.59
    remeasure_days_after_invalidating_change: int = 7
    growth_hold_weekly_ratio: float = 1.10  # capacity cuts wait while every one of the last 4 weeks has >= this x the prior 4-week median of queries
    plan_risk_hitl_change: float = 1.0  # plan order divides base savings x confidence by this for an ACTIONABLE step
    plan_risk_canary_change: float = 2.0  # ... and by this for a TEST_REQUIRED step (a canary costs a measurement cycle)


@dataclass(frozen=True)
class WarehouseConfig:
    """Canonical SQL warehouse configuration snapshot used to form config epochs."""
    warehouse_type: str
    warehouse_channel: str
    warehouse_size: str
    min_clusters: int
    max_clusters: int
    auto_stop_minutes: int

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PhotonObservation:
    """Billing-derived Photon observation for the current warehouse epoch.

    This is an observed workload state, not a direct warehouse configuration read.
    It is derived from signed DBU usage attributed to product_features.is_photon.
    """
    status: str  # ENABLED | DISABLED | MIXED | UNKNOWN
    enabled_usage_pct: Optional[float]
    known_feature_coverage_pct: float
    true_dbu: float
    false_dbu: float
    null_dbu: float
    latest_usage_time: Optional[str]
    freshness_days: Optional[int]
    confidence: Dict[str, Any]
    evidence_source: str = "system.billing.usage.product_features.is_photon"


@dataclass(frozen=True)
class QuerySourceProfile:
    """Multi-label query-source profile for a warehouse's current epoch."""
    categories: Dict[str, int]
    percentages: Dict[str, float]
    primary_category: str
    client_applications: List[str]
    multi_label: bool = True


@dataclass(frozen=True)
class WarehouseAttribution:
    """Human/business attribution inferred from billing and query history."""
    owner: Optional[str]
    observed_owners: List[str]
    owner_source: str
    query_source: QuerySourceProfile


@dataclass
class EpochEvidence:
    """Normalized evidence attached to one immutable warehouse configuration epoch."""
    epoch_id: str
    warehouse_id: str
    workspace_id: str
    account_id: str
    warehouse_name: str
    epoch_start: str
    epoch_end: Optional[str]
    is_current: bool
    config: WarehouseConfig
    tags: Dict[str, str] = field(default_factory=dict)
    query: Dict[str, Any] = field(default_factory=dict)
    events: Dict[str, Any] = field(default_factory=dict)
    gaps: Dict[str, Any] = field(default_factory=dict)
    cost: Dict[str, Any] = field(default_factory=dict)
    # Daily resource series (current epoch only): None = not collected (older evidence), [] = collected, no activity.
    resource_daily: Optional[List[Dict[str, Any]]] = None


@dataclass
class WarehouseEvidence:
    """Complete normalized evidence contract for one workspace/warehouse identity."""
    warehouse_id: str
    workspace_id: str
    account_id: str
    warehouse_name: str
    source_region: str
    analysis_time: str
    current_epoch_id: str
    epochs: List[EpochEvidence]
    current_cost_365: Optional[float]  # None = unknown (unpriced usage in the trailing window); never 0.0
    baseline_forecast_cost_365: Optional[float]  # None = unknown forward DBU cost; never 0.0
    currency: str
    cost_history_days: int
    cost_cv_84: Optional[float]  # None = unknown (history too short or no cost basis); never 0.0
    forecast_metadata: Dict[str, Any] = field(default_factory=dict)
    daily_cost_365: List[Dict[str, Any]] = field(default_factory=list)
    billing_features: Dict[str, Any] = field(default_factory=dict)
    aws_baseline_cost_365: Optional[float] = None
    aws_cost_model: Dict[str, Any] = field(default_factory=dict)
    sql_sku_prices: Dict[str, Any] = field(default_factory=dict)
    compatibility_evidence: Dict[str, Any] = field(default_factory=dict)
    photon_observation: Optional[PhotonObservation] = None
    attribution: Optional[WarehouseAttribution] = None

    @property
    def dbu_365d_baseline_cost(self) -> Optional[float]:
        return self.baseline_forecast_cost_365

    @property
    def total_365d_baseline_cost(self) -> Optional[float]:
        if self.aws_baseline_cost_365 is None or self.baseline_forecast_cost_365 is None:
            return None
        return round(self.baseline_forecast_cost_365 + self.aws_baseline_cost_365, 2)

    @property
    def current_epoch(self) -> EpochEvidence:
        for e in self.epochs:
            if e.epoch_id == self.current_epoch_id:
                return e
        raise ValueError(f"Current epoch missing for {self.warehouse_id}: {self.current_epoch_id}")


@dataclass(frozen=True)
class Finding:
    """Atomic explainability record tying a fact to its source, threshold, and epoch."""
    finding_id: str
    metric: str
    value: Any
    unit: str
    threshold: Any
    comparator: str
    source_table: str
    source_field: str
    epoch_id: str
    window: str
    interpretation: str


@dataclass
class AnalysisResult:
    """Analyzer output for one knob: facts and findings only, never a recommendation."""
    warehouse_id: str
    knob: str
    current_epoch_id: str
    findings: List[Finding]
    facts: Dict[str, Any]


@dataclass
class OptimizationDecision:
    """Optimizer decision contract before any cost counterfactual is attached."""
    warehouse_id: str
    knob: str
    objective: str
    status: str
    current_config: Dict[str, Any]
    recommended_config: Dict[str, Any]
    blockers: List[str]
    dependencies: List[str]
    invalidates: List[str]
    reanalysis_required: List[str]
    rationale: str


@dataclass(frozen=True)
class Range3:
    """Deterministic low/base/high range used for cost, savings, and scenario estimates."""
    low: float
    base: float
    high: float

    def rounded(self) -> "Range3":
        return Range3(round(self.low, 2), round(self.base, 2), round(self.high, 2))


@dataclass
class CandidateRecommendation:
    """Reviewed standalone per-knob counterfactual with evidence, confidence, and economics."""
    recommendation_id: str
    warehouse_id: str
    workspace_id: str
    warehouse_name: str
    knob: str
    objective: str
    status: str
    current_config: Dict[str, Any]
    recommended_config: Dict[str, Any]
    current_cost_365: Optional[float]
    baseline_forecast_cost_365: Optional[float]
    estimated_cost_365: Optional[float]
    estimated_cost_range_365: Optional[Range3]
    estimated_savings_365: Optional[float]
    estimated_savings_range_365: Optional[Range3]
    confidence: Dict[str, Any]
    findings: List[Finding]
    blockers: List[str]
    dependencies: List[str]
    invalidates: List[str]
    reanalysis_required: List[str]
    metadata: Dict[str, Any]
    dbu_365d_baseline_cost: Optional[float] = None
    aws_365d_baseline_cost: Optional[float] = None
    total_365d_baseline_cost: Optional[float] = None
    dbu_365d_savings_estimate: Optional[float] = None
    aws_365d_savings_estimate: Optional[float] = None
    total_365d_savings_estimate: Optional[float] = None
    dbu_365d_savings_range: Optional[Range3] = None
    aws_365d_savings_range: Optional[Range3] = None
    total_365d_savings_range: Optional[Range3] = None
    dbu_365d_estimated_cost_range: Optional[Range3] = None
    aws_365d_estimated_cost_range: Optional[Range3] = None
    total_365d_estimated_cost_range: Optional[Range3] = None


@dataclass
class PlanStep:
    """One ordered scenario step referencing a canonical candidate and validation gate."""
    step: int
    recommendation_id: str
    knob: str
    current_config: Dict[str, Any]
    recommended_config: Dict[str, Any]
    status: str
    validation_gate: str
    remeasure_days: int
    invalidates: List[str]
    dependencies: List[str]
    execution_state: str = "READY"
    instruction: str = ""
    running_estimated_cost_range_365: Optional[Range3] = None
    running_estimated_savings_range_365: Optional[Range3] = None
    running_dbu_cost_range_365: Optional[Range3] = None
    running_aws_cost_range_365: Optional[Range3] = None
    running_dbu_savings_range_365: Optional[Range3] = None
    running_aws_savings_range_365: Optional[Range3] = None
    step_dbu_savings_range_365: Optional[Range3] = None
    step_aws_savings_range_365: Optional[Range3] = None
    confidence: Dict[str, Any] = field(default_factory=dict)
    pricing_basis: str = "AUTHORITATIVE"


@dataclass
class PortfolioEstimate:
    """Stateful non-overlapping forward DBU/AWS/total scenario economics."""
    estimate_status: str
    baseline_cost_365: float
    estimated_cost_range_365: Optional[Range3]
    estimated_savings_range_365: Optional[Range3]
    method: str
    non_additive: bool = True
    dbu_365d_baseline_cost: Optional[float] = None
    aws_365d_baseline_cost: Optional[float] = None
    total_365d_baseline_cost: Optional[float] = None
    dbu_365d_estimated_cost_range: Optional[Range3] = None
    aws_365d_estimated_cost_range: Optional[Range3] = None
    total_365d_estimated_cost_range: Optional[Range3] = None
    dbu_365d_savings_range: Optional[Range3] = None
    aws_365d_savings_range: Optional[Range3] = None
    total_365d_savings_range: Optional[Range3] = None
    # Stateful upside of steps marked PENDING_REEVALUATION; None when nothing is pending or it cannot be priced.
    contingent: Optional["PortfolioEstimate"] = None


@dataclass
class Scenario:
    """Mutually exclusive execution-plan branch with ordered steps and portfolio estimate."""
    scenario_id: str
    warehouse_id: str
    workspace_id: str
    name: str
    description: str
    steps: List[PlanStep]
    portfolio_estimate: PortfolioEstimate
    blockers: List[str]
    recommended: bool = False
    compatibility: Dict[str, Any] = field(default_factory=dict)
    confidence: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WarehouseOptimization:
    """Reviewed decision-layer result for one warehouse; not a serialization contract."""
    warehouse_id: str
    workspace_id: str
    evidence: WarehouseEvidence
    candidates: List[CandidateRecommendation]
    scenarios: List[Scenario]
    recommended_scenario_id: Optional[str]

    @property
    def recommended_scenario(self) -> Optional[Scenario]:
        return next((s for s in self.scenarios if s.scenario_id == self.recommended_scenario_id), None)

    @property
    def recommended_execution_plan(self) -> List[PlanStep]:
        scenario = self.recommended_scenario
        return [] if scenario is None else scenario.steps

    @property
    def query_source_profile(self) -> Dict[str, Any]:
        attribution = self.evidence.attribution
        profile = attribution.query_source if attribution else derive_query_source_profile(self.evidence.current_epoch.query)
        return as_serializable(profile)

    @property
    def aws_365d_baseline_cost(self) -> Optional[float]:
        return self.evidence.aws_baseline_cost_365

    @property
    def total_365d_baseline_cost(self) -> Optional[float]:
        return self.evidence.total_365d_baseline_cost



# ---------------------------------------------------------------------------
# System-table SQL
# ---------------------------------------------------------------------------

# Configuration epochs are islands of the decision fields. system.compute.warehouses writes a snapshot whenever any
# property changes, including the name and tags; those cosmetic snapshots are folded into the surrounding period so they
# never reset the epoch-maturity gate, the cost history or the forecast basis. A period starts at the first snapshot
# whose decision fields differ from the previous snapshot; its name and tags come from its latest snapshot. Portable SQL
# (window functions, IS DISTINCT FROM) shared by every collector query that groups activity by configuration epoch.
# The table has no unique snapshot key, so snapshots sharing a change_time are first collapsed to one deterministic row
# (a deleted snapshot wins, so the warehouse fails closed as deleted); every later window then orders by a unique key.
CONFIG_EPOCHS_CTE = """config_snapshot_ties AS (
  SELECT
    account_id, workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_channel, warehouse_size,
    min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
    ROW_NUMBER() OVER (
      PARTITION BY workspace_id, warehouse_id, change_time
      ORDER BY CASE WHEN delete_time IS NULL THEN 1 ELSE 0 END, warehouse_type, warehouse_channel, warehouse_size,
        min_clusters, max_clusters, auto_stop_minutes, warehouse_name
    ) AS rn_tie
  FROM system.compute.warehouses
), config_snapshots AS (
  SELECT
    account_id, workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_channel, warehouse_size,
    min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
    CASE WHEN LAG(change_time) OVER w IS NULL
      OR LAG(warehouse_type) OVER w IS DISTINCT FROM warehouse_type
      OR LAG(warehouse_channel) OVER w IS DISTINCT FROM warehouse_channel
      OR LAG(warehouse_size) OVER w IS DISTINCT FROM warehouse_size
      OR LAG(min_clusters) OVER w IS DISTINCT FROM min_clusters
      OR LAG(max_clusters) OVER w IS DISTINCT FROM max_clusters
      OR LAG(auto_stop_minutes) OVER w IS DISTINCT FROM auto_stop_minutes
    THEN 1 ELSE 0 END AS starts_epoch,
    ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn_desc
  FROM config_snapshot_ties
  WHERE rn_tie = 1
  WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY change_time)
), config_islands AS (
  SELECT *,
    SUM(starts_epoch) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS epoch_seq
  FROM config_snapshots
), config_island_rows AS (
  SELECT *,
    MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id, epoch_seq) AS epoch_start,
    ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, epoch_seq ORDER BY change_time DESC) AS rn_in_epoch
  FROM config_islands
), config_epochs AS (
  SELECT
    account_id, workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_channel, warehouse_size,
    min_clusters, max_clusters, auto_stop_minutes, tags, epoch_start,
    LEAD(epoch_start) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY epoch_start) AS epoch_end
  FROM config_island_rows
  WHERE rn_in_epoch = 1
)"""

CONFIG_EPOCHS_SQL = r"""
WITH __CONFIG_EPOCHS__,
active AS (
  SELECT workspace_id, warehouse_id
  FROM config_snapshots
  WHERE rn_desc = 1 AND delete_time IS NULL
)
SELECT
  h.account_id,
  h.workspace_id,
  h.warehouse_id,
  h.warehouse_name,
  h.warehouse_type,
  h.warehouse_channel,
  h.warehouse_size,
  h.min_clusters,
  h.max_clusters,
  h.auto_stop_minutes,
  h.tags,
  h.epoch_start,
  h.epoch_end,
  CASE WHEN h.epoch_end IS NULL THEN TRUE ELSE FALSE END AS is_current
FROM config_epochs h
JOIN active a ON h.workspace_id = a.workspace_id AND h.warehouse_id = a.warehouse_id
WHERE h.epoch_start >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
   OR h.epoch_end IS NULL
   OR h.epoch_end >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
ORDER BY h.workspace_id, h.warehouse_id, h.epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

EPOCH_QUERY_METRICS_SQL = r"""
WITH __CONFIG_EPOCHS__,
h AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),
q AS (
  SELECT
    x.workspace_id,
    x.compute.warehouse_id AS warehouse_id,
    h.epoch_start,
    h.epoch_end,
    x.start_time,
    x.total_duration_ms,
    x.total_task_duration_ms,
    x.execution_duration_ms,
    x.waiting_for_compute_duration_ms,
    x.waiting_at_capacity_duration_ms,
    x.spilled_local_bytes,
    x.read_bytes,
    x.read_io_cache_percent,
    x.execution_status,
    x.from_result_cache,
    x.compilation_duration_ms,
    x.result_fetch_duration_ms,
    -- Decision population for latency and spill: queries not served from the result cache (cache hits take about
    -- 0 ms and would loosen the latency gates). Failed and cancelled queries stay in: a statement timeout is latency.
    COALESCE(x.from_result_cache, FALSE) = FALSE AS in_decision_population,
    x.query_tags,
    x.query_source,
    x.client_application,
    x.statement_text
  FROM system.query.history x
  JOIN h ON x.workspace_id = h.workspace_id
        AND x.compute.warehouse_id = h.warehouse_id
        AND x.start_time >= h.epoch_start
        AND (h.epoch_end IS NULL OR x.start_time < h.epoch_end)
  WHERE x.compute.type = 'WAREHOUSE'
    AND x.compute.warehouse_id IS NOT NULL
    AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
)
SELECT
  workspace_id,
  warehouse_id,
  epoch_start,
  COUNT(*) AS epoch_queries,
  COUNT(DISTINCT DATE(start_time)) AS epoch_active_days,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS) AS queries_30,
  COUNT(DISTINCT CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN DATE(start_time) END) AS active_days_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN total_duration_ms END, 0.95) AS p95_total_ms_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_for_compute_duration_ms,0) > 0) AS compute_wait_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND waiting_for_compute_duration_ms > 0 THEN waiting_for_compute_duration_ms END, 0.95) AS p95_wait_compute_ms_when_waited_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_at_capacity_duration_ms,0) > 0) AS queued_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND waiting_at_capacity_duration_ms > 0 THEN waiting_at_capacity_duration_ms END, 0.95) AS p95_wait_capacity_ms_when_queued_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population) AS decision_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN execution_duration_ms END, 0.95) AS p95_execution_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN execution_duration_ms END, 0.99) AS p99_execution_ms_30,
  MAX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN execution_duration_ms END) AS max_execution_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN compilation_duration_ms END, 0.95) AS p95_compilation_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population THEN result_fetch_duration_ms END, 0.95) AS p95_result_fetch_ms_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_at_capacity_duration_ms,0) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS queued_query_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population AND COALESCE(spilled_local_bytes,0) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population),0) AS spill_query_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population AND COALESCE(spilled_local_bytes,0) > 0) AS spill_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND in_decision_population AND spilled_local_bytes > 0 THEN spilled_local_bytes END,0.95) AS p95_spill_bytes_when_spilled_30,
  SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN COALESCE(spilled_local_bytes,0) ELSE 0 END)/POW(1024,3) AS total_spill_gib_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN COALESCE(read_bytes,0) END,0.95) AS p95_read_bytes_30,
  -- I/O cache share of bytes read (weighted by read_bytes), not a per-query average that small reads would dominate.
  SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND read_io_cache_percent IS NOT NULL THEN read_io_cache_percent * COALESCE(read_bytes,0) END)
    / NULLIF(SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND read_io_cache_percent IS NOT NULL THEN COALESCE(read_bytes,0) END),0) AS avg_read_io_cache_percent_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND execution_status IN ('FAILED','CANCELED'))
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS failure_cancel_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND from_result_cache = TRUE)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS result_cache_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND SIZE(query_tags) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS tagged_query_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND (query_source.dashboard_id IS NOT NULL OR query_source.legacy_dashboard_id IS NOT NULL)) AS dashboard_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.job_info.job_id IS NOT NULL) AS job_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.notebook_id IS NOT NULL) AS notebook_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.genie_space_id IS NOT NULL) AS genie_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.alert_id IS NOT NULL) AS alert_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND query_source.sql_query_id IS NOT NULL) AS sql_query_source_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%power bi%') AS power_bi_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%tableau%') AS tableau_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%looker%') AS looker_queries_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(client_application,'')) LIKE '%databricks sql editor%') AS databricks_sql_editor_client_queries_30,
  ARRAY_SORT(COLLECT_SET(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND client_application IS NOT NULL THEN client_application END)) AS client_applications_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND statement_text IS NOT NULL AND TRIM(statement_text) <> '' AND statement_text <> '<REDACTED>')
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS statement_text_visible_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(statement_text,'')) LIKE '%hive_metastore.%') AS hive_metastore_reference_queries_30,
  PERCENTILE_APPROX(execution_duration_ms,0.95) AS epoch_p95_execution_ms,
  100.0 * COUNT_IF(COALESCE(waiting_at_capacity_duration_ms,0) > 0)/NULLIF(COUNT(*),0) AS epoch_queued_query_pct,
  100.0 * COUNT_IF(COALESCE(spilled_local_bytes,0) > 0)/NULLIF(COUNT(*),0) AS epoch_spill_query_pct,
  SUM(total_task_duration_ms) AS epoch_task_duration_ms,
  -- Queries per complete UTC week (week 1 = the 7 days before today), null for a week not wholly inside the epoch:
  -- the growth guard compares the last 4 weeks with the 4 before.
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 7 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 7 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 0 DAYS) END AS queries_week_1,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 14 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 14 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 7 DAYS) END AS queries_week_2,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 21 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 21 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 14 DAYS) END AS queries_week_3,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 28 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 28 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 21 DAYS) END AS queries_week_4,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 35 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 35 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 28 DAYS) END AS queries_week_5,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 42 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 42 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 35 DAYS) END AS queries_week_6,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 49 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 49 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 42 DAYS) END AS queries_week_7,
  CASE WHEN epoch_start <= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 56 DAYS THEN COUNT_IF(start_time >= CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 56 DAYS AND start_time < CAST(CURRENT_DATE() AS TIMESTAMP) - INTERVAL 49 DAYS) END AS queries_week_8
FROM q
GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

# Spill concentration (current epoch, last 30 days): spilled bytes per statement source (saved query, dashboard, job,
# notebook, else a hash of the statement text; a redacted statement counts on its own, never merged with others) and the
# share carried by the top three sources. The statement text itself is never exported.
def build_spill_source_sql(t: Thresholds) -> str:
    """Spill concentration collector, with the number of top sources rendered from Thresholds."""
    return SPILL_SOURCE_SQL_TEMPLATE.replace("__TOP_SOURCES__", str(int(t.spill_concentration_top_sources)))


SPILL_SOURCE_SQL_TEMPLATE = r"""
WITH __CONFIG_EPOCHS__,
cur AS (SELECT workspace_id, warehouse_id, epoch_start FROM config_epochs WHERE epoch_end IS NULL),
src AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start,
    COALESCE(x.query_source.sql_query_id, x.query_source.dashboard_id, x.query_source.legacy_dashboard_id,
      CAST(x.query_source.job_info.job_id AS STRING), x.query_source.notebook_id,
      CASE WHEN x.statement_text IS NULL OR x.statement_text = '<REDACTED>' THEN x.statement_id ELSE SHA2(x.statement_text, 256) END) AS source_key,
    SUM(CAST(x.spilled_local_bytes AS DOUBLE)) AS spilled_bytes
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id AND x.start_time >= c.epoch_start
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
    AND COALESCE(x.from_result_cache, FALSE) = FALSE AND x.spilled_local_bytes > 0
  GROUP BY 1, 2, 3, 4
), ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, epoch_start ORDER BY spilled_bytes DESC, source_key) AS rn,
    SUM(spilled_bytes) OVER (PARTITION BY workspace_id, warehouse_id, epoch_start) AS total_bytes
  FROM src
)
SELECT workspace_id, warehouse_id, epoch_start, COUNT(*) AS spill_sources_30,
  100.0 * SUM(CASE WHEN rn <= __TOP_SOURCES__ THEN spilled_bytes ELSE 0 END) / MAX(total_bytes) AS top_spill_source_share_pct_30
FROM ranked GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

EPOCH_EVENT_METRICS_SQL = r"""
WITH __CONFIG_EPOCHS__,
epochs AS (SELECT workspace_id, warehouse_id, min_clusters, max_clusters, epoch_start, epoch_end FROM config_epochs),
events AS (
  SELECT
    workspace_id,
    warehouse_id,
    event_type,
    COALESCE(cluster_count,0) AS cluster_count,
    event_time,
    LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, event_type, cluster_count) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
),
segments AS (
  SELECT
    p.workspace_id,
    p.warehouse_id,
    p.epoch_start,
    p.epoch_end,
    p.min_clusters,
    p.max_clusters,
    e.event_type,
    e.event_time,
    e.cluster_count,
    GREATEST(e.event_time,p.epoch_start) AS segment_start,
    LEAST(COALESCE(e.next_event_time,CURRENT_TIMESTAMP()),COALESCE(p.epoch_end,CURRENT_TIMESTAMP())) AS segment_end
  FROM events e
  JOIN epochs p
    ON e.workspace_id=p.workspace_id
   AND e.warehouse_id=p.warehouse_id
   AND e.event_time < COALESCE(p.epoch_end,CURRENT_TIMESTAMP())
   AND COALESCE(e.next_event_time,CURRENT_TIMESTAMP()) > p.epoch_start
),
durations AS (
  SELECT *,
    GREATEST(0.0,TIMESTAMPDIFF(SECOND,segment_start,segment_end)/60.0) AS duration_minutes
  FROM segments
  WHERE segment_end > segment_start
)
SELECT
  workspace_id,
  warehouse_id,
  epoch_start,
  SUM(CASE WHEN cluster_count>0 THEN duration_minutes ELSE 0 END) AS uptime_minutes,
  SUM(CASE WHEN cluster_count>0 THEN duration_minutes*cluster_count ELSE 0 END) AS cluster_minutes,
  SUM(CASE WHEN cluster_count=min_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END) AS minutes_at_min,
  SUM(CASE WHEN cluster_count=max_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END) AS minutes_at_max,
  100.0*SUM(CASE WHEN cluster_count=min_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END)
    /NULLIF(SUM(CASE WHEN cluster_count>0 THEN duration_minutes ELSE 0 END),0) AS time_at_min_pct,
  100.0*SUM(CASE WHEN cluster_count=max_clusters AND cluster_count>0 THEN duration_minutes ELSE 0 END)
    /NULLIF(SUM(CASE WHEN cluster_count>0 THEN duration_minutes ELSE 0 END),0) AS time_at_max_pct,
  MAX(cluster_count) AS max_observed_clusters,
  -- Like the recent window's peak: counts above the ceiling are cluster-recycling transients, not demand.
  MAX(CASE WHEN cluster_count<=max_clusters THEN cluster_count END) AS max_observed_clusters_within_ceiling,
  MIN(segment_start) AS event_coverage_start,
  COUNT_IF(event_type='STARTING' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS starts_30,
  COUNT_IF(event_type='SCALED_UP' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS scale_ups_30,
  COUNT_IF(event_type='SCALED_DOWN' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS scale_downs_30
FROM durations
GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

def build_epoch_gap_metrics_sql(t: Thresholds) -> str:
    """Busy time and the inter-query gap sample per epoch (auto-stop savings come from the running-idle collector)."""
    return f"""
WITH {CONFIG_EPOCHS_CTE},
epochs AS (SELECT workspace_id, warehouse_id, auto_stop_minutes, epoch_start, epoch_end FROM config_epochs),
retained AS (
  -- warehouse_events keeps 365 days: running time is only known after a warehouse's first retained event, so the
  -- gap sample covers the same period as the event totals it is divided by.
  SELECT workspace_id, warehouse_id, MIN(event_time) AS first_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
  GROUP BY workspace_id, warehouse_id
),
ordered AS (
  SELECT x.workspace_id, x.compute.warehouse_id AS warehouse_id, p.epoch_start, p.auto_stop_minutes,
    x.start_time, x.end_time,
    MAX(x.end_time) OVER (
      PARTITION BY x.workspace_id,x.compute.warehouse_id,p.epoch_start
      ORDER BY x.start_time, x.end_time, x.statement_id
      ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
    ) AS previous_busy_end_time
  FROM system.query.history x
  JOIN epochs p ON x.workspace_id=p.workspace_id
    AND x.compute.warehouse_id=p.warehouse_id
    AND x.start_time>=p.epoch_start AND (p.epoch_end IS NULL OR x.start_time<p.epoch_end)
  JOIN retained r ON r.workspace_id=x.workspace_id AND r.warehouse_id=x.compute.warehouse_id
    AND x.start_time >= r.first_event_time
  WHERE x.compute.type='WAREHOUSE' AND x.start_time>=CURRENT_TIMESTAMP()-INTERVAL 365 DAYS
), busy AS (
  -- Wall-clock minutes with at least one query running (length of the union of query intervals), over the same
  -- period as the gaps: each query adds only the part that extends past every earlier-starting query. Milliseconds:
  -- most queries last under a second, and whole-second differences would drop them.
  SELECT workspace_id, warehouse_id, epoch_start,
    SUM(GREATEST(0.0,TIMESTAMPDIFF(MILLISECOND,GREATEST(start_time,COALESCE(previous_busy_end_time,start_time)),COALESCE(end_time,start_time))/60000.0)) AS busy_minutes
  FROM ordered GROUP BY workspace_id,warehouse_id,epoch_start
), gaps AS (
  SELECT *, GREATEST(0.0,TIMESTAMPDIFF(MILLISECOND,previous_busy_end_time,start_time)/60000.0) AS gap_minutes
  FROM ordered WHERE previous_busy_end_time IS NOT NULL AND start_time>previous_busy_end_time
), gap_stats AS (
  SELECT workspace_id, warehouse_id, epoch_start,
  COUNT(*) AS gap_count,
  PERCENTILE_APPROX(gap_minutes,0.50) AS p50_gap_minutes,
  PERCENTILE_APPROX(gap_minutes,0.90) AS p90_gap_minutes
  FROM gaps GROUP BY workspace_id,warehouse_id,epoch_start
)
SELECT b.workspace_id, b.warehouse_id, b.epoch_start, b.busy_minutes,
  COALESCE(g.gap_count,0) AS gap_count, g.p50_gap_minutes, g.p90_gap_minutes
FROM busy b LEFT JOIN gap_stats g
  ON g.workspace_id=b.workspace_id AND g.warehouse_id=b.warehouse_id AND g.epoch_start=b.epoch_start
"""

def auto_stop_targets(t: Thresholds) -> List[int]:
    """Every auto-stop value any supported warehouse type may be evaluated at (the collector measures each)."""
    return list(range(min(t.auto_stop_floor_serverless, t.auto_stop_floor_pro_classic), t.auto_stop_max_minutes + 1, t.auto_stop_step_minutes))


def auto_stop_candidates(t: Thresholds, warehouse_type: str) -> List[int]:
    """Auto-stop values evaluated for one warehouse type: every step from the type's floor up to the maximum."""
    floor = t.auto_stop_floor_serverless if warehouse_type == "SERVERLESS" else t.auto_stop_floor_pro_classic
    return [x for x in auto_stop_targets(t) if x >= floor]


def is_nonprod(tags: Mapping[str, Any], t: Thresholds) -> bool:
    """Nonprod profile by warehouse tag: a known environment key (case-insensitive) with a known nonprod value."""
    keys, values = set(t.nonprod_tag_keys), set(t.nonprod_tag_values)
    return any(str(k).strip().lower() in keys and str(v).strip().lower() in values for k, v in (tags or {}).items())


RUNNING_IDLE_TARGET_FIELDS = ("removable_running_wall_minutes", "removable_running_cluster_minutes", "additional_starts",
                              "avoided_starts", "added_idle_wall_minutes")
RUNNING_IDLE_HEADER_FIELDS = ("running_idle_window_start", "running_idle_window_end", "running_idle_queries", "running_segment_count",
                              "observed_running_cluster_minutes", "kept_alive_wall_minutes", "kept_alive_gap_count", "restarts_for_queries")


def running_idle_metrics(busy: Sequence[Tuple[float, float]], segments: Sequence[Tuple[float, float, int]], window: Tuple[float, float],
                         auto_stop: int, targets: Sequence[float], full_fraction: float = 0.99, margin_pct: float = 10.0,
                         kept_alive_tolerance: float = 2.0, stopped_gap_lag: float = 10.0) -> Dict[str, Any]:
    """Reference implementation of the running-idle collector (times in minutes, intervals half-open).

    ``busy`` are query intervals (start, end), ``segments`` RUNNING intervals (start, end, clusters > 0), ``window`` the
    evidence period (the same period as the event totals). An idle gap runs from the latest busy end before a query to
    that query's start; the gap after the last query runs to the window end (from the window start when there was no
    query). Under target T the warehouse would stop T minutes into a gap, but a lower auto-stop cannot stop it later
    than the current setting already does: running time past the current auto-stop was kept alive by something other
    than queries (open sessions, result fetching) and is reported; a gap kept alive longer than ``kept_alive_tolerance``
    minutes (stop events can trail the auto-stop) would not stop at a lower value either, so it contributes no
    removable time and no added restart. A value covers a gap (keeps the
    warehouse up until the next query) only when it is at least ``margin_pct`` longer than the gap, so a small schedule
    drift does not turn every gap into a restart. Per T (``running_idle_by_target``, only values with a non-zero field):

    * removable running wall/cluster minutes: RUNNING time in [gap start + T, min(gap end, gap start + current));
    * additional starts: gaps with a next query that the warehouse ran through (it did not restart for that query)
      and that T does not cover;
    * avoided starts / added idle wall minutes (raising auto-stop): gaps the warehouse stopped in, restarting for the
      next query; under T it stays up for min(gap, T) minutes, and a gap T covers saves its restart.
    """
    w0, w1 = window
    cover = 1.0 + margin_pct / 100.0
    gaps: List[Tuple[float, float, bool]] = []
    frontier: Optional[float] = None
    for start, end in sorted(busy):
        if frontier is not None and start > frontier:
            gaps.append((frontier, start, True))
        frontier = end if frontier is None else max(frontier, end)
    last = w0 if frontier is None else frontier
    if last < w1:
        gaps.append((last, w1, False))
    segs = [(max(a, w0), min(b, w1), c) for a, b, c in segments if c > 0 and min(b, w1) > max(a, w0)]  # clipped to the window
    # Cumulative running time F(x) = sum over segments of max(0, min(x, end) - start): with segment starts and ends
    # sorted, F(x) = sum over starts < x of (x - start) minus sum over ends < x of (x - end), so any overlap is
    # F(b) - F(a) in O(log n).
    def prefix(points: List[Tuple[float, float]]) -> Tuple[List[float], List[float], List[float], List[float], List[float]]:
        points = sorted(points)
        xs = [x for x, _ in points]; cw, cwx, cn, cnx = [0.0], [0.0], [0.0], [0.0]
        for x, w in points:
            cw.append(cw[-1] + w); cwx.append(cwx[-1] + w * x); cn.append(cn[-1] + 1); cnx.append(cnx[-1] + x)
        return xs, cw, cwx, cn, cnx
    starts, ends = prefix([(a, c) for a, _, c in segs]), prefix([(b, c) for _, b, c in segs])

    def cumulative(x: float, weighted: bool) -> float:
        total = 0.0
        for (xs, cw, cwx, cn, cnx), sign in ((starts, 1.0), (ends, -1.0)):
            k = bisect.bisect_left(xs, x)
            total += sign * ((cw[k] * x - cwx[k]) if weighted else (cn[k] * x - cnx[k]))
        return total

    def overlap(a: float, b: float, weighted: bool = False) -> float:
        return max(0.0, cumulative(b, weighted) - cumulative(a, weighted)) if b > a else 0.0

    out: Dict[str, Any] = {"running_segment_count": len(segs),
                           "observed_running_cluster_minutes": round(sum((b - a) * c for a, b, c in segs), 3)}
    kept_alive, kept_gaps, restarts = 0.0, 0, 0
    per_gap = []
    for g0, g1, has_next in gaps:
        if g1 <= w0:
            continue
        c0, c1 = max(g0, w0), min(g1, w1)
        stop_at = g0 + auto_stop if auto_stop > 0 else None
        running = overlap(c0, c1)
        alive = overlap(max(c0, stop_at), c1) if stop_at is not None else 0.0
        ran_through = running >= full_fraction * (c1 - c0)
        # Kept alive: ran through to the next query (or the window end) more than the tolerance past the auto-stop, or
        # stopped before it but only after running longer than a late stop event explains (a session held it open).
        kept = alive > kept_alive_tolerance and (ran_through or alive > stopped_gap_lag)
        kept_alive += alive if kept else 0.0; kept_gaps += 1 if kept else 0; restarts += 1 if has_next and not ran_through else 0
        per_gap.append((g0, g1, has_next, c0, c1, stop_at, running, ran_through, kept))
    out["kept_alive_wall_minutes"] = round(kept_alive, 3)
    out["kept_alive_gap_count"] = kept_gaps
    out["restarts_for_queries"] = restarts
    by_target: Dict[str, Dict[str, Any]] = {}
    # Only gap/value pairs that can contribute (as in the SQL): a gap the warehouse ran through matters to values that
    # do not cover it; a gap it stopped in matters to every value.
    through = sorted((x for x in per_gap if x[7]), key=lambda x: x[1] - x[0], reverse=True)
    stopped = [x for x in per_gap if not x[7]]
    for target in targets:
        wall = cluster = added_idle = 0.0
        added = avoided = 0
        k = 0
        while k < len(through) and target < cover * (through[k][1] - through[k][0]):
            k += 1
        for g0, g1, has_next, c0, c1, stop_at, running, ran_through, kept in through[:k] + stopped:
            r0 = max(g0 + target, c0)
            r1 = c0 if kept else c1 if stop_at is None else min(c1, stop_at)
            wall += overlap(r0, r1); cluster += overlap(r0, r1, True)
            covered = target >= cover * (g1 - g0)
            # A restart is added only where the warehouse ran through to the next query because of the current setting
            # (a gap within the current auto-stop); one kept alive past it would not stop at a lower value either.
            if has_next and ran_through and not covered and not kept and (stop_at is None or stop_at >= g1):
                added += 1
            if not ran_through:
                added_idle += max(0.0, min(g1 - g0, float(target)) - running)
                if has_next and covered:
                    avoided += 1
        row = {"removable_running_wall_minutes": round(wall, 3), "removable_running_cluster_minutes": round(cluster, 3),
               "additional_starts": added, "avoided_starts": avoided, "added_idle_wall_minutes": round(added_idle, 3)}
        if any(row.values()):
            by_target[str(target)] = row
    out["running_idle_by_target"] = by_target
    return out


def _running_idle_ctes(t: Thresholds) -> str:
    """Shared CTEs of the running-idle collector: same arithmetic as ``running_idle_metrics``.

    Same period as the event totals (events.cluster_minutes): from the later of the epoch start and the warehouse's
    first event retained in the last 365 days, to now. Milliseconds: whole-second differences would drop sub-second
    remainders on every gap.
    """
    full = t.running_idle_full_window_fraction
    cover = 1.0 + t.auto_stop_bridge_margin_pct / 100.0
    tolerance = t.kept_alive_gap_tolerance_minutes
    stopped_lag = t.kept_alive_stopped_gap_minutes
    targets = auto_stop_targets(t)
    return f"""
WITH {CONFIG_EPOCHS_CTE},
retained AS (
  SELECT workspace_id, warehouse_id, MIN(event_time) AS first_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
  GROUP BY workspace_id, warehouse_id
), cur AS (
  -- Current epoch over the period the event totals cover: running time is known only after the first retained event.
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.auto_stop_minutes,
    GREATEST(c.epoch_start, r.first_event_time) AS w_start, CURRENT_TIMESTAMP() AS w_end
  FROM config_epochs c JOIN retained r ON r.workspace_id = c.workspace_id AND r.warehouse_id = c.warehouse_id
  WHERE c.epoch_end IS NULL
), ev AS (
  SELECT workspace_id, warehouse_id, event_type, COALESCE(cluster_count, 0) AS cluster_count, event_time,
    LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, event_type, cluster_count) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
), running AS (
  SELECT c.workspace_id, c.warehouse_id, v.cluster_count,
    GREATEST(v.event_time, c.w_start) AS seg_start,
    LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end) AS seg_end
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE v.cluster_count > 0 AND v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
), q AS (
  -- Queries from a day before the window seed the busy frontier; a query without an end time ends at start + total
  -- duration; with neither, one started in the last day is still running (busy until now), an older one ends at start.
  SELECT c.workspace_id, c.warehouse_id, x.statement_id, x.start_time,
    COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, x.total_duration_ms, x.start_time),
      CASE WHEN x.start_time >= c.w_end - INTERVAL 1 DAY THEN c.w_end ELSE x.start_time END) AS busy_end
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id
    AND x.start_time >= GREATEST(c.epoch_start, c.w_start - INTERVAL 1 DAY) AND x.start_time < c.w_end
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 366 DAYS
), ordered AS (
  SELECT *, MAX(busy_end) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY start_time, busy_end, statement_id
    ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_busy_end
  FROM q
), gap_rows AS (
  SELECT workspace_id, warehouse_id, prev_busy_end AS g0, start_time AS g1, TRUE AS has_next
  FROM ordered WHERE prev_busy_end IS NOT NULL AND start_time > prev_busy_end
  UNION ALL
  -- After the last query (from the window start when there was none) the gap runs to the window end.
  SELECT c.workspace_id, c.warehouse_id, COALESCE(MAX(q.busy_end), c.w_start) AS g0, c.w_end AS g1, FALSE AS has_next
  FROM cur c LEFT JOIN q ON q.workspace_id = c.workspace_id AND q.warehouse_id = c.warehouse_id
  GROUP BY c.workspace_id, c.warehouse_id, c.w_start, c.w_end
  HAVING COALESCE(MAX(q.busy_end), c.w_start) < c.w_end
), g AS (
  -- A lower auto-stop cannot stop the warehouse later than the current setting does: running time past it was kept
  -- alive by something other than queries.
  SELECT r.workspace_id, r.warehouse_id, r.g0, r.g1, r.has_next,
    GREATEST(r.g0, c.w_start) AS c0, LEAST(r.g1, c.w_end) AS c1,
    TIMESTAMPDIFF(MILLISECOND, r.g0, r.g1) / 60000.0 AS gap_min,
    CASE WHEN c.auto_stop_minutes > 0 THEN TIMESTAMPADD(MINUTE, c.auto_stop_minutes, r.g0) END AS stop_at
  FROM gap_rows r JOIN cur c ON r.workspace_id = c.workspace_id AND r.warehouse_id = c.warehouse_id
  WHERE r.g1 > c.w_start
), pieces AS (
  -- RUNNING time inside each gap (inner join: a gap with no running segment has no piece, never a NULL bound).
  SELECT /*+ RANGE_JOIN(s, 3600) */ g.workspace_id, g.warehouse_id, g.g0, g.g1, s.cluster_count,
    GREATEST(g.c0, s.seg_start) AS p0, LEAST(g.c1, s.seg_end) AS p1
  FROM g JOIN running s ON s.workspace_id = g.workspace_id AND s.warehouse_id = g.warehouse_id
    AND s.seg_start < g.c1 AND s.seg_end > g.c0
), gap_running AS (
  SELECT g.workspace_id, g.warehouse_id, g.g0, g.g1,
    COALESCE(SUM(TIMESTAMPDIFF(MILLISECOND, p.p0, p.p1)), 0) / 60000.0 AS running_min,
    COALESCE(SUM(CASE WHEN g.stop_at IS NOT NULL AND p.p1 > g.stop_at
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, g.stop_at), p.p1) END), 0) / 60000.0 AS kept_alive_min
  FROM g LEFT JOIN pieces p ON p.workspace_id = g.workspace_id AND p.warehouse_id = g.warehouse_id AND p.g0 = g.g0 AND p.g1 = g.g1
  GROUP BY g.workspace_id, g.warehouse_id, g.g0, g.g1
), gaps_full AS (
  -- A gap run past the current auto-stop by more than a late stop event explains (the tolerance when it ran through
  -- to the next query, the stopped-gap lag otherwise) was kept alive by something other than queries and would not
  -- stop at a lower value either: no removable time, no added restart.
  SELECT g.*, gr.running_min, gr.kept_alive_min,
    gr.running_min >= {full} * TIMESTAMPDIFF(MILLISECOND, g.c0, g.c1) / 60000.0 AS ran_through,
    gr.kept_alive_min > {tolerance} AND (gr.running_min >= {full} * TIMESTAMPDIFF(MILLISECOND, g.c0, g.c1) / 60000.0
      OR gr.kept_alive_min > {stopped_lag}) AS kept_alive
  FROM g JOIN gap_running gr ON gr.workspace_id = g.workspace_id AND gr.warehouse_id = g.warehouse_id AND gr.g0 = g.g0 AND gr.g1 = g.g1
), per_target AS (
  -- Only pairs that can contribute: a gap the warehouse ran through matters to values that do not cover it (with the
  -- margin); a gap it stopped in matters to every value (removable time, added idle, restarts saved).
  SELECT f.*, t.target_min,
    GREATEST(TIMESTAMPADD(MINUTE, t.target_min, f.g0), f.c0) AS r_start,
    CASE WHEN f.kept_alive THEN f.c0 WHEN f.stop_at IS NULL THEN f.c1 ELSE LEAST(f.c1, f.stop_at) END AS r_end
  FROM gaps_full f CROSS JOIN (SELECT EXPLODE(SEQUENCE({targets[0]}, {targets[-1]}, {t.auto_stop_step_minutes})) AS target_min) t
  WHERE NOT f.ran_through OR t.target_min < {cover} * f.gap_min
), removable AS (
  SELECT m.workspace_id, m.warehouse_id, m.g0, m.g1, m.has_next, m.gap_min, m.running_min, m.ran_through, m.target_min,
    NOT m.kept_alive AND (m.stop_at IS NULL OR m.stop_at >= m.g1) AS within_current,
    COALESCE(SUM(CASE WHEN m.r_end > m.r_start AND p.p1 > m.r_start AND p.p0 < m.r_end
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.r_start), LEAST(p.p1, m.r_end)) END), 0) / 60000.0 AS wall_min,
    COALESCE(SUM(CASE WHEN m.r_end > m.r_start AND p.p1 > m.r_start AND p.p0 < m.r_end
      THEN TIMESTAMPDIFF(MILLISECOND, GREATEST(p.p0, m.r_start), LEAST(p.p1, m.r_end)) * p.cluster_count END), 0) / 60000.0 AS cluster_min
  FROM per_target m LEFT JOIN pieces p ON p.workspace_id = m.workspace_id AND p.warehouse_id = m.warehouse_id AND p.g0 = m.g0 AND p.g1 = m.g1
  GROUP BY m.workspace_id, m.warehouse_id, m.g0, m.g1, m.has_next, m.gap_min, m.running_min, m.ran_through, m.target_min, m.stop_at, m.kept_alive
)"""


def build_running_idle_sql(t: Thresholds) -> str:
    """Running-idle collector, one row per current warehouse: window, queries, running segments, kept-alive time."""
    return _running_idle_ctes(t) + """
, gap_stats AS (
  SELECT workspace_id, warehouse_id, SUM(CASE WHEN kept_alive THEN kept_alive_min ELSE 0 END) AS kept_alive_wall_minutes, COUNT_IF(kept_alive) AS kept_alive_gap_count,
    COUNT_IF(has_next AND NOT ran_through) AS restarts_for_queries
  FROM gaps_full GROUP BY workspace_id, warehouse_id
), seg_stats AS (
  SELECT workspace_id, warehouse_id, COUNT(*) AS running_segment_count,
    SUM(TIMESTAMPDIFF(MILLISECOND, seg_start, seg_end) * cluster_count) / 60000.0 AS observed_running_cluster_minutes
  FROM running WHERE seg_end > seg_start GROUP BY workspace_id, warehouse_id
), query_stats AS (
  SELECT q.workspace_id, q.warehouse_id, COUNT(*) AS running_idle_queries
  FROM q JOIN cur c ON q.workspace_id = c.workspace_id AND q.warehouse_id = c.warehouse_id
  WHERE q.start_time >= c.w_start GROUP BY q.workspace_id, q.warehouse_id
)
SELECT c.workspace_id, c.warehouse_id, c.epoch_start,
  c.w_start AS running_idle_window_start, c.w_end AS running_idle_window_end,
  COALESCE(qs.running_idle_queries, 0) AS running_idle_queries,
  COALESCE(ss.running_segment_count, 0) AS running_segment_count,
  COALESCE(ss.observed_running_cluster_minutes, 0) AS observed_running_cluster_minutes,
  COALESCE(gs.kept_alive_wall_minutes, 0) AS kept_alive_wall_minutes,
  COALESCE(gs.kept_alive_gap_count, 0) AS kept_alive_gap_count,
  COALESCE(gs.restarts_for_queries, 0) AS restarts_for_queries
FROM cur c
LEFT JOIN gap_stats gs ON gs.workspace_id = c.workspace_id AND gs.warehouse_id = c.warehouse_id
LEFT JOIN seg_stats ss ON ss.workspace_id = c.workspace_id AND ss.warehouse_id = c.warehouse_id
LEFT JOIN query_stats qs ON qs.workspace_id = c.workspace_id AND qs.warehouse_id = c.warehouse_id
"""


def build_running_idle_targets_sql(t: Thresholds) -> str:
    """Running-idle collector, one row per current warehouse and auto-stop value with a non-zero field (absent = 0)."""
    cover = 1.0 + t.auto_stop_bridge_margin_pct / 100.0
    return _running_idle_ctes(t) + f"""
SELECT c.workspace_id, c.warehouse_id, c.epoch_start, m.target_min,
  SUM(m.wall_min) AS removable_running_wall_minutes,
  SUM(m.cluster_min) AS removable_running_cluster_minutes,
  COUNT_IF(m.has_next AND m.ran_through AND m.within_current AND m.target_min < {cover} * m.gap_min) AS additional_starts,
  COUNT_IF(m.has_next AND NOT m.ran_through AND m.target_min >= {cover} * m.gap_min) AS avoided_starts,
  SUM(CASE WHEN NOT m.ran_through THEN GREATEST(0, LEAST(m.gap_min, m.target_min) - m.running_min) ELSE 0 END) AS added_idle_wall_minutes
FROM removable m JOIN cur c ON c.workspace_id = m.workspace_id AND c.warehouse_id = m.warehouse_id
GROUP BY c.workspace_id, c.warehouse_id, c.epoch_start, m.target_min
HAVING SUM(m.wall_min) > 0 OR SUM(m.cluster_min) > 0 OR COUNT_IF(m.has_next AND m.ran_through AND m.within_current AND m.target_min < {cover} * m.gap_min) > 0
  OR COUNT_IF(m.has_next AND NOT m.ran_through AND m.target_min >= {cover} * m.gap_min) > 0
  OR SUM(CASE WHEN NOT m.ran_through THEN GREATEST(0, LEAST(m.gap_min, m.target_min) - m.running_min) ELSE 0 END) > 0
ORDER BY c.workspace_id, c.warehouse_id, m.target_min
"""


CONCURRENCY_SCOPES = ("RECENT", "EPOCH", "BUSY_START")


def concurrency_histograms(intervals: Sequence[Tuple[float, float, float]], epoch_window: Tuple[float, float], recent_start: Optional[float],
                           lag_minutes: int) -> Dict[str, Dict[str, float]]:
    """Reference implementation of the concurrency collector (times in minutes from any origin aligned to UTC minutes).

    ``intervals`` are (start, end, queries) spans of running or queued queries (one query each in the collector). Each
    UTC minute's concurrency is the time-weighted number of queries in it (query-minutes / 1); its level is that number
    rounded up. Histograms (level -> weight, levels as strings): EPOCH = busy minutes (level >= 1) over the epoch
    window, RECENT = busy minutes from ``recent_start`` (current epoch only), BUSY_START = for each busy period that
    starts after at least ``lag_minutes`` idle minutes, its highest level in its first ``lag_minutes`` minutes (count of
    periods), within the recent window.
    """
    lo, hi = epoch_window
    per_minute: Dict[int, float] = {}
    for a, b, w in intervals:
        a, b = max(a, lo), min(b, hi)
        while a < b:
            m = math.floor(a + 1e-9)
            cut = min(b, m + 1.0)
            per_minute[m] = per_minute.get(m, 0.0) + (cut - a) * w
            a = cut
    out: Dict[str, Dict[str, float]] = {"EPOCH": {}}

    def add(hist: Dict[str, float], level: int, weight: float = 1.0) -> None:
        hist[str(level)] = hist.get(str(level), 0.0) + weight

    busy = sorted(m for m, c in per_minute.items() if c > 1e-9)
    for m in busy:
        add(out["EPOCH"], math.ceil(per_minute[m] - 1e-9))
    if recent_start is not None:
        out["RECENT"], out["BUSY_START"] = {}, {}
        recent = [m for m in busy if m >= recent_start]
        for m in recent:
            add(out["RECENT"], math.ceil(per_minute[m] - 1e-9))
        prev: Optional[int] = None
        for i, m in enumerate(recent):
            if prev is None or m - prev > lag_minutes:
                peak = max(math.ceil(per_minute[x] - 1e-9) for x in recent[i:] if x < m + lag_minutes)
                add(out["BUSY_START"], peak)
            prev = m
    return out


def histogram_percentile(hist: Optional[Mapping[str, Any]], pct: float) -> Optional[int]:
    """Smallest level whose cumulative weight reaches pct% of the total; None when the histogram is empty or missing."""
    if not hist:
        return None
    items = sorted((int(k), safe_float(v)) for k, v in hist.items())
    total = sum(w for _, w in items)
    if total <= 0:
        return None
    run = 0.0
    for level, w in items:
        run += w
        if run >= pct / 100.0 * total - 1e-9:
            return level
    return items[-1][0]


def histogram_top_level(hist: Optional[Mapping[str, Any]], minutes: float) -> Optional[int]:
    """Highest level reached in at least `minutes` of weight (the level of the minutes-th busiest minute); None when the
    histogram holds less weight than that (too little busy time to have a sustained peak)."""
    if not hist:
        return None
    items = sorted(((int(k), safe_float(v)) for k, v in hist.items()), reverse=True)
    run = 0.0
    for level, w in items:
        run += w
        if run >= minutes - 1e-9:
            return level
    return None


def recent_peak_at_rate(recent: Optional[Mapping[str, Any]], long_days: float, t: Thresholds, recent_days: float = 30.0) -> Optional[int]:
    """The recent (30-day) window's peak read at the same rate as a long window's: a long window's peak is the level
    reached in peak_demand_min_minutes busy minutes over long_days, so the recent window's is the level reached in
    peak_demand_min_minutes x recent_days / long_days of its busy minutes (at least 1); recent_days is the window's
    actual span (30, or the current epoch's age when younger). A recent window with busy minutes but
    less weight than that counts at its highest level; one with no busy minute counts as 0; None when not collected."""
    if recent is None:
        return None
    minutes = max(1.0, t.peak_demand_min_minutes * max(recent_days, 0.0) / max(long_days, 1.0))
    level = histogram_top_level(recent, minutes)
    if level is None:
        level = max((int(k) for k, v in recent.items() if safe_float(v) > 0), default=0)
    return level


def window_days(start: Optional[str], end: Optional[str], analysis_time: str, horizon_days: int = 365) -> float:
    """Calendar days of [start, end) (end None = analysis time) within the last horizon_days."""
    a, b = parse_datetime(start), parse_datetime(end) if end else parse_datetime(analysis_time)
    lo = parse_datetime(analysis_time) - dt.timedelta(days=horizon_days)
    if a is None or b is None:
        return 0.0
    return max(0.0, (min(b, parse_datetime(analysis_time)) - max(a, lo)).total_seconds() / 86400.0)


def build_concurrency_sql(t: Thresholds) -> str:
    """Per-minute query concurrency (running + queued, time-weighted) as histograms per epoch (last 365 days).

    Same arithmetic as ``concurrency_histograms``: EPOCH for every epoch, RECENT (last 30 days) and BUSY_START for the
    current epoch. A query occupies [start_time, end_time); milliseconds keep sub-second queries.
    """
    return f"""
WITH {CONFIG_EPOCHS_CTE},
ep AS (
  SELECT workspace_id, warehouse_id, epoch_start, epoch_end, warehouse_type,
    GREATEST(epoch_start, CURRENT_TIMESTAMP() - INTERVAL 365 DAYS) AS e_start, COALESCE(epoch_end, CURRENT_TIMESTAMP()) AS e_end,
    CASE WHEN warehouse_type = 'SERVERLESS' THEN {t.scale_up_lag_minutes_serverless} ELSE {t.scale_up_lag_minutes_pro_classic} END AS lag_min
  FROM config_epochs
  WHERE COALESCE(epoch_end, CURRENT_TIMESTAMP()) > CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
), q0 AS (
  SELECT p.workspace_id, p.warehouse_id, p.epoch_start, p.epoch_end, p.lag_min,
    GREATEST(x.start_time, p.e_start) AS s,
    LEAST(COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, x.total_duration_ms, x.start_time),
      CASE WHEN x.start_time >= p.e_end - INTERVAL 1 DAY THEN p.e_end ELSE x.start_time END), p.e_end) AS t
  FROM system.query.history x
  JOIN ep p ON x.workspace_id = p.workspace_id AND x.compute.warehouse_id = p.warehouse_id
    AND x.start_time >= p.epoch_start AND x.start_time < p.e_end
  WHERE x.compute.type = 'WAREHOUSE' AND x.start_time >= CURRENT_TIMESTAMP() - INTERVAL 366 DAYS
), q AS (
  -- Zero-length spans are dropped before the minute explode (SEQUENCE would be empty or reversed).
  SELECT * FROM q0 WHERE t > s
), qm AS (
  SELECT q.workspace_id, q.warehouse_id, q.epoch_start, q.epoch_end, q.lag_min, m AS minute_start,
    TIMESTAMPDIFF(MILLISECOND, GREATEST(q.s, m), LEAST(q.t, TIMESTAMPADD(MINUTE, 1, m))) AS ms
  FROM q LATERAL VIEW EXPLODE(SEQUENCE(DATE_TRUNC('MINUTE', q.s), DATE_TRUNC('MINUTE', TIMESTAMPADD(MILLISECOND, -1, q.t)), INTERVAL 1 MINUTE)) mm AS m
), minutes AS (
  SELECT workspace_id, warehouse_id, epoch_start, epoch_end, lag_min, minute_start, CAST(CEIL(SUM(ms) / 60000.0) AS INT) AS level
  FROM qm GROUP BY workspace_id, warehouse_id, epoch_start, epoch_end, lag_min, minute_start
  HAVING SUM(ms) > 0
), recent AS (
  SELECT *, CASE WHEN LAG(minute_start) OVER w IS NULL
      OR TIMESTAMPDIFF(MINUTE, LAG(minute_start) OVER w, minute_start) > lag_min THEN 1 ELSE 0 END AS starts_period
  FROM minutes
  WHERE epoch_end IS NULL AND minute_start >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS
  WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY minute_start)
), periods AS (
  SELECT *, SUM(starts_period) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY minute_start ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS period_id
  FROM recent
), period_peaks AS (
  -- Each busy period's highest level in its first lag minutes: demand the autoscaler cannot meet above the floor yet.
  SELECT workspace_id, warehouse_id, epoch_start, period_id, MAX(level) AS level
  FROM (SELECT *, MIN(minute_start) OVER (PARTITION BY workspace_id, warehouse_id, period_id) AS period_start FROM periods) x
  WHERE minute_start < TIMESTAMPADD(MINUTE, lag_min, period_start)
  GROUP BY workspace_id, warehouse_id, epoch_start, period_id
)
SELECT workspace_id, warehouse_id, epoch_start, 'EPOCH' AS scope, level, COUNT(*) AS weight
FROM minutes GROUP BY workspace_id, warehouse_id, epoch_start, level
UNION ALL
SELECT workspace_id, warehouse_id, epoch_start, 'RECENT' AS scope, level, COUNT(*) AS weight
FROM recent GROUP BY workspace_id, warehouse_id, epoch_start, level
UNION ALL
SELECT workspace_id, warehouse_id, epoch_start, 'BUSY_START' AS scope, level, COUNT(*) AS weight
FROM period_peaks GROUP BY workspace_id, warehouse_id, epoch_start, level
ORDER BY workspace_id, warehouse_id, epoch_start, scope, level
"""


RESOURCE_DAILY_COLLECT_DAYS = 120  # covers the 84-day window plus realistic billing lag; Python clips to what was collected

# Daily resource time for the current epoch over the last 120 UTC days (Python trims to the billing basis). Warehouse-event
# segments are split at midnight so every minute lands on its own UTC day; the peak excludes cluster-recycling transients
# (cluster_count above max_clusters), which are counted separately.
RESOURCE_DAILY_SQL = r"""
WITH __CONFIG_EPOCHS__,
cur AS (
  SELECT workspace_id, warehouse_id, epoch_start, min_clusters, max_clusters,
    GREATEST(epoch_start, CAST(DATE_SUB(CURRENT_DATE(), 120) AS TIMESTAMP)) AS w_start,
    CAST(CURRENT_DATE() AS TIMESTAMP) AS w_end
  FROM config_epochs
  WHERE epoch_end IS NULL AND epoch_start < CAST(CURRENT_DATE() AS TIMESTAMP)
), ev AS (
  SELECT workspace_id, warehouse_id, event_type, COALESCE(cluster_count,0) AS cluster_count, event_time,
    LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, event_type, cluster_count) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
), seg AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.min_clusters, c.max_clusters, v.cluster_count,
    GREATEST(v.event_time, c.w_start) AS seg_start,
    LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end) AS seg_end
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
), days AS (
  SELECT c.workspace_id, c.warehouse_id, d AS day_start, TIMESTAMPADD(DAY, 1, d) AS day_end
  FROM cur c LATERAL VIEW explode(sequence(CAST(DATE(c.w_start) AS TIMESTAMP), TIMESTAMPADD(DAY, -1, c.w_end), INTERVAL 1 DAY)) t AS d
), piece AS (
  SELECT d.workspace_id, d.warehouse_id, s.epoch_start, s.min_clusters, s.max_clusters, s.cluster_count,
    DATE(d.day_start) AS usage_day,
    GREATEST(0, TIMESTAMPDIFF(SECOND, GREATEST(s.seg_start, d.day_start), LEAST(s.seg_end, d.day_end))) / 60.0 AS m
  FROM days d JOIN seg s ON s.workspace_id = d.workspace_id AND s.warehouse_id = d.warehouse_id
    AND s.seg_start < d.day_end AND s.seg_end > d.day_start AND s.seg_end > s.seg_start
), starts AS (
  SELECT v.workspace_id, v.warehouse_id, DATE(v.event_time) AS usage_day, COUNT(*) AS starts
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE v.event_type = 'STARTING' AND v.event_time >= c.w_start AND v.event_time < c.w_end
  GROUP BY v.workspace_id, v.warehouse_id, DATE(v.event_time)
), daily AS (
  SELECT workspace_id, warehouse_id, epoch_start, usage_day,
    SUM(CASE WHEN cluster_count > 0 THEN m ELSE 0 END) AS uptime_minutes,
    SUM(CASE WHEN cluster_count > 0 THEN m * cluster_count ELSE 0 END) AS cluster_minutes,
    SUM(CASE WHEN cluster_count > 0 AND cluster_count = min_clusters THEN m ELSE 0 END) AS minutes_at_min,
    SUM(CASE WHEN cluster_count > 0 AND cluster_count = max_clusters THEN m ELSE 0 END) AS minutes_at_max,
    SUM(CASE WHEN cluster_count > max_clusters THEN m ELSE 0 END) AS recycle_minutes,
    MAX(CASE WHEN cluster_count <= max_clusters THEN cluster_count END) AS max_cluster_count
  FROM piece
  GROUP BY workspace_id, warehouse_id, epoch_start, usage_day
)
SELECT dl.*, COALESCE(st.starts, 0) AS starts, DATE_SUB(CURRENT_DATE(), 120) AS collected_from
FROM daily dl LEFT JOIN starts st
  ON st.workspace_id = dl.workspace_id AND st.warehouse_id = dl.warehouse_id AND st.usage_day = dl.usage_day
ORDER BY dl.workspace_id, dl.warehouse_id, dl.usage_day
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

# Usage with no matching list_prices row, or a row without a list price (promotional or not-yet-listed SKU), is kept
# and counted as unpriced DBU:
# an inner join would silently drop it and understate cost. Its cost is unknown (null), never zero. When price rows
# overlap, each usage record takes the latest price_start_time, so it is counted once. Every record_type stays in the
# signed sums (RETRACTION rows are negative); record_type only labels the retracted and restated quantities, which feed
# the billing correction share (a quality signal, not a filter).
EPOCH_DAILY_COST_SQL = r"""
WITH __CONFIG_EPOCHS__,
epochs AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),
cost AS (
  SELECT
    u.workspace_id,
    u.usage_metadata.warehouse_id AS warehouse_id,
    u.usage_date, u.usage_end_time,
    p.currency_code,
    u.sku_name,
    CAST(u.usage_quantity AS DOUBLE) AS dbu_quantity,
    CASE WHEN p.sku_name IS NULL OR p.pricing.effective_list.default IS NULL THEN CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS unpriced_dbu_quantity,
    CASE WHEN u.record_type='RETRACTION' THEN -CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS retracted_dbu_quantity,
    CASE WHEN u.record_type='RESTATEMENT' THEN CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS restated_dbu_quantity,
    CAST(u.usage_quantity AS DOUBLE)*CAST(p.pricing.effective_list.default AS DOUBLE) AS list_cost
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON p.account_id=u.account_id AND p.sku_name=u.sku_name AND p.cloud=u.cloud
   AND u.usage_end_time>=p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time<p.price_end_time)
  WHERE u.usage_metadata.warehouse_id IS NOT NULL
    AND u.billing_origin_product='SQL'
    AND u.usage_date>=DATE_SUB(CURRENT_DATE(),365) AND u.usage_date<CURRENT_DATE()
    AND (__INGESTION_CUTOFF__ IS NULL OR u.ingestion_date <= __INGESTION_CUTOFF__)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY p.price_start_time DESC NULLS LAST, p.currency_code, p.price_end_time) = 1
)
SELECT c.workspace_id,c.warehouse_id,e.epoch_start,c.usage_date,c.currency_code,
  SUM(c.dbu_quantity) AS dbu_quantity, SUM(c.unpriced_dbu_quantity) AS unpriced_dbu_quantity, SUM(c.list_cost) AS list_cost,
  SUM(c.retracted_dbu_quantity) AS retracted_dbu_quantity, SUM(c.restated_dbu_quantity) AS restated_dbu_quantity
FROM cost c JOIN epochs e ON c.workspace_id=e.workspace_id AND c.warehouse_id=e.warehouse_id
  AND c.usage_end_time>=e.epoch_start AND (e.epoch_end IS NULL OR c.usage_end_time<e.epoch_end)
GROUP BY c.workspace_id,c.warehouse_id,e.epoch_start,c.usage_date,c.currency_code
ORDER BY c.workspace_id,c.warehouse_id,c.usage_date
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

BILLING_FEATURE_SQL = r"""
SELECT
  workspace_id,
  usage_metadata.warehouse_id AS warehouse_id,
  100.0*SUM(CASE WHEN product_features.is_photon=TRUE THEN CAST(usage_quantity AS DOUBLE) ELSE 0 END)
    /NULLIF(SUM(CAST(usage_quantity AS DOUBLE)),0) AS photon_usage_pct_365,
  100.0*SUM(CASE WHEN product_features.is_serverless=TRUE THEN CAST(usage_quantity AS DOUBLE) ELSE 0 END)
    /NULLIF(SUM(CAST(usage_quantity AS DOUBLE)),0) AS serverless_usage_pct_365,
  MAX(cloud) AS cloud,
  COLLECT_SET(sku_name) AS sku_names
FROM system.billing.usage
WHERE usage_metadata.warehouse_id IS NOT NULL AND billing_origin_product='SQL'
  AND usage_date>=DATE_SUB(CURRENT_DATE(),365) AND usage_date<CURRENT_DATE()
  AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
GROUP BY workspace_id, usage_metadata.warehouse_id
"""

# Billing ingestion watermark: the newest usage hour already present in system.billing.usage. Databricks documents
# ORIGINAL records as "typically available within 12 hours" (longer for new workspaces) and gives no bound for
# RETRACTION/RESTATEMENT corrections, so a calendar day is treated as covered only once the watermark has passed its
# end. That is ingestion progress, not finality: corrections can still revise a covered day. Timestamps are UTC.
BILLING_WATERMARK_SQL = r"""
SELECT workspace_id, MAX(usage_end_time) AS billing_watermark, MAX(ingestion_date) AS billing_ingested_through
FROM system.billing.usage
WHERE usage_date>=DATE_SUB(CURRENT_DATE(),30) AND billing_origin_product='SQL'
  AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
GROUP BY workspace_id
"""

EPOCH_BILLING_ATTRIBUTION_SQL = r"""
WITH __CONFIG_EPOCHS__,
epochs AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),
usage AS (
  SELECT
    workspace_id,
    usage_metadata.warehouse_id AS warehouse_id,
    usage_end_time,
    CAST(usage_quantity AS DOUBLE) AS dbu_quantity,
    product_features.is_photon AS is_photon,
    -- A redacted or blank owner is unknown, never an owner, so it cannot win the latest-owner pick.
    CASE WHEN UPPER(TRIM(identity_metadata.owned_by)) IN ('__REDACTED__','<REDACTED>','REDACTED') OR TRIM(identity_metadata.owned_by)=''
         THEN NULL ELSE identity_metadata.owned_by END AS owned_by,
    usage_metadata.node_type AS node_type,
    product_features.sql_tier AS sql_tier
  FROM system.billing.usage
  WHERE usage_metadata.warehouse_id IS NOT NULL
    AND billing_origin_product='SQL'
    AND usage_unit='DBU'
    AND usage_date>=DATE_SUB(CURRENT_DATE(),365) AND usage_date<CURRENT_DATE()
    AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
)
SELECT
  u.workspace_id,
  u.warehouse_id,
  e.epoch_start,
  SUM(CASE WHEN u.is_photon=TRUE THEN u.dbu_quantity ELSE 0 END) AS photon_true_dbu,
  SUM(CASE WHEN u.is_photon=FALSE THEN u.dbu_quantity ELSE 0 END) AS photon_false_dbu,
  SUM(CASE WHEN u.is_photon IS NULL THEN u.dbu_quantity ELSE 0 END) AS photon_null_dbu,
  MAX(u.usage_end_time) AS latest_usage_time,
  MAX_BY(u.owned_by, CASE WHEN u.owned_by IS NOT NULL THEN u.usage_end_time END) AS latest_owner,
  ARRAY_SORT(COLLECT_SET(u.owned_by)) AS observed_owners,
  ARRAY_SORT(COLLECT_SET(u.node_type)) AS observed_node_types,
  ARRAY_SORT(COLLECT_SET(u.sql_tier)) AS observed_sql_tiers
FROM usage u
JOIN epochs e
  ON u.workspace_id=e.workspace_id
 AND u.warehouse_id=e.warehouse_id
 AND u.usage_end_time>=e.epoch_start
 AND (e.epoch_end IS NULL OR u.usage_end_time<e.epoch_end)
GROUP BY u.workspace_id,u.warehouse_id,e.epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

SQL_SKU_PRICE_SQL = r"""
SELECT
  account_id, sku_name, cloud, currency_code, usage_unit,
  CAST(pricing.effective_list.default AS DOUBLE) AS effective_list_price
FROM system.billing.list_prices
WHERE price_end_time IS NULL
  AND usage_unit = 'DBU'
  AND UPPER(sku_name) LIKE '%SQL%'
ORDER BY account_id, sku_name
"""

# ---------------------------------------------------------------------------
# Utilities / auth / collection
# ---------------------------------------------------------------------------

def failure_blockers(failure_pct: Optional[float], t: "Thresholds") -> List[str]:
    """Reliability guard for every capacity cut: a high or unknown failed/cancelled share holds the cut."""
    if failure_pct is None:
        return ["FAILURE_CANCEL_EVIDENCE_UNAVAILABLE"]
    return ["FAILURE_CANCEL_RATE_HIGH"] if failure_pct > t.failure_cancel_pct_downsize_safe else []


def dbu_baseline_blocker(e: "WarehouseEvidence", t: "Thresholds") -> str:
    """Why the forward DBU baseline is unknown.

    COST_HISTORY_INSUFFICIENT is the calendar gate (a young epoch; the re-evaluation window says when it clears);
    BILLED_COST_DAYS_INSUFFICIENT is a mature epoch billed on too few days (not time-boxed); DBU_COST_UNAVAILABLE is a
    cost that cannot be priced or a non-positive run rate.
    """
    if e.cost_history_days < t.min_economics_cost_history_days:
        return "COST_HISTORY_INSUFFICIENT"
    status = (e.forecast_metadata or {}).get("dbu_forecast_status")
    return "BILLED_COST_DAYS_INSUFFICIENT" if status in ("INSUFFICIENT_ACTIVE_COST_DAYS", "INSUFFICIENT_BILLING_CYCLES") else "DBU_COST_UNAVAILABLE"


MIGRATION_BASELINE_RATIONALE = {
    "COST_HISTORY_INSUFFICIENT": "Migration is judged on a pre-canary cost comparison, but the current epoch has fewer than 7 days of billing history to price it.",
    "BILLED_COST_DAYS_INSUFFICIENT": "Migration is judged on a pre-canary cost comparison, but the current epoch has fewer than 7 billed days to forecast its DBU cost.",
    "DBU_COST_UNAVAILABLE": "Migration is judged on a pre-canary cost comparison, but the current DBU cost is unavailable (unpriced usage or a non-positive run rate).",
}

DBU_BASELINE_WITHHELD = {
    # blocker -> (bundle status, reason shown on withheld plan economics)
    "COST_HISTORY_INSUFFICIENT": ("REQUIRES_COST_HISTORY", "At least one step cannot be priced because the current epoch has too little billing history; economics are withheld until it accrues."),
    "BILLED_COST_DAYS_INSUFFICIENT": ("UNAVAILABLE", "The current epoch has too few billed days to forecast its DBU cost; economics are withheld."),
    "DBU_COST_UNAVAILABLE": ("UNAVAILABLE", "The forward DBU cost is unavailable (unpriced usage or a non-positive run rate); economics are withheld."),
}


def optional_float(value: Any) -> Optional[float]:
    """Float or None when the source did not report a usable value; unknown evidence must not read as zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def cost_stability(cv: Optional[float]) -> Optional[float]:
    """Cost-stability confidence component in [0, 1]; None when the coefficient of variation is unknown."""
    return None if cv is None else clamp(1.0 - safe_float(cv) / 2.0, 0.0, 1.0)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def conditional_severity(q: Dict[str, Any], count_key: str, p95_key: str) -> Tuple[Optional[float], int]:
    """p95 severity among *affected* queries, plus how many queries were affected.

    Returns (None, 0) when the collector did not report the affected-query count (older evidence): the
    severity is unknown, never zero. A reported count of 0 means no query was affected: (0.0, 0).
    """
    if q.get(count_key) is None:
        return None, 0
    n = safe_int(q.get(count_key))
    if n <= 0:
        return 0.0, 0
    return (None if q.get(p95_key) is None else safe_float(q.get(p95_key))), n


def round_or_none(value: Optional[float], digits: int = 0) -> Optional[float]:
    return None if value is None else (round(value) if digits == 0 else round(value, digits))


def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def parse_datetime(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


def iso(value: Any) -> Optional[str]:
    parsed = parse_datetime(value)
    return parsed.astimezone(dt.timezone.utc).isoformat() if parsed else None


def normalize_size(value: Any) -> str:
    return "UNKNOWN" if value is None else str(value).strip().upper().replace("-", "_")


def _map_pairs(value: Any) -> Optional[Dict[str, str]]:
    """A MAP column as a mapping or, as some connectors return it, a list of [key, value] pairs or key/value entries."""
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        out: Dict[str, str] = {}
        for item in value:
            if isinstance(item, Mapping) and "key" in item and "value" in item:
                out[str(item["key"])] = str(item["value"])
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                out[str(item[0])] = str(item[1])
            else:
                return None
        return out
    return None


def normalize_map(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            obj = json.loads(value)
        except Exception:
            return {"raw": value}
        decoded = _map_pairs(obj)
        return decoded if decoded is not None else {"raw": value}
    decoded = _map_pairs(value)
    return decoded if decoded is not None else {"raw": str(value)}


def hive_reference_count(q: Mapping[str, Any]) -> Optional[int]:
    """Queries naming hive_metastore, or None when no statement text was visible (a zero would claim a clean scan)."""
    if safe_float(q.get("statement_text_visible_pct_30")) <= 0:
        return None
    return safe_int(q.get("hive_metastore_reference_queries_30"))


REDACTED_OWNER_VALUES = frozenset({"__REDACTED__", "<REDACTED>", "REDACTED"})


def clean_owner(value: Any) -> Optional[str]:
    """An owner identity, or None when it is blank or a redaction token (unknown, never an owner)."""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.upper() in REDACTED_OWNER_VALUES else text


def classify_sql_sku(sku_name: str) -> str:
    n = str(sku_name or "").upper()
    if "SQL" not in n:
        return "OTHER"
    if "SERVERLESS" in n:
        return "SERVERLESS"
    if "SQL_PRO" in n or "PRO_SQL" in n or ("PRO" in n and "SQL" in n):
        return "PRO"
    return "CLASSIC"


def load_aws_price_registry(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"AWS price registry not found: {path}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SystemExit(f"AWS price registry must be a JSON object: {path}")
    return dict(payload)


def aws_cluster_hourly_price(size: str, region: str, registry: Mapping[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    topology = AWS_SQL_WAREHOUSE_TOPOLOGY.get(size)
    if not topology:
        return None, {"status": "UNAVAILABLE", "blocker": "WAREHOUSE_SIZE_NOT_IN_AWS_TOPOLOGY", "warehouse_size": size}
    regions = registry.get("regions") if isinstance(registry, Mapping) else None
    region_prices = regions.get(region) if isinstance(regions, Mapping) else None
    if not isinstance(region_prices, Mapping):
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_REGION_NOT_IN_REGISTRY", "region": region}
    driver = str(topology["driver"]); workers = int(topology["workers"]); worker_type = "i3.2xlarge"
    driver_price = region_prices.get(driver); worker_price = region_prices.get(worker_type)
    if driver_price is None or worker_price is None:
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_INSTANCE_PRICE_MISSING", "region": region, "driver": driver, "worker": worker_type}
    hourly = safe_float(driver_price) + workers * safe_float(worker_price)
    return round(hourly, 6), {
        "status": "MODELED", "pricing_mode": registry.get("pricing_mode", "UNKNOWN"), "currency": registry.get("currency", "USD"),
        "region": region, "warehouse_size": size, "driver_instance": driver, "driver_hourly_price": safe_float(driver_price),
        "worker_instance": worker_type, "worker_count": workers, "worker_hourly_price": safe_float(worker_price),
        "cluster_hourly_price": round(hourly, 6), "sources": registry.get("sources", []),
        "region_instance_prices": {str(k): safe_float(v) for k, v in region_prices.items()},
    }


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


def canonical_json(value: Any) -> str:
    return json.dumps(as_serializable(value), sort_keys=True, separators=(",", ":"), default=str)


def as_serializable(value: Any) -> Any:
    if is_dataclass(value):
        return as_serializable(asdict(value))
    if isinstance(value, Mapping):
        return {str(k): as_serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [as_serializable(v) for v in value]
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    return value


# Internal join keys. They tie findings, decisions and plan steps together while a run is in memory, and `epoch_id` also
# keys epochs in evidence.json. They are not public identities: packages carry only the contract IDs from
# contracts/reporter_contract.py (make_epoch_id, make_evidence_id, make_candidate_id, make_scenario_id), which never read
# these. tests/test_locked_reporter_integration.py pins that.
def epoch_id(workspace_id: str, warehouse_id: str, start: str) -> str:
    """Internal epoch key: 16 hex over workspace, warehouse and start time. Not the public EPOCH- identity."""
    raw = f"{workspace_id}|{warehouse_id}|{start}"
    return "EPOCH-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def finding_id(workspace_id: str, warehouse_id: str, knob: str, epoch: str, metric: str) -> str:
    """Internal finding key, mapped to a public EVID- identity when the package is built."""
    raw = f"{ANALYZER_VERSION}|{workspace_id}|{warehouse_id}|{knob}|{epoch}|{metric}"
    return "FND-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def recommendation_id(workspace_id: str, warehouse_id: str, knob: str, epoch: str, current: Mapping[str, Any], recommended: Mapping[str, Any]) -> str:
    """Internal decision key, mapped to a public CAND- identity when the package is built."""
    raw = canonical_json({
        "contract_version": CONTRACT_VERSION,
        "policy_version": POLICY_VERSION,
        "decision_version": DECISION_VERSION,
        "estimator_version": ESTIMATOR_VERSION,
        "workspace_id": workspace_id,
        "warehouse_id": warehouse_id,
        "knob": knob,
        "epoch": epoch,
        "current": current,
        "recommended": recommended,
    })
    return f"WHOPT-{knob.upper()}-{hashlib.sha256(raw.encode()).hexdigest()[:16].upper()}"


def scenario_id(workspace_id: str, warehouse_id: str, name: str, recommendation_ids: Sequence[str]) -> str:
    """Internal plan key, mapped to a public SCN- identity when the package is built."""
    raw = canonical_json({"v": DECISION_VERSION, "workspace": workspace_id, "warehouse": warehouse_id, "name": name, "ids": list(recommendation_ids)})
    return "SCN-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


# Why a ceiling increase's best case is zero added cost, stated with the candidate and its pricing method.
MAX_INCREASE_COST_BOUNDS = ("Cost: the best case adds no cost, because the same queries run on more clusters at once and the "
                            "total running time does not grow; the worst case runs every added cluster whenever the warehouse "
                            "sat at its current ceiling.")


def billed_days_recent(e: "WarehouseEvidence", window_days: int) -> Optional[Dict[str, Any]]:
    """Billed days among the last `window_days` settled days of the current epoch's forecast basis, with their cost.

    None when the basis is unknown. A day counts as billed when it has positive DBU usage; cost is None when any of
    those days carries unpriced usage (unknown, never understated).
    """
    meta = e.forecast_metadata or {}
    start, end = parse_datetime(meta.get("forecast_basis_start")), parse_datetime(meta.get("forecast_basis_end"))
    if not start or not end or end < start:
        return None
    first = max(start.date(), end.date() - dt.timedelta(days=window_days - 1))
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
    return {"settled_days": (end.date() - first).days + 1, "billed_days": sum(1 for v in dbu.values() if v > 1e-12),
            "billed_cost": None if unpriced else round(cost, 2), "first_day": first.isoformat(), "last_day": end.date().isoformat()}


def epoch_age_days(epoch: EpochEvidence, as_of: Optional[str] = None) -> int:
    start = parse_datetime(epoch.epoch_start)
    if not start:
        return 0
    end = parse_datetime(epoch.epoch_end) or parse_datetime(as_of)
    if not end:
        raise ValueError("Current epoch age requires a deterministic analysis_time")
    return max(0, (end - start).days)


def infer_photon_observation(row: Mapping[str, Any], analysis_time: str, thresholds: Thresholds) -> PhotonObservation:
    """Infer observed Photon state from signed current-epoch DBU billing usage.

    Corrections remain safe because system.billing.usage retractions carry negative
    quantities and restatements carry corrected positive quantities. The inference
    is intentionally conservative: insufficient coverage, stale evidence, or a
    mixed workload produces UNKNOWN/MIXED instead of pretending to know config.
    """
    true_dbu = max(0.0, safe_float(row.get("photon_true_dbu")))
    false_dbu = max(0.0, safe_float(row.get("photon_false_dbu")))
    null_dbu = max(0.0, safe_float(row.get("photon_null_dbu")))
    known = true_dbu + false_dbu
    total = known + null_dbu
    coverage = (100.0 * known / total) if total > 0 else 0.0
    enabled_pct = (100.0 * true_dbu / known) if known > 0 else None
    latest = iso(row.get("latest_usage_time"))
    latest_dt = parse_datetime(latest) if latest else None
    as_of = parse_datetime(analysis_time)
    freshness_days = None
    if latest_dt and as_of:
        freshness_days = max(0, (as_of.astimezone(dt.timezone.utc) - latest_dt.astimezone(dt.timezone.utc)).days)

    sufficient = (
        total >= thresholds.photon_min_epoch_dbu
        and coverage >= thresholds.photon_min_feature_coverage_pct
        and freshness_days is not None
        and freshness_days <= thresholds.photon_freshness_days
        and enabled_pct is not None
    )
    if not sufficient:
        status = "UNKNOWN"
    elif enabled_pct >= thresholds.photon_enabled_usage_pct:
        status = "ENABLED"
    elif enabled_pct <= thresholds.photon_disabled_usage_pct:
        status = "DISABLED"
    else:
        status = "MIXED"

    freshness_score = 0.0 if freshness_days is None else clamp(1.0 - freshness_days / max(1.0, float(thresholds.photon_freshness_days)), 0.0, 1.0)
    coverage_score = clamp(coverage / 100.0, 0.0, 1.0)
    volume_score = clamp(total / max(thresholds.photon_min_epoch_dbu * 10.0, 1.0), 0.0, 1.0)
    purity = 0.0 if enabled_pct is None else max(enabled_pct, 100.0 - enabled_pct) / 100.0
    score = clamp(0.35 * coverage_score + 0.25 * freshness_score + 0.20 * volume_score + 0.20 * purity, 0.0, 1.0)
    if status == "UNKNOWN":
        score = min(score, 0.49)
    elif status == "MIXED":
        score = min(score, 0.59)
    confidence = {
        "score": round(score, 3),
        "level": "HIGH" if score >= thresholds.confidence_high_threshold else "MEDIUM" if score >= thresholds.confidence_medium_threshold else "LOW",
        "components": {
            "feature_coverage": round(coverage_score, 3),
            "freshness": round(freshness_score, 3),
            "dbu_volume": round(volume_score, 3),
            "state_purity": round(purity, 3),
        },
    }
    return PhotonObservation(
        status=status,
        enabled_usage_pct=(round(enabled_pct, 3) if enabled_pct is not None else None),
        known_feature_coverage_pct=round(coverage, 3),
        true_dbu=round(true_dbu, 6),
        false_dbu=round(false_dbu, 6),
        null_dbu=round(null_dbu, 6),
        latest_usage_time=latest,
        freshness_days=freshness_days,
        confidence=confidence,
    )


def as_string_list(value: Any) -> Optional[List[str]]:
    """A collector array column as sorted distinct strings: a list, a JSON array string, or a Spark array rendered as
    text ("[a, b]"). None stays None (not collected)."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            text = value.strip()
            value = [v.strip().strip('"') for v in text[1:-1].split(",")] if text.startswith("[") and text.endswith("]") else [text]
        if not isinstance(value, list):
            value = [value]
    return sorted({str(x) for x in value if x not in (None, "")})


def derive_query_source_profile(query: Mapping[str, Any]) -> QuerySourceProfile:
    """Create a deterministic multi-label source profile from query history aggregates."""
    categories = {
        "DASHBOARD": safe_int(query.get("dashboard_queries_30")),
        "JOB": safe_int(query.get("job_queries_30")),
        "NOTEBOOK": safe_int(query.get("notebook_queries_30")),
        "GENIE": safe_int(query.get("genie_queries_30")),
        "ALERT": safe_int(query.get("alert_queries_30")),
        "SQL_EDITOR": safe_int(query.get("sql_query_source_queries_30")),
        "POWER_BI": safe_int(query.get("power_bi_queries_30")),
        "TABLEAU": safe_int(query.get("tableau_queries_30")),
        "LOOKER": safe_int(query.get("looker_queries_30")),
    }
    q30 = max(0, safe_int(query.get("queries_30")))
    percentages = {k: round(100.0 * v / q30, 3) if q30 else 0.0 for k, v in categories.items()}
    ranked = sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))
    primary = ranked[0][0] if ranked and ranked[0][1] > 0 else "UNATTRIBUTED"
    apps = as_string_list(query.get("client_applications_30")) or []
    return QuerySourceProfile(
        categories=categories, percentages=percentages, primary_category=primary,
        client_applications=sorted({str(x) for x in apps if x is not None}), multi_label=True,
    )


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


@dataclass(frozen=True)
class AuthContext:
    """Resolved local OAuth authentication context; token is never reported or logged."""
    profile: str
    host: str
    http_path: str
    access_token: str
    token_expiry: Optional[str]
    execution_kind: str
    execution_id: str
    workspace_id: Optional[str] = None


def _load_profile_values(profile: str, config_file: Optional[str] = None) -> Dict[str, str]:
    path = Path(config_file or os.getenv("DATABRICKS_CONFIG_FILE") or Path.home() / ".databrickscfg")
    if not path.exists():
        raise SystemExit(f"Databricks CLI config not found: {path}")
    parser = configparser.ConfigParser()
    parser.read(path)
    if profile == "DEFAULT":
        section: Mapping[str, str] = parser.defaults()
    elif parser.has_section(profile):
        section = parser[profile]
    else:
        raise SystemExit(f"Databricks CLI profile not found: {profile} in {path}")
    return {str(k): str(v) for k, v in section.items()}


def _load_profile_host(profile: str, config_file: Optional[str] = None) -> str:
    section = _load_profile_values(profile, config_file)
    host = str(section.get("host") or "").strip()
    if not host:
        raise SystemExit(f"Databricks host is missing from CLI profile: {profile}")
    return host.replace("https://", "").replace("http://", "").rstrip("/")


def _resolve_cli_token(profile: str, config_file: Optional[str] = None) -> Tuple[str, str, Optional[str]]:
    values = _load_profile_values(profile, config_file)
    if values.get("token"):
        raise SystemExit(f"Profile {profile} contains a PAT token. Use an OAuth U2M Databricks CLI profile.")
    host = _load_profile_host(profile, config_file)
    cmd = ["databricks", "auth", "token", profile, "-o", "json"]
    clean_env = dict(os.environ)
    clean_env.pop("DATABRICKS_TOKEN", None)
    if config_file:
        clean_env["DATABRICKS_CONFIG_FILE"] = str(config_file)
    LOGGER.info("auth.resolve profile=%s host=%s", profile, host)
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True, env=clean_env)
    except FileNotFoundError as exc:
        raise SystemExit("Databricks CLI is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise SystemExit(f"Unable to resolve OAuth token for profile {profile}: {detail}") from exc
    payload = json.loads(result.stdout)
    token = str(payload.get("access_token") or "")
    if not token:
        raise SystemExit(f"Databricks CLI returned no access token for profile {profile}")
    return host, token, str(payload.get("expiry") or "") or None


def _api_get(host: str, token: str, path: str, params: Optional[Mapping[str, Any]] = None, *, return_headers: bool = False) -> Any:
    url = f"https://{host.rstrip('/')}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": f"sql-warehouse-optimizer/{PUBLISHER_VERSION}"}, method="GET")
    started = time.monotonic()
    LOGGER.info("api.get start path=%s", path)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode("utf-8")
            payload = json.loads(body) if body.strip() else {}
            LOGGER.info("api.get done path=%s status=%s elapsed_ms=%d", path, getattr(response, "status", 200), int((time.monotonic()-started)*1000))
            return (payload, dict(response.headers.items())) if return_headers else payload
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        LOGGER.warning("api.get failed path=%s error=%s", path, type(exc).__name__)
        raise


def _current_workspace_id(host: str, token: str) -> Optional[str]:
    try:
        _payload, headers = _api_get(host, token, "/api/2.0/preview/scim/v2/Me", return_headers=True)
        for key, value in headers.items():
            if key.lower() == "x-databricks-org-id" and str(value).strip():
                return str(value).strip()
    except Exception:
        return None
    return None


def _validate_all_purpose_cluster(host: str, token: str, cluster_id: str) -> Dict[str, Any]:
    payload = _api_get(host, token, "/api/2.1/clusters/get", {"cluster_id": cluster_id})
    source = str(payload.get("cluster_source") or "").upper()
    if source == "JOB":
        raise SystemExit("The Databricks SQL Connector does not support jobs compute. Supply an all-purpose cluster ID.")
    LOGGER.info("execution.cluster validated cluster_id=%s name=%s source=%s state=%s", cluster_id, payload.get("cluster_name"), source or "UNKNOWN", payload.get("state"))
    return {"cluster_id": cluster_id, "cluster_name": payload.get("cluster_name"), "cluster_source": source, "state": payload.get("state")}


def resolve_cli_oauth(profile: str, execution_kind: str, execution_id: Optional[str] = None, config_file: Optional[str] = None, workspace_id: Optional[str] = None) -> AuthContext:
    """Resolve U2M OAuth and build the connector path for a SQL warehouse or all-purpose cluster.

    Read-only REST is used only when necessary to resolve/validate an all-purpose
    cluster execution target. It is never a source of recommendation configs.
    Backward compatibility: resolve_cli_oauth(profile, warehouse_id, config_file).
    """
    if execution_kind.lower().strip() not in {"warehouse","cluster"}:
        legacy_warehouse_id=execution_kind
        if config_file is None and execution_id:
            config_file=str(execution_id)
        execution_kind="warehouse"; execution_id=legacy_warehouse_id
    if not execution_id:
        raise SystemExit("execution_id is required")
    host, token, expiry = _resolve_cli_token(profile, config_file)
    kind = execution_kind.lower().strip()
    resolved_workspace_id = workspace_id or os.getenv("DATABRICKS_WORKSPACE_ID")
    if kind == "warehouse":
        http_path = f"/sql/1.0/warehouses/{execution_id}"
    elif kind == "cluster":
        _validate_all_purpose_cluster(host, token, execution_id)
        resolved_workspace_id = resolved_workspace_id or _current_workspace_id(host, token)
        if not resolved_workspace_id:
            raise SystemExit("Unable to resolve workspace ID for all-purpose cluster HTTP path. Supply --workspace-id or DATABRICKS_WORKSPACE_ID.")
        http_path = f"sql/protocolv1/o/{resolved_workspace_id}/{execution_id}"
    else:
        raise SystemExit(f"Unsupported execution kind: {execution_kind}. Use warehouse or cluster.")
    LOGGER.info("execution.target kind=%s id=%s workspace_id=%s http_path=%s", kind, execution_id, resolved_workspace_id or "UNKNOWN", http_path)
    return AuthContext(profile=profile, host=host, http_path=http_path, access_token=token, token_expiry=expiry, execution_kind=kind, execution_id=execution_id, workspace_id=resolved_workspace_id)


def _external_hive_config_keys(data_access_config: Any) -> List[str]:
    risky_tokens=("hive.metastore.uris","javax.jdo.option.connectionurl","datanucleus.connectionurl")
    keys=[]
    for item in data_access_config or []:
        key=str((item or {}).get("key") or "").strip()
        lower=key.lower()
        if key and any(tok in lower for tok in risky_tokens):
            keys.append(key)
    return sorted(set(keys))


def collect_workspace_compatibility_evidence(auth: AuthContext, mode: str = "auto") -> Dict[str, Any]:
    """Use read-only workspace SQL config when available; otherwise fall back to HITL.

    Data-access values are never retained or logged. Only config key names needed
    to detect an external legacy Hive metastore are kept.
    """
    if mode == "off":
        LOGGER.info("compatibility.api disabled workspace_id=%s", auth.workspace_id or "UNKNOWN")
        return {"mode":"A_HITL_ONLY","status":"API_DISABLED","workspace_id":auth.workspace_id}
    workspace_id=auth.workspace_id or _current_workspace_id(auth.host,auth.access_token)
    if not workspace_id:
        LOGGER.warning("compatibility.fallback reason=WORKSPACE_ID_UNAVAILABLE")
        return {"mode":"A_HITL_FALLBACK","status":"WORKSPACE_ID_UNAVAILABLE","workspace_id":None}
    try:
        cfg=_api_get(auth.host,auth.access_token,"/api/2.0/sql/config/warehouses")
        hive_keys=_external_hive_config_keys(cfg.get("data_access_config"))
        enabled_types={str(x.get("warehouse_type") or "").upper(): bool(x.get("enabled")) for x in (cfg.get("enabled_warehouse_types") or [])}
        LOGGER.info("compatibility.api available workspace_id=%s external_legacy_hive=%s", workspace_id, bool(hive_keys))
        return {
            "mode":"B_READ_ONLY_API",
            "status":"AVAILABLE",
            "workspace_id":str(workspace_id),
            "source":"GET /api/2.0/sql/config/warehouses",
            "enable_serverless_compute":cfg.get("enable_serverless_compute"),
            "enabled_warehouse_types":enabled_types,
            "security_policy":cfg.get("security_policy"),
            "external_legacy_hive_metastore_detected":bool(hive_keys),
            "external_legacy_hive_metastore_config_keys":hive_keys,
            "s3_access_policy_status":"UNKNOWN_NOT_EXPOSED_BY_VERIFIED_WORKSPACE_SQL_CONFIG_API",
            "data_access_config_values_retained":False,
        }
    except Exception as exc:
        LOGGER.warning("compatibility.fallback workspace_id=%s reason=READ_ONLY_API_UNAVAILABLE error=%s", workspace_id, type(exc).__name__)
        return {"mode":"A_HITL_FALLBACK","status":"READ_ONLY_API_UNAVAILABLE","workspace_id":str(workspace_id),"error_type":type(exc).__name__}


UTC_TIMEZONE_NAMES = frozenset({"UTC", "Etc/UTC", "Etc/UCT", "UCT", "Z", "+00:00", "GMT", "Etc/GMT"})


class SqlClient:
    """Thin read-only Databricks SQL connector wrapper with labeled query logging."""
    def __init__(self, auth: AuthContext):
        self.auth = auth
        self._connection = None

    def __enter__(self) -> "SqlClient":
        try:
            from databricks import sql  # type: ignore
        except ImportError as exc:
            raise SystemExit('Install dependency: python -m pip install "databricks-sql-connector>=4.2.6"') from exc
        # Billing usage_date and every analysis window are UTC; DATE()/CURRENT_DATE() follow the session time zone,
        # so the session is pinned to UTC and verified before any collector query runs.
        self._connection = sql.connect(
            server_hostname=self.auth.host,
            http_path=self.auth.http_path,
            access_token=self.auth.access_token,
            user_agent_entry=f"sql-warehouse-optimizer/{PUBLISHER_VERSION}",
            session_configuration={"TIMEZONE": "UTC"},
        )
        rows = self.query("SELECT current_timezone() AS tz", label="session_timezone")
        timezone = str(rows[0].get("tz") if rows else "")
        if timezone not in UTC_TIMEZONE_NAMES:
            self.__exit__(None, None, None)
            raise SystemExit(f"SQL session time zone is {timezone or 'unknown'}, not UTC; collector dates would not align with UTC billing dates.")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._connection is not None:
            self._connection.close()

    def query(self, statement: str, *, label: str = "collector_sql") -> List[Dict[str, Any]]:
        assert self._connection is not None
        started=time.monotonic()
        LOGGER.info("sql.start label=%s", label)
        with self._connection.cursor() as cursor:
            cursor.execute(statement)
            cols = [d[0] for d in cursor.description]
            rows=[dict(zip(cols, row)) for row in cursor.fetchall()]
        LOGGER.info("sql.done label=%s rows=%d elapsed_ms=%d", label, len(rows), int((time.monotonic()-started)*1000))
        return rows


def _warehouse_key(row: Mapping[str, Any]) -> Tuple[str, str]:
    return str(row.get("workspace_id") or ""), str(row.get("warehouse_id") or "")


def _row_key(row: Mapping[str, Any]) -> Tuple[str, str, str]:
    workspace_id, warehouse_id = _warehouse_key(row)
    return workspace_id, warehouse_id, iso(row.get("epoch_start")) or str(row.get("epoch_start"))


def _index_epoch_rows(rows: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    out: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for r in rows:
        out[_row_key(r)] = r
    return out


def _is_unpriced_only_row(row: Mapping[str, Any]) -> bool:
    return row.get("currency_code") is None and row.get("list_cost") is None and safe_float(row.get("unpriced_dbu_quantity")) > 0


def billing_cadence(series: Sequence[float], basis_start: dt.date, t: Thresholds) -> Dict[str, Any]:
    """Runs of consecutive billed days in a zero-filled daily cost series and their run-to-run cycles.

    periodic: short runs (median run length at most forecast_cadence_gap_days) repeating on a current cadence, with the
    longest gap between billed days and the median cycle both above forecast_cadence_gap_days (with fewer than three
    runs there is no median, so the gap alone decides). weekly: on that boundary (the gap or the median cycle equals
    it), where the cadence model is used only when the run rate has no number. A cadence is current when the last run
    started no more than one reference cycle (the median, or the only cycle) plus forecast_cadence_gap_days before the
    basis end. A run that starts on the basis's first day may be cut by it, so it is not a cycle anchor. The cycle cost
    is the cost from the first anchor run's first day up to the last run's first day: whole cycles only.
    """
    gap_limit = t.forecast_cadence_gap_days
    billed = [i for i, x in enumerate(series) if x > 1e-12]
    runs: List[List[int]] = []
    for i in billed:
        if runs and i - runs[-1][-1] <= 1:
            runs[-1].append(i)
        else:
            runs.append([i])
    starts = [r[0] for r in runs]
    gap = max((b - a for a, b in zip(billed, billed[1:])), default=None)
    cycles = [b - a for a, b in zip(starts, starts[1:])]
    median = statistics.median(cycles) if len(starts) >= 3 else None
    short_runs = bool(runs) and statistics.median(len(r) for r in runs) <= gap_limit
    reference = median if median is not None else (cycles[0] if cycles else None)
    current = reference is not None and (len(series) - 1 - starts[-1]) <= reference + gap_limit
    shape = short_runs and current
    periodic = shape and gap is not None and gap > gap_limit and (median is None or median > gap_limit)
    weekly = (shape and gap is not None and not periodic and gap >= gap_limit and (median is None or median >= gap_limit))
    anchors = starts[1:] if starts and starts[0] == 0 else starts
    day = lambda i: (basis_start + dt.timedelta(days=i)).isoformat()
    return {
        "periodic": periodic, "weekly": weekly, "runs": len(runs), "cycles": max(0, len(anchors) - 1),
        "median_cycle_days": median,
        "cycle_cost": sum(series[anchors[0]:anchors[-1]]) if len(anchors) >= 2 else 0.0,
        "cycle_days": (anchors[-1] - anchors[0]) if len(anchors) >= 2 else 0,
        "window_start": day(anchors[0]) if anchors else None, "window_end": day(anchors[-1]) if anchors else None,
        "run_starts": [day(i) for i in anchors],
    }


class Modeler:
    """Forward-looking baseline model using zero-filled calendar-day cost history."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def forecast_from_daily_cost(self, rows: Sequence[Dict[str, Any]], current_epoch_start: str, analysis_time: str, billing_watermark: Optional[str] = None, ingestion_cutoff: Optional[str] = None) -> Dict[str, Any]:
        analysis_dt = parse_datetime(analysis_time)
        current_start = parse_datetime(current_epoch_start)
        if not analysis_dt:
            raise ValueError(f"Invalid analysis_time: {analysis_time}")
        if not current_start:
            raise ValueError(f"Invalid current epoch start: {current_epoch_start}")

        # Unpriced usage rows (no list_prices match) carry no currency; they make cost unknown, not mixed.
        currencies = sorted({str(r.get("currency_code") or "USD") for r in rows if not _is_unpriced_only_row(r)})
        if len(currencies) > 1:
            raise ValueError(f"Mixed currencies cannot be aggregated: {currencies}")
        currency = currencies[0] if currencies else "USD"

        by_date: Dict[dt.date, float] = {}
        current_by_date: Dict[dt.date, float] = {}
        unpriced_by_date: Dict[dt.date, float] = {}
        # Billing corrections per day: (net DBU, retracted DBU, restated DBU); None when the row predates their collection.
        correction_by_date: Dict[dt.date, Optional[Tuple[float, float, float]]] = {}
        for r in rows:
            date_val = parse_datetime(r.get("usage_date"))
            if not date_val:
                continue
            d = date_val.date()
            cost = safe_float(r.get("list_cost"))
            by_date[d] = by_date.get(d, 0.0) + cost
            if d >= current_start.date():
                current_by_date[d] = current_by_date.get(d, 0.0) + cost
            unpriced = safe_float(r.get("unpriced_dbu_quantity"))
            if unpriced:
                unpriced_by_date[d] = unpriced_by_date.get(d, 0.0) + unpriced
            retracted, restated = optional_float(r.get("retracted_dbu_quantity")), optional_float(r.get("restated_dbu_quantity"))
            prior = correction_by_date.get(d, (0.0, 0.0, 0.0))
            if retracted is None or restated is None or prior is None:
                correction_by_date[d] = None
            else:
                correction_by_date[d] = (prior[0] + safe_float(r.get("dbu_quantity")), prior[1] + retracted, prior[2] + restated)

        analysis_date = analysis_dt.astimezone(dt.timezone.utc).date()
        # Half-open window [analysis_date-365, last complete day]. Today is incomplete; yesterday is complete only once
        # the billing watermark has passed its end (records are typically available within 12h, not guaranteed).
        window_start = analysis_date - dt.timedelta(days=365)
        # A day is complete once it has settled (billing_settle_hours after it ends) and, when known, the workspace's
        # billing watermark has passed it. Records arrive "typically within 12 hours"; the buffer covers that latency.
        settled_through = (settle_reference_time(analysis_time, ingestion_cutoff) - dt.timedelta(hours=self.t.billing_settle_hours)).date() - dt.timedelta(days=1)
        basis_end = min(analysis_date - dt.timedelta(days=1), settled_through)
        watermark_dt = parse_datetime(billing_watermark) if billing_watermark else None
        if watermark_dt is not None:
            complete_through = watermark_dt.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1)
            basis_end = min(basis_end, complete_through)
            completeness = "COVERED_BY_WATERMARK"
        else:
            completeness = "UNVERIFIED_ASSUMED_COMPLETE_THROUGH_PRIOR_DAY"
        excluded_incomplete_days = (analysis_date - dt.timedelta(days=1) - basis_end).days
        basis_start = max(current_start.astimezone(dt.timezone.utc).date(), window_start)
        unpriced_365 = round(sum(v for d, v in unpriced_by_date.items() if window_start <= d <= basis_end), 6)
        unpriced_in_basis = round(sum(v for d, v in unpriced_by_date.items() if basis_start <= d <= basis_end), 6)
        # Correction share over the same settled window as the trailing actual: retracted DBU / DBU originally billed
        # (net + retracted - restated, i.e. the ORIGINAL records). Retractions of earlier restatements also count, so it
        # can exceed 1 when a day is corrected repeatedly. A quality signal only; unknown when any day predates its
        # collection.
        window_corrections = [v for d, v in correction_by_date.items() if window_start <= d <= basis_end]
        retracted_365: Optional[float] = None
        restated_365: Optional[float] = None
        correction_share: Optional[float] = None
        if all(v is not None for v in window_corrections):
            retracted_365 = round(sum((v[1] for v in window_corrections), 0.0), 6)
            restated_365 = round(sum((v[2] for v in window_corrections), 0.0), 6)
            original = sum(v[0] for v in window_corrections) + retracted_365 - restated_365
            correction_share = round(retracted_365 / original, 6) if original > 0 else None
        # Unpriced DBU has unknown cost: the trailing actual (and, when inside the forecast basis, the forecast) is
        # unknown rather than understated.
        actual: Optional[float] = None if unpriced_365 > 0 else round(sum(v for d, v in by_date.items() if window_start <= d <= basis_end), 2)
        completeness_meta = {
            "billing_watermark": watermark_dt.astimezone(dt.timezone.utc).isoformat() if watermark_dt else None,
            "billing_completeness": completeness,
            "billing_complete_through": basis_end.isoformat(),
            "excluded_incomplete_days": excluded_incomplete_days,
            "billing_settle_hours": self.t.billing_settle_hours,
            "unpriced_dbu_quantity_365": unpriced_365,
            "unpriced_dbu_quantity_in_basis": unpriced_in_basis,
            "dbu_pricing_status": "UNPRICED_USAGE" if unpriced_365 > 0 else "COMPLETE",
            "billing_retracted_dbu_quantity_365": retracted_365,
            "billing_restated_dbu_quantity_365": restated_365,
            "billing_correction_dbu_share_365": correction_share,
        }

        series: List[float] = []
        if basis_start <= basis_end:
            d = basis_start
            while d <= basis_end:
                series.append(current_by_date.get(d, 0.0))
                d += dt.timedelta(days=1)

        if not series:
            return {
                "current_cost_365": actual,
                "forecast_cost_365": None,
                "dbu_forecast_status": "UNPRICED_USAGE" if unpriced_in_basis > 0 else "INSUFFICIENT_ACTIVE_COST_DAYS",
                "currency": currency,
                "cost_history_days": 0,
                "active_cost_days": 0,
                "cost_cv_84": None,
                **completeness_meta,
                "forecast_method": MODEL_VERSION,
                "forecast_model": "RUN_RATE",
                "forecast_basis_start": None,
                "forecast_basis_end": None,
            }

        def mean(x: Sequence[float]) -> float:
            return statistics.mean(x) if x else 0.0

        recent = self.t.forecast_recent_days
        long = self.t.forecast_long_days
        rate = whole_weeks(series)  # rates use whole weeks only, so weekday/weekend mix carries no bias
        last28 = rate[-recent:]
        last84 = rate[-long:]
        prev28 = rate[-2*recent:-recent] if len(rate) >= 2*recent else []
        mean28, mean84 = mean(last28), mean(last84)
        if len(rate) >= long:
            daily = self.t.forecast_weight_28_long * mean28 + (1 - self.t.forecast_weight_28_long) * mean84
        elif len(rate) >= recent:
            daily = self.t.forecast_weight_28_mid * mean28 + (1 - self.t.forecast_weight_28_mid) * mean(rate)
        else:
            daily = mean(rate)

        ratio = mean28 / mean(prev28) if prev28 and mean(prev28) > 0 else 1.0
        ratio = clamp(ratio, self.t.trend_ratio_floor, self.t.trend_ratio_ceiling)
        trend_adjustment_factor = 1.0 + self.t.trend_adjustment_weight * (ratio - 1.0)
        daily *= trend_adjustment_factor
        cv = statistics.pstdev(last84) / mean84 if len(last84) >= self.t.forecast_cv_min_days and mean84 > 0 else None
        active_cost_days = sum(1 for x in series if x > 1e-12)  # billed days; a correction-only (negative) day is not one
        rate_billed_days = sum(1 for x in rate[-long:] if x > 1e-12)  # the floor applies to the days the rate actually uses
        # The forward DBU cost is published only when it is known: every day priced, enough billed days to estimate a
        # run rate, and a positive run rate. Otherwise it is unknown (null), never $0 or a guess.
        if unpriced_in_basis > 0:
            forecast_status = "UNPRICED_USAGE"
        elif rate_billed_days < self.t.min_economics_cost_history_days:
            forecast_status = "INSUFFICIENT_ACTIVE_COST_DAYS"
        elif daily <= 0:
            forecast_status = "NON_POSITIVE_RUN_RATE"
        else:
            forecast_status = "COMPLETE"
        forecast_365 = round(daily * 365, 2) if forecast_status == "COMPLETE" else None
        cadence = billing_cadence(series, basis_start, self.t)
        model = "RUN_RATE"
        if cadence["periodic"] or (cadence["weekly"] and forecast_365 is None):
            # Periodic workload (billing gaps and cycles longer than a week; weekly only when the run rate has no
            # number): average whole run-to-run cycles, with no trend; fewer than forecast_min_cycles cycles is unknown.
            model = "CADENCE"
            ratio = None
            trend_adjustment_factor = 1.0
            cv = None
            if unpriced_in_basis > 0:
                forecast_status = "UNPRICED_USAGE"
            elif cadence["cycles"] < self.t.forecast_min_cycles:
                forecast_status = "INSUFFICIENT_BILLING_CYCLES"
            elif cadence["cycle_cost"] <= 0:
                forecast_status = "NON_POSITIVE_RUN_RATE"
            else:
                forecast_status = "COMPLETE"
            forecast_365 = round(cadence["cycle_cost"] / cadence["cycle_days"] * 365, 2) if forecast_status == "COMPLETE" else None
        if unpriced_in_basis > 0:
            # Unpriced days look like $0 days in the cost series: trend, run rates and stability are unknown, and the
            # AWS model gets no trend adjustment instead of a spurious downward one.
            ratio = None
            trend_adjustment_factor = 1.0
            cv = None
        return {
            "current_cost_365": actual,
            "forecast_cost_365": forecast_365,
            "forecast_model": model,
            "billing_runs": cadence["runs"],
            "billing_cycles": cadence["cycles"],
            "median_cycle_days": cadence["median_cycle_days"],
            "cadence_window_start": cadence["window_start"] if model == "CADENCE" else None,
            "cadence_window_end": cadence["window_end"] if model == "CADENCE" else None,
            "cadence_run_starts": cadence["run_starts"] if model == "CADENCE" else None,
            "dbu_forecast_status": forecast_status,
            "currency": currency,
            "cost_history_days": len(series),
            "active_cost_days": active_cost_days,
            "cost_cv_84": None if cv is None else round(cv, 4),
            "forecast_method": MODEL_VERSION,
            "run_rate_28": None if unpriced_in_basis > 0 else round(mean28 * 365, 2),
            "run_rate_84": None if unpriced_in_basis > 0 else round(mean84 * 365, 2),
            "trend_ratio": round_or_none(ratio, 6),
            "trend_adjustment_factor": round(trend_adjustment_factor, 6),
            "forecast_basis_start": basis_start.isoformat(),
            "forecast_basis_end": basis_end.isoformat(),
            **completeness_meta,
            "zero_cost_days_in_basis": sum(1 for x in series if abs(x) <= 1e-12),
            "rate_basis_days": min(len(rate), long),
            "rate_billed_days": rate_billed_days,
        }


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


RESOURCE_SERIES_FIELDS = ("uptime_minutes", "cluster_minutes", "minutes_at_min", "minutes_at_max", "recycle_minutes", "max_cluster_count", "starts")


def resource_window_from(epoch: EpochEvidence, basis_end: Optional[str], t: Thresholds, days: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The current epoch's daily resource series on the DBU forecast's calendar: the last forecast_long_days (or `days`)
    complete days ending at the billing basis end, clipped to the epoch start, missing days zero-filled. None when the
    series was not collected or no basis day exists."""
    end = parse_datetime(basis_end) if basis_end else None
    start_dt = parse_datetime(epoch.epoch_start)
    if epoch.resource_daily is None or end is None or start_dt is None:
        return None
    end_day = end.date()
    start_day = max(start_dt.astimezone(dt.timezone.utc).date(), end_day - dt.timedelta(days=(days or t.forecast_long_days) - 1))
    collected_from = parse_datetime(epoch.events.get("resource_daily_from"))
    if collected_from is not None:
        # Days before the collected range are unknown, never zero: the window shrinks rather than invents idle days.
        start_day = max(start_day, collected_from.date())
    if epoch.resource_daily and "resource_coverage_from" not in epoch.events:
        return None  # collected before the coverage marker existed: billed lead-in days cannot be told from idle ones
    coverage_from = parse_datetime(epoch.events.get("resource_coverage_from"))
    if coverage_from is not None:
        # Billed days before the first retained warehouse event have unknown running time (never idle): the collector
        # marks the first fully covered day. Unbilled lead-in days (not yet created, dormant) stay known zeros.
        start_day = max(start_day, coverage_from.date())
    if start_day > end_day:
        return None
    by_day = {str(r.get("usage_day"))[:10]: r for r in epoch.resource_daily}
    days = [start_day + dt.timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    window: Dict[str, Any] = {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": len(days)}
    for name in RESOURCE_SERIES_FIELDS:
        window[name] = [safe_float((by_day.get(d.isoformat()) or {}).get(name)) for d in days]
    return window


def resource_window(e: "WarehouseEvidence", t: Thresholds) -> Optional[Dict[str, Any]]:
    meta = e.forecast_metadata or {}
    return resource_window_from(e.current_epoch, meta.get("forecast_basis_end") or meta.get("billing_complete_through"), t)


def annualized_resource_rates(epoch: EpochEvidence, meta: Mapping[str, Any], t: Thresholds, fields: Sequence[str]) -> Tuple[Optional[Dict[str, float]], Dict[str, Any]]:
    """Annual totals of daily resource series on the DBU forecast's model, so AWS and DBU describe the same year.

    Run-rate model: the forecast's window and recent-weighted blend, times its trend adjustment. Cadence model: the same
    whole run-to-run cycles over the latest runs warehouse events cover (at least forecast_min_cycles of them), with no
    trend. Returns (None, info with a blocker) when the window is missing or holds too few covered cycles.
    """
    end = meta.get("forecast_basis_end") or meta.get("billing_complete_through")
    if meta.get("forecast_model") == "CADENCE":
        window = resource_window_from(epoch, end, t, days=365)
        if window is None:
            return None, {"blocker": "RECENT_RESOURCE_WINDOW_UNAVAILABLE"}
        first = dt.date.fromisoformat(window["start"])
        starts = [x for x in (meta.get("cadence_run_starts") or []) if x >= window["start"]]
        if len(starts) < t.forecast_min_cycles + 1:
            return None, {"blocker": "CADENCE_CYCLES_NOT_COVERED_BY_EVENTS", "cadence_cycles": max(0, len(starts) - 1),
                          "resource_window_start": window["start"], "resource_window_end": window["end"]}
        i0, i1 = ((dt.date.fromisoformat(x) - first).days for x in (starts[0], starts[-1]))
        rates = {f: sum(window[f][i0:i1]) / (i1 - i0) * 365.0 for f in fields}
        return rates, {"model": "CADENCE", "cadence_cycles": len(starts) - 1, "trend_adjustment_factor": 1.0, "window_cluster_minutes": sum(window["cluster_minutes"][i0:i1]),
                       "window_uptime_minutes": sum(window["uptime_minutes"][i0:i1]), "max_observed_clusters": max(window["max_cluster_count"][i0:i1], default=0.0),
                       "resource_window_start": starts[0], "resource_window_end": (dt.date.fromisoformat(starts[-1]) - dt.timedelta(days=1)).isoformat(), "resource_window_days": i1 - i0}
    window = resource_window_from(epoch, end, t)
    if window is None:
        return None, {"blocker": "RECENT_RESOURCE_WINDOW_UNAVAILABLE"}
    trend = safe_float(meta.get("trend_adjustment_factor"), 1.0)
    rates = {f: blended_daily_rate(window[f], t) * 365.0 * trend for f in fields}
    return rates, {"model": "RUN_RATE", "trend_adjustment_factor": trend, "window_cluster_minutes": sum(window["cluster_minutes"]),
                   "window_uptime_minutes": sum(window["uptime_minutes"]), "max_observed_clusters": max(window["max_cluster_count"], default=0.0),
                   "resource_window_start": window["start"], "resource_window_end": window["end"], "resource_window_days": window["days"]}


def whole_weeks(values: Sequence[float]) -> List[float]:
    """The most recent whole weeks of a daily series (all of it when shorter than a week)."""
    values = list(values)
    return values[len(values) % 7:] if len(values) >= 7 else values


def blended_daily_rate(values: Sequence[float], t: Thresholds) -> float:
    """Same recent-weighted blend as the DBU forecast, so AWS and DBU describe the same year."""
    values = whole_weeks(values)
    if not values:
        return 0.0
    recent, long = values[-t.forecast_recent_days:], values[-t.forecast_long_days:]
    if len(values) >= t.forecast_long_days:
        return t.forecast_weight_28_long * statistics.mean(recent) + (1 - t.forecast_weight_28_long) * statistics.mean(long)
    if len(values) >= t.forecast_recent_days:
        return t.forecast_weight_28_mid * statistics.mean(recent) + (1 - t.forecast_weight_28_mid) * statistics.mean(values)
    return statistics.mean(values)


def recent_capacity_facts(window: Optional[Mapping[str, Any]], t: Thresholds) -> Dict[str, Optional[float]]:
    """Time at min/max and the observed peak over the last pressure_window_days of the resource window."""
    if window is None:
        return {"state": "MISSING", "at_min_pct": None, "at_max_pct": None, "max_observed": None, "window_days": None,
                "minutes_at_max": None, "cluster_minutes": None}
    n = t.pressure_window_days
    uptime = sum(window["uptime_minutes"][-n:])
    peak = max(window["max_cluster_count"][-n:], default=0.0)
    return {
        # IDLE: collected, but the warehouse did not run in the window, so time-at-level shares are undefined.
        "state": "OK" if uptime > 0 else "IDLE",
        "at_min_pct": None if uptime <= 0 else round(100.0 * sum(window["minutes_at_min"][-n:]) / uptime, 4),
        "at_max_pct": None if uptime <= 0 else round(100.0 * sum(window["minutes_at_max"][-n:]) / uptime, 4),
        "max_observed": int(peak),
        "window_days": min(n, window["days"]),
        "minutes_at_max": round(sum(window["minutes_at_max"][-n:]), 6),
        "cluster_minutes": round(sum(window["cluster_minutes"][-n:]), 6),
    }


def size_busy_share(busy_minutes: Any, uptime_minutes: Any, cluster_minutes: Any, min_clusters: Any) -> Optional[float]:
    """Share of running cluster-minutes spent with queries running; None when unknown.

    Idle running time is assumed to hold the minimum cluster count (Classic/Pro scale down only after sustained low
    load), so idle cluster-minutes = (running minutes - busy minutes) x min clusters, and busy cluster-minutes are
    at least busy wall-minutes. Busy time above running time (sources disagree) counts as all busy.
    """
    busy = optional_float(busy_minutes); uptime = optional_float(uptime_minutes); cluster = optional_float(cluster_minutes)
    if busy is None or uptime is None or cluster is None or uptime <= 0 or cluster <= 0:
        return None
    idle_cluster_minutes = max(0.0, uptime - busy) * max(1, safe_int(min_clusters, 1))
    # Busy cluster-minutes are never below busy wall-minutes (cluster counts under the floor would otherwise give 0).
    return round(clamp(max(1.0 - idle_cluster_minutes / cluster, busy / cluster), 0.0, 1.0), 6)


def query_volume_growth_ratio(weeks: Sequence[Any]) -> Optional[float]:
    """Smallest of the last 4 weekly query counts over the median of the 4 weeks before; None when unknown.

    Using every recent week (not a total) keeps a single month-, quarter- or year-end burst week from looking like
    growth, and the median keeps one quiet or burst week in the earlier period from moving the base.
    """
    vals = [optional_float(x) for x in list(weeks)[:8]]
    if len(vals) < 8 or any(v is None for v in vals):
        return None
    prior = statistics.median(vals[4:8])
    if prior <= 0:
        return None
    return round(min(vals[0:4]) / prior, 6)


RESOURCE_WINDOW_BLOCKERS = {"MISSING": "RECENT_RESOURCE_WINDOW_UNAVAILABLE", "IDLE": "NO_RECENT_RUNNING_TIME"}


def model_aws_baseline_cost(current: EpochEvidence, source_region: str, analysis_time: str, forecast_metadata: Mapping[str, Any], aws_registry: Mapping[str, Any], thresholds: Optional[Thresholds] = None, *, observed_node_types: Optional[Sequence[str]] = None) -> Tuple[Optional[float], Dict[str, Any]]:
    if current.config.warehouse_type == "SERVERLESS":
        region_prices = dict((aws_registry.get("regions") or {}).get(source_region) or {})
        return 0.0, {
            "status": "NOT_CUSTOMER_BILLED",
            "reason": "Serverless SQL runs on Databricks-managed infrastructure; customer AWS EC2 is not modeled as a separate line item.",
            "region": source_region,
            "region_instance_prices": region_prices,
            "registry_region_available": bool(region_prices),
        }
    hourly, topo = aws_cluster_hourly_price(current.config.warehouse_size, source_region, aws_registry)
    if hourly is None:
        return None, topo
    # Billed node types (driver and workers are not distinguished) must belong to the modeled topology for this size.
    billed_nodes = sorted({str(x) for x in (observed_node_types or []) if x})
    if billed_nodes and not set(billed_nodes) <= {topo["driver_instance"], topo["worker_instance"]}:
        return None, {**topo, "status": "UNAVAILABLE", "blocker": "AWS_NODE_TYPE_MISMATCH", "observed_node_types": billed_nodes,
                      "expected_node_types": sorted({topo["driver_instance"], topo["worker_instance"]})}
    topo = {**topo, "node_type_check": "VERIFIED" if billed_nodes else "UNVERIFIED", "observed_node_types": billed_nodes}
    t = thresholds or Thresholds()
    rates, info = annualized_resource_rates(current, forecast_metadata, t, ("cluster_minutes",))
    if rates is None:
        return None, {**topo, "status": "UNAVAILABLE", **info}
    cluster_minutes = info["window_cluster_minutes"]
    if cluster_minutes <= 0:
        return None, {**topo, "status": "UNAVAILABLE", "blocker": "INSUFFICIENT_WAREHOUSE_EVENT_CLUSTER_MINUTES", "cluster_minutes": cluster_minutes, "resource_window_days": info["resource_window_days"]}
    annual_cluster_minutes = rates["cluster_minutes"]
    cost = annual_cluster_minutes / 60.0 * hourly
    cadence = info["model"] == "CADENCE"
    return round(cost, 2), {
        **topo, "status": "MODELED",
        "method": ("cluster-minutes over the DBU forecast's whole billing cycles" if cadence else "cluster-minute run rate on the DBU forecast's window and blend")
                  + " × documented SQL warehouse AWS topology × versioned regional EC2 price registry",
        "observed_cluster_minutes": round(cluster_minutes, 3), "resource_window_start": info["resource_window_start"], "resource_window_end": info["resource_window_end"],
        "resource_window_days": info["resource_window_days"], "trend_adjustment_factor": round(info["trend_adjustment_factor"], 6), "annualized_cluster_minutes": round(annual_cluster_minutes, 3),
        "annualized_cluster_hours": round(annual_cluster_minutes / 60.0, 3),
        **({"cadence_cycles": info["cadence_cycles"]} if cadence else {}),
    }


def settle_reference_time(analysis_time: str, ingestion_cutoff: Optional[str]) -> dt.datetime:
    """The time the settle buffer is measured from: the analysis time, or for a re-collection pinned to an ingestion
    date, that date's 00:00 UTC (earlier of the two), so a later re-collection reproduces the original run's basis."""
    reference = parse_datetime(analysis_time).astimezone(dt.timezone.utc)
    if ingestion_cutoff:
        cutoff = dt.datetime.combine(dt.date.fromisoformat(str(ingestion_cutoff)), dt.time.min, tzinfo=dt.timezone.utc)
        reference = min(reference, cutoff)
    return reference


def billing_check_through(analysis_time: str, account_watermark: Optional[str], ingestion_cutoff: Optional[str], settle_hours: int) -> dt.date:
    """Last UTC day whose billing should already be present: settled, passed by the account watermark and, for a
    re-collection pinned to an ingestion date, before that date. Running after it says nothing about ingestion lag."""
    bound = (settle_reference_time(analysis_time, ingestion_cutoff) - dt.timedelta(hours=settle_hours)).date() - dt.timedelta(days=1)
    acct = parse_datetime(account_watermark) if account_watermark else None
    if acct is not None:
        bound = min(bound, acct.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1))
    if ingestion_cutoff:
        bound = min(bound, dt.date.fromisoformat(str(ingestion_cutoff)) - dt.timedelta(days=1))
    return bound


def effective_billing_watermark(workspace_watermark: Any, account_watermark: Optional[str], resource_daily: Optional[Sequence[Mapping[str, Any]]], *, check_through: dt.date) -> Tuple[Optional[str], str]:
    """The workspace's own billing watermark, unless the warehouse simply stopped running.

    A workspace whose SQL warehouses go idle stops producing usage, so its watermark stalls at the last usage even though
    billing is complete. Warehouse events come from a separate pipeline: if the warehouse did not run after the
    workspace watermark, its later days are complete $0 days and the account-wide watermark applies. If it did run, the
    missing billing is real ingestion lag and the workspace watermark stands. Without the daily series it cannot tell,
    so the workspace watermark stands (fail closed)."""
    ws = parse_datetime(workspace_watermark) if workspace_watermark is not None else None
    acct = parse_datetime(account_watermark) if account_watermark else None
    if acct is None or (ws is not None and ws >= acct):
        return (iso(workspace_watermark) if ws is not None else account_watermark), "WORKSPACE" if ws is not None else "ACCOUNT"
    if resource_daily is None:
        return (iso(workspace_watermark) if ws is not None else None), "WORKSPACE_UNVERIFIED_IDLE"
    # Running counts as lag on days after the watermark's day through the last day that should already be billed, and on
    # the watermark's own day only when it provably ran after the watermark (more running minutes than had elapsed
    # before the watermark time; the daily series cannot place them within the day).
    ws_utc = ws.astimezone(dt.timezone.utc) if ws is not None else None
    ws_day = ws_utc.date().isoformat() if ws_utc is not None else ""
    before_watermark = (ws_utc.hour * 60 + ws_utc.minute + ws_utc.second / 60.0) if ws_utc is not None else 0.0
    last = check_through.isoformat()

    def ran_after(row: Mapping[str, Any]) -> bool:
        day, uptime = str(row.get("usage_day"))[:10], safe_float(row.get("uptime_minutes"))
        if day == ws_day:
            return uptime > before_watermark
        return ws_day < day <= last and uptime > 0

    ran_since = any(ran_after(r) for r in resource_daily)
    if ran_since:
        return (iso(workspace_watermark) if ws is not None else None), "WORKSPACE_LAGGING"
    return account_watermark, "ACCOUNT_IDLE_WAREHOUSE"


def with_ingestion_cutoff(statement: str, cutoff: Optional[str]) -> str:
    """Pin a billing query to records ingested on or before `cutoff` (YYYY-MM-DD), so a re-collection reproduces an
    earlier run after late corrections; without a cutoff the predicate is a no-op."""
    if cutoff is None:
        return statement.replace("__INGESTION_CUTOFF__", "CAST(NULL AS DATE)")
    try:
        day = dt.date.fromisoformat(str(cutoff))
    except ValueError as exc:
        raise SystemExit(f"--billing-ingested-through must be a YYYY-MM-DD date, got {cutoff!r}") from exc
    return statement.replace("__INGESTION_CUTOFF__", f"DATE'{day.isoformat()}'")


def extract_evidence(client: SqlClient, thresholds: Thresholds, source_region: str, aws_registry: Optional[Mapping[str, Any]] = None, compatibility_evidence: Optional[Mapping[str, Any]] = None, *, ingestion_cutoff: Optional[str] = None, analysis_time: Optional[str] = None) -> List[WarehouseEvidence]:
    analysis_time = analysis_time or dt.datetime.now(dt.timezone.utc).isoformat()
    with_ingestion_cutoff("", ingestion_cutoff)  # validate the cutoff before any query runs
    LOGGER.info("collector.start source_region=%s", source_region)
    epoch_rows = client.query(CONFIG_EPOCHS_SQL,label="config_epochs")
    LOGGER.info("collector.discovery current_warehouses=%d epoch_rows=%d", sum(1 for r in epoch_rows if bool(r.get("is_current"))), len(epoch_rows))
    query_idx = _index_epoch_rows(client.query(EPOCH_QUERY_METRICS_SQL,label="query_metrics"))
    for r in client.query(build_spill_source_sql(thresholds),label="spill_sources"):
        if _row_key(r) in query_idx:
            query_idx[_row_key(r)].update({"spill_sources_30": safe_int(r.get("spill_sources_30")), "top_spill_source_share_pct_30": optional_float(r.get("top_spill_source_share_pct_30"))})
    event_idx = _index_epoch_rows(client.query(EPOCH_EVENT_METRICS_SQL,label="warehouse_events"))
    gap_idx = _index_epoch_rows(client.query(build_epoch_gap_metrics_sql(thresholds),label="idle_gaps"))
    running_idle_idx = _index_epoch_rows(client.query(build_running_idle_sql(thresholds),label="running_idle"))
    for r in running_idle_idx.values():
        for k in ("running_idle_window_start", "running_idle_window_end"):
            if r.get(k) is not None:
                r[k] = iso(r[k])
        r["running_idle_by_target"] = {}  # collected: an auto-stop value without a row is a known zero
    for r in client.query(build_running_idle_targets_sql(thresholds),label="running_idle_targets"):
        header = running_idle_idx.get(_row_key(r))
        if header is not None:
            header["running_idle_by_target"][str(safe_int(r.get("target_min")))] = {k: r.get(k) for k in RUNNING_IDLE_TARGET_FIELDS}
    concurrency_idx: Dict[Tuple[str, str, str], Dict[str, Dict[str, float]]] = {}
    for r in client.query(build_concurrency_sql(thresholds),label="concurrency"):
        scope = str(r.get("scope") or "")
        if scope in CONCURRENCY_SCOPES:
            concurrency_idx.setdefault(_row_key(r), {}).setdefault(scope, {})[str(safe_int(r.get("level")))] = safe_float(r.get("weight"))
    resource_daily_idx: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    # The query reports its own collected range (CURRENT_DATE at run time); the analysis clock is only a fallback.
    resource_collected_from: Optional[str] = None
    for r in client.query(RESOURCE_DAILY_SQL,label="resource_daily"):
        day = {k: (str(v)[:10] if k == "usage_day" else v) for k, v in r.items() if k not in {"workspace_id", "warehouse_id", "epoch_start", "collected_from"}}
        resource_daily_idx.setdefault(_row_key(r), []).append(day)
        if r.get("collected_from") is not None:
            resource_collected_from = str(r.get("collected_from"))[:10]
    cost_rows = client.query(with_ingestion_cutoff(EPOCH_DAILY_COST_SQL, ingestion_cutoff),label="billing_daily_cost")
    # Billing completeness is judged per workspace: one workspace's lagging ingestion must not be hidden by another's.
    watermark_by_ws = {str(r.get("workspace_id") or ""): r for r in client.query(with_ingestion_cutoff(BILLING_WATERMARK_SQL, ingestion_cutoff),label="billing_watermark")}
    account_watermark = max((iso(r.get("billing_watermark")) for r in watermark_by_ws.values() if r.get("billing_watermark") is not None), default=None, key=lambda v: parse_datetime(v))
    feature_rows = {_warehouse_key(r): r for r in client.query(with_ingestion_cutoff(BILLING_FEATURE_SQL, ingestion_cutoff),label="billing_features") if r.get("warehouse_id") is not None}
    attribution_idx = _index_epoch_rows(client.query(with_ingestion_cutoff(EPOCH_BILLING_ATTRIBUTION_SQL, ingestion_cutoff),label="billing_attribution_photon_owner"))
    sku_price_rows = client.query(SQL_SKU_PRICE_SQL,label="sql_sku_prices")
    sku_prices_by_account: Dict[str, Dict[str, Any]] = {}
    for r in sku_price_rows:
        account_id = str(r.get("account_id") or "")
        name = str(r.get("sku_name") or "")
        if not name:
            continue
        sku_prices_by_account.setdefault(account_id, {})[name] = {
            "effective_list_price": safe_float(r.get("effective_list_price")),
            "currency_code": str(r.get("currency_code") or "USD"),
            "cloud": str(r.get("cloud") or "AWS"),
            "usage_unit": str(r.get("usage_unit") or "DBU"),
            "classification": classify_sql_sku(name),
        }

    daily_by_wh: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    daily_by_epoch: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in cost_rows:
        wk = _warehouse_key(r)
        daily_by_wh.setdefault(wk, []).append(r)
        daily_by_epoch.setdefault(_row_key(r), []).append(r)

    grouped: Dict[Tuple[str, str], List[EpochEvidence]] = {}
    for r in epoch_rows:
        workspace_id = str(r.get("workspace_id") or "")
        wh = str(r["warehouse_id"])
        wk = (workspace_id, wh)
        start = iso(r.get("epoch_start"))
        if not start:
            continue
        key = (workspace_id, wh, start)
        config = WarehouseConfig(
            warehouse_type=str(r.get("warehouse_type") or "UNKNOWN").upper(),
            warehouse_channel=str(r.get("warehouse_channel") or "UNKNOWN").upper(),
            warehouse_size=normalize_size(r.get("warehouse_size")),
            min_clusters=safe_int(r.get("min_clusters"), 1),
            max_clusters=safe_int(r.get("max_clusters"), 1),
            auto_stop_minutes=safe_int(r.get("auto_stop_minutes"), 0),
        )
        epoch_daily = daily_by_epoch.get(key, [])
        currencies = sorted({str(x.get("currency_code") or "USD") for x in epoch_daily if not _is_unpriced_only_row(x)})
        if len(currencies) > 1:
            raise ValueError(f"Mixed currencies within epoch {key}: {currencies}")
        end = iso(r.get("epoch_end"))
        unpriced_dbu = round(sum(safe_float(x.get("unpriced_dbu_quantity")) for x in epoch_daily), 6)
        epoch_cost = {
            # Unpriced usage leaves the epoch's cost unknown; calibration then skips the epoch instead of underpricing it.
            "list_cost": None if unpriced_dbu > 0 else round(sum(safe_float(x.get("list_cost")) for x in epoch_daily), 2),
            "dbu_quantity": round(sum(safe_float(x.get("dbu_quantity")) for x in epoch_daily), 6),
            "unpriced_dbu_quantity": unpriced_dbu,
            "cost_days": observed_epoch_calendar_days(start, end, analysis_time),
            "active_cost_days": len({str(x.get("usage_date")) for x in epoch_daily}),
            "currency": currencies[0] if currencies else "USD",
        }
        grouped.setdefault(wk, []).append(EpochEvidence(
            epoch_id=epoch_id(workspace_id, wh, start),
            warehouse_id=wh,
            workspace_id=workspace_id,
            account_id=str(r.get("account_id") or ""),
            warehouse_name=str(r.get("warehouse_name") or wh),
            epoch_start=start,
            epoch_end=end,
            is_current=bool(r.get("is_current")),
            config=config,
            tags=normalize_map(r.get("tags")),
            # Concurrency histograms (collected for every epoch in the last 365 days; an empty histogram is a known zero).
            query={**query_idx.get(key, {}), **({"concurrency_histogram": {**{sc: {} for sc in (CONCURRENCY_SCOPES if bool(r.get("is_current")) else ("EPOCH",))},
                                                                         **concurrency_idx.get(key, {})}}
                                                if (end is None or parse_datetime(end) > parse_datetime(analysis_time) - dt.timedelta(days=365)) else {})},
            events=event_idx.get(key, {}),
            # Running-idle evidence (current epoch only) joins the gap facts; absent keys mean not collected.
            gaps={**gap_idx.get(key, {}), **(running_idle_idx.get(key, {}) if bool(r.get("is_current")) else {})},
            cost=epoch_cost,
            # Current epoch only; [] means collected with no activity (a known zero), unlike None (not collected).
            resource_daily=sorted(resource_daily_idx.get(key, []), key=lambda d: d["usage_day"]) if bool(r.get("is_current")) else None,
        ))

    modeler = Modeler(thresholds)
    result: List[WarehouseEvidence] = []
    for wk, epochs in grouped.items():
        workspace_id, wh = wk
        epochs.sort(key=lambda e: e.epoch_start)
        currents = [e for e in epochs if e.is_current]
        if len(currents) != 1:
            raise ValueError(f"Expected one current epoch for {workspace_id}/{wh}, found {len(currents)}")
        current = currents[0]
        current.events["resource_daily_from"] = resource_collected_from or (parse_datetime(analysis_time).astimezone(dt.timezone.utc).date() - dt.timedelta(days=RESOURCE_DAILY_COLLECT_DAYS)).isoformat()
        rows_days = sorted(str(r.get("usage_day"))[:10] for r in (current.resource_daily or []))
        lead_from = max(current.events["resource_daily_from"], current.epoch_start[:10])
        current.events["resource_coverage_from"] = None  # no billed day before the first retained event
        if rows_days and rows_days[0] > lead_from and any(
                lead_from <= str(x.get("usage_date"))[:10] < rows_days[0] and safe_float(x.get("dbu_quantity")) > 0
                for x in daily_by_epoch.get((workspace_id, wh, current.epoch_start), [])):
            # Billed before the warehouse's first retained event: running time is unknown until the first full day
            # the event history covers.
            current.events["resource_coverage_from"] = (dt.date.fromisoformat(rows_days[0]) + dt.timedelta(days=1)).isoformat()
        if current.resource_daily == [] and any(
                str(x.get("usage_date"))[:10] >= current.events["resource_daily_from"] and safe_float(x.get("dbu_quantity")) > 0
                for x in daily_by_epoch.get((workspace_id, wh, current.epoch_start), [])):
            # Billed but no warehouse event in the retained history (running since before it): running time is
            # unknown, not idle.
            current.resource_daily = None
            current.events["resource_daily_status"] = "UNAVAILABLE_BILLED_WITHOUT_RETAINED_EVENTS"
        ws_watermark = watermark_by_ws.get(workspace_id, {})
        effective_watermark, watermark_source = effective_billing_watermark(
            ws_watermark.get("billing_watermark"), account_watermark, current.resource_daily,
            check_through=billing_check_through(analysis_time, account_watermark, ingestion_cutoff, thresholds.billing_settle_hours))
        model = modeler.forecast_from_daily_cost(daily_by_wh.get(wk, []), current.epoch_start, analysis_time, billing_watermark=effective_watermark, ingestion_cutoff=ingestion_cutoff)
        model["billing_watermark_source"] = watermark_source
        model["billing_ingested_through"] = None if ws_watermark.get("billing_ingested_through") is None else str(ws_watermark.get("billing_ingested_through"))[:10]
        model["billing_ingestion_cutoff"] = ingestion_cutoff
        account_sku_prices = sku_prices_by_account.get(current.account_id, {})
        attribution_row = attribution_idx.get((workspace_id, wh, current.epoch_start), {})
        node_types = as_string_list(attribution_row.get("observed_node_types"))
        sql_tiers = as_string_list(attribution_row.get("observed_sql_tiers"))
        aws_cost, aws_model = model_aws_baseline_cost(current, source_region, analysis_time, model, aws_registry or {}, thresholds, observed_node_types=node_types)
        photon_observation = infer_photon_observation(attribution_row, analysis_time, thresholds)
        query_source = derive_query_source_profile(current.query)
        owners = as_string_list(attribution_row.get("observed_owners")) or []
        attribution = WarehouseAttribution(
            owner=clean_owner(attribution_row.get("latest_owner")),
            observed_owners=sorted({o for o in (clean_owner(x) for x in owners) if o is not None}),
            owner_source="system.billing.usage.identity_metadata.owned_by",
            query_source=query_source,
        )
        LOGGER.info(
            "collector.attribution workspace_id=%s warehouse_id=%s owner=%s photon_status=%s photon_coverage_pct=%.2f query_source=%s",
            workspace_id, wh, attribution.owner or "UNKNOWN", photon_observation.status,
            photon_observation.known_feature_coverage_pct, query_source.primary_category,
        )
        result.append(WarehouseEvidence(
            warehouse_id=wh,
            workspace_id=workspace_id,
            account_id=current.account_id,
            warehouse_name=current.warehouse_name,
            source_region=source_region,
            analysis_time=analysis_time,
            current_epoch_id=current.epoch_id,
            epochs=epochs,
            current_cost_365=model["current_cost_365"],
            baseline_forecast_cost_365=model["forecast_cost_365"],
            currency=model["currency"],
            cost_history_days=model["cost_history_days"],
            cost_cv_84=model["cost_cv_84"],
            forecast_metadata={k:v for k,v in model.items() if k not in {"current_cost_365","forecast_cost_365","currency"}},
            daily_cost_365=daily_by_wh.get(wk, []),
            billing_features={**feature_rows.get(wk, {}), **({} if sql_tiers is None else {"current_epoch_sql_tiers": sql_tiers}),
                              **({} if node_types is None else {"current_epoch_node_types": node_types})},
            aws_baseline_cost_365=aws_cost,
            aws_cost_model=aws_model,
            sql_sku_prices=account_sku_prices,
            compatibility_evidence=(dict(compatibility_evidence or {}) if str((compatibility_evidence or {}).get("workspace_id") or "") == workspace_id else {"mode":"A_HITL_FALLBACK","status":"OTHER_WORKSPACE_API_NOT_QUERIED","workspace_id":workspace_id}),
            photon_observation=photon_observation,
            attribution=attribution,
        ))
    LOGGER.info("collector.done warehouses=%d epochs=%d", len(result), sum(len(x.epochs) for x in result))
    return sorted(result, key=lambda e: (e.warehouse_name.lower(), e.workspace_id, e.warehouse_id))


# ---------------------------------------------------------------------------
# Analyzer: facts and evidence only; no config recommendations.
# ---------------------------------------------------------------------------

class Analyzer:
    """Convert normalized warehouse evidence into per-knob facts and findings only."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def _f(self, e: WarehouseEvidence, knob: str, metric: str, value: Any, *, unit: str = "", threshold: Any = None, comparator: str = "", table: str, field: str, window: str, interpretation: str) -> Finding:
        ep = e.current_epoch
        return Finding(
            finding_id=finding_id(e.workspace_id, e.warehouse_id, knob, ep.epoch_id, metric),
            metric=metric, value=value, unit=unit, threshold=threshold, comparator=comparator,
            source_table=table, source_field=field, epoch_id=ep.epoch_id, window=window,
            interpretation=interpretation,
        )

    @staticmethod
    def _auto_stop_options(gaps: Mapping[str, Any], current_auto: int, values: Sequence[int], restart_cost: float, floor_clusters: int,
                           budget: Optional[float]) -> List[Dict[str, Any]]:
        """Score auto-stop values on running-idle evidence (lowering: idle removed minus restarts added; raising:
        restarts saved minus idle added), with each value's added cold-start share against the budget (None: no budget)."""
        by_target = gaps.get("running_idle_by_target") or {}
        queries = optional_float(gaps.get("running_idle_queries"))
        options: List[Dict[str, Any]] = []
        for x in values:
            if x == current_auto:
                continue
            row = by_target.get(str(x)) or {}  # collected, no row: nothing removed, added or avoided at this value
            lower = current_auto == 0 or x < current_auto
            if lower:
                gain = safe_float(row.get("removable_running_cluster_minutes")); starts = safe_int(row.get("additional_starts"))
                loss = starts * restart_cost
            else:
                starts = -safe_int(row.get("avoided_starts"))
                gain = -starts * restart_cost; loss = safe_float(row.get("added_idle_wall_minutes")) * floor_clusters
            share = None if not queries else round(100.0 * starts / queries, 4)
            options.append({"target": x, "direction": "LOWER" if lower else "RAISE", "gain_cluster_minutes": round(gain, 3),
                            "loss_cluster_minutes": round(loss, 3), "net_cluster_minutes": round(gain - loss, 3), "added_starts": starts,
                            "removable_wall_minutes": safe_float(row.get("removable_running_wall_minutes")) if lower else 0.0,
                            "added_cold_start_share_pct": share,
                            "within_cold_start_budget": budget is None or starts <= 0 or (share is not None and share <= budget)})
        return options

    def analyze(self, e: WarehouseEvidence) -> Dict[str, AnalysisResult]:
        ep = e.current_epoch
        q, ev, gaps, cfg = ep.query, ep.events, ep.gaps, ep.config
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
        qpc_all = self.t.queries_per_cluster_pro_classic
        older_peak: Optional[int] = None; recent_peak: Optional[int] = None; held = False
        cur_recent_days = min(30.0, window_days(ep.epoch_start, None, e.analysis_time))
        for x in e.epochs:
            if x.is_current or not isinstance(x.query.get("concurrency_histogram"), Mapping) or (x.epoch_end is not None and parse_datetime(x.epoch_end) <= horizon):
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
        qpc = self.t.queries_per_cluster_pro_classic
        def clusters_for(level: Optional[int]) -> Optional[int]:
            return None if level is None else max(1, math.ceil(level / qpc))
        conc_recent = histogram_percentile((hist or {}).get("RECENT"), self.t.max_cluster_target_percentile)
        conc_epoch = histogram_top_level((hist or {}).get("EPOCH"), self.t.peak_demand_min_minutes)
        # The recent window's peak at the same rate as the whole epoch's, for the seasonal comparison.
        recent_days = min(30.0, window_days(ep.epoch_start, None, e.analysis_time))
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
                       "epoch_peak_outside_recent": (conc_epoch is not None and conc_recent_sustained is not None
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
        # warehouse's observed RUNNING time, over the same period as the event totals). Lowering removes running idle
        # time and adds the restarts of gaps the warehouse used to run through; raising adds idle time and saves the
        # restarts of gaps shorter than the new value. A restart costs restart-cost cluster-minutes per floor cluster.
        cluster_minutes = safe_float(ev.get("cluster_minutes")); current_auto = cfg.auto_stop_minutes
        serverless = cfg.warehouse_type == "SERVERLESS"
        grid = auto_stop_candidates(self.t, cfg.warehouse_type)
        floor_clusters = max(1, cfg.min_clusters)
        restart_cost = (self.t.restart_cost_cluster_minutes_serverless if serverless else self.t.restart_cost_cluster_minutes_pro_classic) * floor_clusters
        nonprod = is_nonprod(ep.tags, self.t)
        budget = None if serverless else (self.t.cold_start_added_share_max_pct_nonprod if nonprod else self.t.cold_start_added_share_max_pct)
        ri_queries = optional_float(gaps.get("running_idle_queries"))
        by_target = gaps.get("running_idle_by_target")
        running_available = isinstance(by_target, Mapping) and all(gaps.get(k) is not None for k in
                                                                   ("running_idle_queries", "observed_running_cluster_minutes", "kept_alive_wall_minutes"))
        options = self._auto_stop_options(gaps, current_auto, grid, restart_cost, floor_clusters, budget) if running_available else []
        kept_alive = optional_float(gaps.get("kept_alive_wall_minutes"))
        window = "current epoch since the first retained warehouse event"
        uptime = optional_float(ev.get("uptime_minutes"))
        w0, w1 = parse_datetime(gaps.get("running_idle_window_start")), parse_datetime(gaps.get("running_idle_window_end"))
        window_minutes = (w1 - w0).total_seconds() / 60.0 if w0 and w1 and w1 > w0 else None
        running_clock_pct = None if not uptime or not window_minutes else round(100.0 * min(uptime, window_minutes) / window_minutes, 3)
        busy_minutes = optional_float(gaps.get("busy_minutes"))
        busy_pct = None if not uptime or busy_minutes is None else round(100.0 * min(busy_minutes, uptime) / uptime, 3)
        kept_alive_pct = None if not uptime or kept_alive is None else round(100.0 * kept_alive / uptime, 3)
        # Billing alone: on how many of the last pressure-window settled days of the current epoch the warehouse was
        # billed (it ran), and what those days cost. Used only when no retained warehouse event gives running intervals.
        billed_recent = billed_days_recent(e, self.t.pressure_window_days)
        positive = [o for o in options if o["net_cluster_minutes"] > 0]
        best_any = max(positive, key=lambda o: (o["net_cluster_minutes"], -o["target"]), default=None)
        best_ok = max((o for o in positive if o["within_cold_start_budget"]), key=lambda o: (o["net_cluster_minutes"], -o["target"]), default=None)
        fs = [
            self._f(e,"auto_stop_minutes","auto_stop_minutes",current_auto,unit="minutes",table="system.compute.warehouses",field="auto_stop_minutes",window="current epoch",interpretation="Current idle termination setting."),
            self._f(e,"auto_stop_minutes","failure_cancel_pct_30",round_or_none(common["failure_pct"],4),unit="%",threshold=self.t.failure_cancel_pct_downsize_safe,comparator="<= cut-safe",table="system.query.history",field="execution_status",window="last 30 days within current epoch",interpretation="Failed or cancelled query share; a high or unknown rate holds every capacity cut."),
            self._f(e,"auto_stop_minutes","cluster_minutes",round(cluster_minutes,2),unit="cluster-minutes",threshold=self.t.min_cluster_minutes_for_cost_model,comparator=">= evidence minimum",table="system.compute.warehouse_events",field="cluster_count/event_time",window=window,interpretation="Running cluster-minutes: the basis idle-time savings are a share of."),
            self._f(e,"auto_stop_minutes","running_idle_queries",None if ri_queries is None else int(ri_queries),unit="queries",table="system.query.history",field="start_time",window=window,interpretation="Queries over the running-idle window; added restarts are judged as a share of them."),
            self._f(e,"auto_stop_minutes","kept_alive_wall_minutes",round_or_none(kept_alive,2),unit="minutes",table="system.query.history + system.compute.warehouse_events",field="end_time, cluster_count",window=window,interpretation="Running time after the current auto-stop should have stopped the warehouse: something other than queries (open sessions, result fetching) kept it running, so no auto-stop value removes it."),
            self._f(e,"auto_stop_minutes","running_share_of_clock_pct",running_clock_pct,unit="%",threshold=self.t.always_on_running_pct,comparator=">= always-on",table="system.compute.warehouse_events",field="cluster_count",window=window,interpretation="Share of the window the warehouse was running."),
            self._f(e,"auto_stop_minutes","busy_share_of_running_pct",busy_pct,unit="%",threshold=self.t.always_on_busy_pct_max,comparator="<= kept up by tiny queries",table="system.query.history",field="start_time/end_time",window=window,interpretation="Share of running time with a query running; a round-the-clock warehouse with a small share is kept up by frequent tiny queries (BI polling, keep-alive, dashboard refresh)."),
            self._f(e,"auto_stop_minutes","restart_cost_cluster_minutes",round(restart_cost,3),unit="cluster-minutes per restart",table="policy",field="restart cost",window="policy",interpretation="Priced cost of one restart (start time x floor clusters); Pro/Classic starting minutes are assumed billed (not documented)."),
            self._f(e,"auto_stop_minutes","nonprod_profile",nonprod,unit="state",table="system.compute.warehouses",field="tags",window="current epoch",interpretation="Nonprod by warehouse tag (environment key with a dev/test value); only effect: the larger cold-start budget."),
            self._f(e,"auto_stop_minutes","auto_stop_values_evaluated",[grid[0],grid[-1],self.t.auto_stop_step_minutes] if grid else None,unit="first, last, step (minutes)",table="policy",field="auto-stop search",window="policy",interpretation="Auto-stop values scored: every step from the type's floor to the maximum; a value covers a gap only when it is the margin longer than the gap."),
            self._f(e,"auto_stop_minutes","auto_stop_best_net_option",best_any,unit="per value",threshold=budget,comparator="added cold-start share <= budget (%)",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,interpretation="The auto-stop value with the largest net saving before the cold-start budget: idle cluster-minutes removed or added, restarts added or avoided, net cluster-minutes saved and the added cold-start share."),
            self._f(e,"auto_stop_minutes","billed_days_of_settled_days_recent",None if billed_recent is None else [billed_recent["billed_days"],billed_recent["settled_days"]],unit="billed days, settled days",table="system.billing.usage",field="usage_date/usage_quantity",window=f"last {self.t.pressure_window_days} settled days within current epoch",interpretation="Days the warehouse was billed (ran) among the settled days; billed every day with no query and no retained warehouse event is a review item."),
            self._f(e,"auto_stop_minutes","auto_stop_best_option_within_budget",best_ok,unit="per value",threshold=budget,comparator="added cold-start share <= budget (%)",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,interpretation="The auto-stop value with the largest net saving among those within the cold-start budget."),
        ]
        out["auto_stop_minutes"] = AnalysisResult(e.warehouse_id,"auto_stop_minutes",ep.epoch_id,fs,{**common,"running_available":running_available,"options":options,"grid":grid,
            "cluster_minutes":cluster_minutes,"running_idle_queries":ri_queries,"kept_alive_wall_minutes":kept_alive,"floor_clusters":floor_clusters,
            "restart_cost_cluster_minutes":restart_cost,"cold_start_budget_pct":budget,"nonprod":nonprod,
            "kept_alive_pct":kept_alive_pct,"running_clock_pct":running_clock_pct,"busy_pct":busy_pct,"client_applications":as_string_list(q.get("client_applications_30")) or [],
            "billed_recent":billed_recent})

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
        target_budget = None if target_type == "SERVERLESS" else (self.t.cold_start_added_share_max_pct_nonprod if nonprod else self.t.cold_start_added_share_max_pct)
        observed_running = optional_float(gaps.get("observed_running_cluster_minutes"))
        current_restarts = optional_float(gaps.get("restarts_for_queries"))
        target_option: Optional[Dict[str, Any]] = None
        if running_available and observed_running and current_restarts is not None:
            def target_running(o: Mapping[str, Any]) -> float:
                if o["direction"] == "LOWER":
                    idle_delta = -o["gain_cluster_minutes"]
                elif o["direction"] == "RAISE":
                    idle_delta = o["loss_cluster_minutes"]
                else:
                    idle_delta = 0.0
                restarts = current_restarts + o["added_starts"]  # added_starts is negative for restarts a raise avoids
                return observed_running + idle_delta + restarts * per_start_target
            # Restart costs are applied in target_running, so the options are scored on idle time only here.
            options_t = self._auto_stop_options(gaps, current_auto, target_values, 0.0, floor_clusters, target_budget)
            keep = {"target": current_auto, "direction": "KEEP", "gain_cluster_minutes": 0.0, "loss_cluster_minutes": 0.0, "added_starts": 0}
            current_valid = current_auto == 0 or current_auto >= target_values[0]
            # Only values within the target's cold-start budget (a raise never adds restarts, so it always qualifies).
            pool = [o for o in options_t if o["within_cold_start_budget"]] + ([keep] if current_valid else [])
            if pool:
                best = min(pool, key=lambda o: (target_running(o), o["target"]))
                target_option = {**best, "target_running_cluster_minutes": round(target_running(best), 3)}
        # Measured running time excludes starting time (restarts are priced on top, as for auto-stop), so today's basis
        # carries today's restarts at today's cost and the target's carries its own.
        running_fraction = (None if target_option is None
                            else round(clamp(target_option["target_running_cluster_minutes"] / (observed_running + current_restarts * per_start_current), 0.0, self.t.cost_factor_ceiling), 6))
        fs.append(self._f(e,"warehouse_type","migration_target_auto_stop",None if target_option is None else target_option["target"],unit="minutes",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,
            interpretation=f"Auto-stop the {target_type.title()} alternative is priced at: its own cost-minimizing value on the measured idle gaps."))
        fs.append(self._f(e,"warehouse_type","migration_target_running_fraction",running_fraction,unit="ratio",table="system.query.history + system.compute.warehouse_events",field="running-idle per auto-stop value",window=window,
            interpretation="The alternative's running cluster-minutes as a share of today's: its own auto-stop removes or adds idle time, and its restarts cost its own start time."))
        out["warehouse_type"] = AnalysisResult(e.warehouse_id,"warehouse_type",ep.epoch_id,fs,{**common,"warehouse_type":cfg.warehouse_type,
            "migration_target_option":target_option,"migration_running_fraction":running_fraction,"busy_share":busy_share,"serverless_observed":serverless_observed,"pro_classic_observed":pro_classic_observed,"statement_text_visible_pct_30":safe_float(q.get("statement_text_visible_pct_30")),"hive_metastore_reference_queries_30":hive_reference_count(q)})
        return out

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
        age = epoch_age_days(e.current_epoch, e.analysis_time)
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
            if age < self.t.current_config_stability_days: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
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
            if age < self.t.current_config_stability_days: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
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
            elif f["queue_n"] < self.t.severity_min_affected_queries or age < self.t.current_config_stability_days or f["queries_30"] < self.t.min_query_sample_size:
                # Like every other change: a mature configuration epoch, a query sample and enough queued queries.
                gates=[code for code,hit in (
                    ("CURRENT_CONFIG_EPOCH_TOO_NEW",age < self.t.current_config_stability_days),
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
            base_eligible = cut_shape and age >= self.t.current_config_stability_days
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
                    f"The current configuration is too new ({age} of {self.t.current_config_stability_days} days) to cut the ceiling; it is re-evaluated once the configuration has run for the stability period."))
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
            if age < self.t.current_config_stability_days: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
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
            if (billed and f["queries_30"]==0 and age>=self.t.current_config_stability_days
                    and billed["settled_days"]>=self.t.current_config_stability_days and billed["billed_days"]==billed["settled_days"]
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
        elif age < self.t.current_config_stability_days:
            auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["CURRENT_CONFIG_EPOCH_TOO_NEW"],"The current configuration is too new to measure its idle gaps; auto-stop is re-evaluated once the epoch matures.")
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
                held=dataclasses.replace(d,status="NO_CHANGE",recommended_config=dict(d.current_config),blockers=sorted(set(d.blockers)|{gate["reason_code"]}),
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
                if x > cfg.max_clusters: raise ValueError("min_clusters exceeds current max_clusters")
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
            for i,st in enumerate(sc.steps):
                if any(st.knob in later.invalidates for later in sc.steps[i+1:]):
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
            group=1 if base is None else 0 if base>0 else 2
            value=0.0 if base is None else round(base*safe_float(c.confidence.get("score"),0.0)/risk,2)
            return (group,-value,PLAN_KNOB_RANK[c.knob])
        remaining=sorted(eligible,key=key); ordered:List[CandidateRecommendation]=[]
        while remaining:
            nxt=next((c for c in remaining if not any(c.knob in x.invalidates for x in remaining if x is not c)),None)
            if nxt is None:
                raise ValueError("Plan steps invalidate each other in a cycle: "+", ".join(c.knob for c in remaining))
            ordered.append(nxt); remaining.remove(nxt)
        # A step another plan step invalidates is always pending re-evaluation (its invalidator precedes it), so ready
        # steps come first: what can be applied now is listed before what waits, and the pending tail prices the
        # contingent upside on its own.
        invalidated={k for c in ordered for k in c.invalidates}
        return [c for c in ordered if c.knob not in invalidated]+[c for c in ordered if c.knob in invalidated]

    def _gate(self,c:CandidateRecommendation)->str:
        if c.knob=="warehouse_size" and c.status=="TEST_REQUIRED": return "A_B_CANARY_LATENCY_COST_ERROR"
        if c.knob=="min_clusters" and c.status=="TEST_REQUIRED": return "A_B_CANARY_QUEUE_LATENCY_COST"
        if c.knob=="warehouse_type": return "A_B_CANARY_COST_PERFORMANCE_COMPATIBILITY"
        if c.knob=="auto_stop_minutes" and c.status=="TEST_REQUIRED": return "A_B_CANARY_BILLED_COST"
        return "HITL_APPROVAL_AND_POST_CHANGE_VALIDATION"

    def _compatibility(self,e:WarehouseEvidence,target_type:str)->Dict[str,Any]:
        q=e.current_epoch.query; checks=[]
        target=target_type.upper(); api=dict(e.compatibility_evidence or {})
        if target=="SERVERLESS":
            region=(e.source_region or "").split(",")[0]
            observed=bool(e.billing_features.get("workspace_serverless_observed_from_system_compute_warehouses"))
            # No verified per-region Serverless availability list is used, so a region label proves nothing; a Serverless
            # warehouse already running in this workspace does.
            checks.append({"check":"aws_region_serverless_availability","status":"PASS" if observed else "UNKNOWN","evidence":{"region":region or None,"existing_serverless_warehouse_in_workspace":observed,"policy":"No verified per-region Serverless availability list; confirm the region supports Serverless SQL unless the workspace already runs a Serverless warehouse."},"source":"system.compute.warehouses"})
            checks.append({"check":"workspace_serverless_observed","status":"PASS" if observed else "UNKNOWN","evidence":observed,"source":"system.compute.warehouses"})
            if api.get("mode")=="B_READ_ONLY_API" and api.get("status")=="AVAILABLE":
                enabled=api.get("enable_serverless_compute")
                checks.append({"check":"workspace_serverless_enablement","status":"PASS" if enabled is True else "FAIL" if enabled is False else "UNKNOWN","evidence":enabled,"source":api.get("source")})
                external=api.get("external_legacy_hive_metastore_detected")
                checks.append({"check":"external_legacy_hive_metastore_workspace_setting","status":"FAIL" if external is True else "PASS" if external is False else "UNKNOWN","evidence":{"detected":external,"config_keys":api.get("external_legacy_hive_metastore_config_keys",[])},"source":api.get("source")})
                checks.append({"check":"s3_access_policy_compatibility","status":"PASS_BY_EXISTING_SERVERLESS" if observed else "UNKNOWN","evidence":api.get("s3_access_policy_status"),"source":api.get("source")})
            else:
                checks.append({"check":"workspace_serverless_enablement","status":"PASS_BY_EXISTING_SERVERLESS" if observed else "UNKNOWN","evidence":api,"source":"fallback A"})
                checks.append({"check":"external_legacy_hive_metastore_workspace_setting","status":"UNKNOWN","evidence":"Read-only compatibility API unavailable or workspace not directly connected","source":"fallback A"})
                checks.append({"check":"s3_access_policy_compatibility","status":"PASS_BY_EXISTING_SERVERLESS" if observed else "UNKNOWN","evidence":"No verified read-only endpoint used; HITL unless existing Serverless proves workspace eligibility","source":"fallback A"})
            visible=safe_float(q.get("statement_text_visible_pct_30")); hive=hive_reference_count(q)
            checks.append({"check":"legacy_hive_metastore_sql_reference","status":"REVIEW" if hive else "PASS_WITH_LIMITATION" if visible>0 else "UNKNOWN","evidence":{"visible_pct":visible,"reference_queries":hive},"source":"system.query.history"})
        else:
            observed=bool(e.billing_features.get("workspace_pro_classic_observed_from_system_compute_warehouses"))
            checks.append({"check":"workspace_pro_classic_observed","status":"PASS" if observed else "UNKNOWN","evidence":observed,"source":"system.compute.warehouses"})
            if api.get("mode")=="B_READ_ONLY_API" and api.get("status")=="AVAILABLE":
                types=api.get("enabled_warehouse_types") or {}
                if "PRO" in types:
                    checks.append({"check":"pro_warehouse_type_enabled","status":"PASS" if types.get("PRO") else "FAIL","evidence":types.get("PRO"),"source":api.get("source")})
            region_prices=bool(e.aws_cost_model.get("region_instance_prices"))
            checks.append({"check":"aws_price_registry_region","status":"PASS" if region_prices else "UNKNOWN","evidence":e.source_region})
        statuses={x["status"] for x in checks}
        result="FAIL" if "FAIL" in statuses else "REVIEW_REQUIRED" if ("REVIEW" in statuses or "UNKNOWN" in statuses) else "PASS_WITH_LIMITATIONS" if ("PASS_WITH_LIMITATION" in statuses or "PASS_BY_EXISTING_SERVERLESS" in statuses) else "PASS"
        return {"target_warehouse_type":target,"assessment":result,"evidence_mode":api.get("mode","A_HITL_FALLBACK"),"checks":checks}

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


_CONTRACT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "recommendation-package.schema.json"
EVIDENCE_FORMAT_VERSION = "1.0.0"
_EVIDENCE_SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "evidence.schema.json"
_ENTITY_QUERY_SOURCE_CATEGORIES = {"DASHBOARD", "JOB", "NOTEBOOK", "GENIE", "ALERT", "SQL_EDITOR", "OTHER", "UNKNOWN"}
_CLIENT_QUERY_SOURCE_CATEGORIES = {"POWER_BI", "TABLEAU", "LOOKER", "SQL_EDITOR"}


def _iso_z(value: Any) -> str:
    """Normalize an observed timestamp to an offset-independent UTC ISO-8601 string."""
    parsed = parse_datetime(value)
    if parsed is None:
        raise ValueError(f"Expected ISO-8601 timestamp, got {value!r}")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


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
    q = ep.query
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
        epoch_scope="CURRENT",
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
                observation_window=f.window or "current_epoch",
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

    q = e.current_epoch.query
    query_ok = safe_int(q.get("queries_30")) >= t.min_query_sample_size
    cost_ok = e.cost_history_days >= t.min_cost_history_days
    epoch_ok = epoch_age_days(e.current_epoch, e.analysis_time) >= t.current_config_stability_days
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
    """Time-clearable gates that currently hide the next step; None once both are clear (contract 1.2.0).

    Pure function of the epoch start, the epoch age and the cost-history days. The contract validator embeds the same
    gate lengths, so a policy change to them must travel with a contract version bump.
    """
    if (thresholds.current_config_stability_days, thresholds.min_economics_cost_history_days) != (
        rc.REEVALUATION_EPOCH_MATURITY_DAYS, rc.REEVALUATION_COST_HISTORY_DAYS
    ):
        raise ValueError("Re-evaluation gate thresholds differ from the locked contract constants; bump the contract version")
    codes = []
    if e.cost_history_days < thresholds.min_economics_cost_history_days:
        codes.append("COST_HISTORY_INSUFFICIENT")
    if epoch_age < thresholds.current_config_stability_days:
        codes.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
    if not codes:
        return None
    return rc.ReevaluationWindow(
        status=rc.ReevaluationStatus.WAITING_FOR_EVIDENCE,
        earliest_reevaluation_time=rc.expected_earliest_reevaluation_time(start_time, sorted(codes)),
        blocking_codes=sorted(codes),
        method=(
            f"Later of the active gates: epoch start + {rc.REEVALUATION_EPOCH_MATURITY_DAYS} days (config-epoch maturity) and "
            f"00:00Z of epoch start date + {rc.REEVALUATION_COST_HISTORY_DAYS} days (cost-history floor). "
            "Earliest only: the 24-hour billing settle buffer, billing ingestion lag and query-sample gates can delay it."
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
                f"{recommended.scenario_name} has no step ready yet: the current epoch is too new for a trustworthy decision. "
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
                         + _canary_sequencing_note(recommended),
        reevaluation=reevaluation,
    )


def _canary_sequencing_note(scenario: rc.Scenario) -> str:
    """When approval-only steps and canaries are both ready: approval-only steps go first, canaries start once those
    have run for the stability period, so a canary's cost reading is not mixed with another change's saving."""
    steps = [x for x in scenario.execution_plan if x.step_type == rc.PlanStepType.CANDIDATE and x.knob is not None]
    hitl = [x.knob.value for x in steps if x.execution_state == rc.ExecutionState.READY]
    canary = [x.knob.value for x in steps if x.execution_state == rc.ExecutionState.READY_FOR_CANARY]
    if not hitl or not canary:
        return ""
    return (f" Apply the ready no-canary steps ({', '.join(hitl)}) first; start the {', '.join(canary)} canary after they have run for "
            f"{rc.REEVALUATION_EPOCH_MATURITY_DAYS} days, so the canary's cost reading is not mixed with their savings.")


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
    recommended = _recommended_plan(scenario_contracts, reevaluation)
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


def _is_supported_warehouse(e: WarehouseEvidence) -> bool:
    return e.current_epoch.config.warehouse_type in SUPPORTED_WAREHOUSE_TYPES


def unsupported_warehouse_exclusions(evidence: Sequence[WarehouseEvidence]) -> List[Dict[str, str]]:
    """Supported-type gate: list warehouses that fail closed, one entry each, in deterministic order.

    An unsupported or future type is never routed into Pro/Classic/Serverless algorithms; it is excluded
    per warehouse and recorded in the run manifest, so the rest of the fleet is still evaluated.
    """
    return [
        {
            "workspace_id": e.workspace_id,
            "warehouse_id": e.warehouse_id,
            "warehouse_name": e.warehouse_name,
            "warehouse_type": e.current_epoch.config.warehouse_type,
            "reason_code": "UNSUPPORTED_WAREHOUSE_TYPE",
        }
        for e in sorted(evidence, key=lambda x: (x.workspace_id, x.warehouse_id))
        if not _is_supported_warehouse(e)
    ]


def _billed_dbu_per_cluster_hour(x: WarehouseEvidence, ep: EpochEvidence, t: Thresholds) -> Optional[float]:
    """Billed DBU per running cluster-hour for one configuration epoch, with billing and running time on the same days.

    The current epoch uses its event-covered resource window; an earlier epoch (calibration evidence only) uses its
    whole-epoch event totals and the billing days from its event coverage start. Unknown when billed DBU per day or the
    event coverage start was not collected.
    """
    def rows_for(days: Optional[set], from_day: Optional[str]) -> List[Mapping[str, Any]]:
        return [r for r in x.daily_cost_365 if str(r.get("epoch_start")) == ep.epoch_start
                and (days is None or str(r.get("usage_date"))[:10] in days) and (from_day is None or str(r.get("usage_date"))[:10] >= from_day)]
    if ep.is_current:
        window = resource_window(x, t)
        if window is None:
            return None
        rows = rows_for({(dt.date.fromisoformat(window["start"]) + dt.timedelta(days=i)).isoformat() for i in range(window["days"])}, None)
        cluster_minutes = sum(window["cluster_minutes"])
    else:
        coverage = parse_datetime(ep.events.get("event_coverage_start"))
        if coverage is None:
            return None
        rows = rows_for(None, coverage.astimezone(dt.timezone.utc).date().isoformat())
        cluster_minutes = safe_float(ep.events.get("cluster_minutes"))
    if not rows or any(r.get("dbu_quantity") is None for r in rows):
        return None
    dbu = sum(safe_float(r.get("dbu_quantity")) for r in rows)
    return dbu / (cluster_minutes / 60.0) if dbu > 0 and cluster_minutes > 0 else None


def _prepare_workspace_calibration(evidence: Sequence[WarehouseEvidence], thresholds: Optional[Thresholds] = None) -> None:
    """Attach workspace-level capability/calibration observations derived only from collected system-table evidence."""
    t = thresholds or Thresholds()
    serverless_workspaces = {x.workspace_id for x in evidence if any(ep.config.warehouse_type == "SERVERLESS" for ep in x.epochs)}
    pro_classic_workspaces = {x.workspace_id for x in evidence if any(ep.config.warehouse_type in {"PRO", "CLASSIC"} for ep in x.epochs)}
    pro_rates: Dict[str, Dict[str, List[float]]] = {}
    for x in evidence:
        for ep in x.epochs:
            if ep.config.warehouse_type in {"PRO", "CLASSIC"}:
                rate = _billed_dbu_per_cluster_hour(x, ep, t)
                if rate is not None:
                    pro_rates.setdefault(x.workspace_id, {}).setdefault(ep.config.warehouse_size, []).append(rate)
    rate_registry = {
        ws: {size: round(statistics.median(values), 6) for size, values in sizes.items() if values}
        for ws, sizes in pro_rates.items()
    }
    for x in evidence:
        x.billing_features["workspace_serverless_observed_from_system_compute_warehouses"] = x.workspace_id in serverless_workspaces
        x.billing_features["workspace_pro_classic_observed_from_system_compute_warehouses"] = x.workspace_id in pro_classic_workspaces
        x.billing_features["workspace_pro_dbu_per_cluster_hour_by_size"] = rate_registry.get(x.workspace_id, {})


def optimize_warehouse(e: WarehouseEvidence, thresholds: Thresholds) -> Tuple[List[CandidateRecommendation], List[Scenario], Optional[str]]:
    """Run the deterministic decision pipeline for one normalized, supported warehouse."""
    analyzer = Analyzer(thresholds)
    optimizer = Optimizer(thresholds)
    estimator = Estimator(thresholds)
    reviewer = Reviewer(thresholds)
    planner = ScenarioPlanner(thresholds)
    analyses = analyzer.analyze(e)
    decisions = optimizer.optimize(e, analyses)
    candidates = estimator.candidates(e, analyses, decisions)
    reviewer.review(e, candidates)
    scenarios, recommended = planner.scenarios(e, candidates)
    reviewer.review_scenarios(e, candidates, scenarios, recommended)
    return candidates, scenarios, recommended


def evaluate_warehouses(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[WarehouseOptimization]:
    """Evaluate supported warehouses through both reviewer gates without constructing reporter objects."""
    LOGGER.info("pipeline.start warehouses=%d batch_size=%d", len(evidence), batch_size)
    for excluded in unsupported_warehouse_exclusions(evidence):
        LOGGER.warning(
            "warehouse.excluded workspace_id=%s warehouse_id=%s warehouse_type=%s reason=%s",
            excluded["workspace_id"], excluded["warehouse_id"], excluded["warehouse_type"], excluded["reason_code"],
        )
    supported = [e for e in evidence if _is_supported_warehouse(e)]
    _prepare_workspace_calibration(supported, thresholds)
    ordered = sorted(supported, key=lambda x: (x.workspace_id, x.warehouse_name.lower(), x.warehouse_id))
    results: List[WarehouseOptimization] = []
    batch = max(1, int(batch_size))
    for offset in range(0, len(ordered), batch):
        chunk = ordered[offset:offset + batch]
        LOGGER.info("pipeline.batch.start batch_index=%d start=%d size=%d", offset // batch + 1, offset, len(chunk))
        for e in chunk:
            LOGGER.info(
                "warehouse.start workspace_id=%s warehouse_id=%s name=%s epoch_id=%s",
                e.workspace_id, e.warehouse_id, e.warehouse_name, e.current_epoch_id,
            )
            candidates, scenarios, recommended = optimize_warehouse(e, thresholds)
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


def build_recommendation_packages(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[rc.RecommendationPackage]:
    """Run the deterministic decision pipeline and directly assemble locked public packages."""
    optimizations = evaluate_warehouses(evidence, thresholds, batch_size)
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
    from contracts import workbook_contract as wc
    workbook_path: Optional[Path] = None
    if generate_workbook:
        LOGGER.info("workbook.v1.start packages=%d", len(packages))
        try:
            from reporters.workbook_reporter import write_workbook
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

def _demo_daily(start: dt.date, days: int, total: float, workspace_id: str, warehouse_id: str, epoch_start: str, dbu_total: float) -> List[Dict[str, Any]]:
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
    return [{
        "usage_day": (first + dt.timedelta(days=i)).isoformat(),
        **{k: round(v / days_observed, 6) for k, v in per_day.items()},
        "max_cluster_count": safe_int(ev.get("max_observed_clusters")),
        "starts": 0,
    } for i in range((analysis_day - first).days)]


def _demo_interp(x: float, points: Sequence[Tuple[float, float, int]]) -> Tuple[float, int]:
    """Linear interpolation of (value, count) between (x, value, count) points (core demo evidence only)."""
    for (x0, v0, n0), (x1, v1, n1) in zip(points, points[1:]):
        if x0 <= x <= x1:
            w = (x - x0) / (x1 - x0)
            return round(v0 + (v1 - v0) * w, 3), int(round(n0 + (n1 - n0) * w))
    raise ValueError(f"{x} outside the interpolation points")


def demo_evidence() -> List[WarehouseEvidence]:
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
        gaps={
            "gap_count": 480, "p50_gap_minutes": 8.0, "p90_gap_minutes": 95.0,
            # Running-idle evidence: idle running time at the minimum (2 clusters) and the restarts each value adds.
            "running_idle_window_start": cur_start, "running_idle_window_end": analysis_time, "running_idle_queries": 110000,
            "running_segment_count": 1000, "observed_running_cluster_minutes": 30000.0, "kept_alive_wall_minutes": 0.0, "kept_alive_gap_count": 0,
            "restarts_for_queries": 900,
            # Idle running time at the minimum (2 clusters) removed by each auto-stop value and the restarts it adds:
            # linear between the measured points, nothing at or above the current 120 minutes.
            "running_idle_by_target": {str(t): {"removable_running_wall_minutes": wall, "removable_running_cluster_minutes": 2 * wall, "additional_starts": starts,
                                                "avoided_starts": 0, "added_idle_wall_minutes": 0.0}
                                       for t in range(5, 120, 5)
                                       for wall, starts in [_demo_interp(t, ((5, 13000.0, 300), (10, 12500.0, 210), (15, 11500.0, 170), (20, 10500.0, 150),
                                                                            (30, 9000.0, 120), (45, 6500.0, 90), (60, 5000.0, 70), (90, 2500.0, 40), (120, 0.0, 0)))]},
        },
        cost={"list_cost": 11000.0, "dbu_quantity": 20000.0, "cost_days": 259, "active_cost_days": 259, "currency": "USD"},
    )

    daily = (
        # Billed DBU = cluster-hours x published DBU/hour (MEDIUM 24, LARGE 40); cost = DBU x Pro list price 0.55.
        _demo_daily(dt.date(2025, 10, 1), 106, 2640.0, workspace, wh, old_start, 4800.0)
        + _demo_daily(dt.date(2026, 1, 15), 259, 11000.0, workspace, wh, cur_start, 20000.0)
    )
    model = Modeler(Thresholds()).forecast_from_daily_cost(daily, cur_start, analysis_time, billing_watermark="2026-10-01T06:00:00+00:00")
    forecast = {k: v for k, v in model.items() if k not in {"current_cost_365", "forecast_cost_365", "currency"}}
    registry=load_aws_price_registry(str(Path(__file__).resolve().parent / "pricing" / "aws_ec2_on_demand.json"))
    cur.resource_daily=synthesize_resource_daily(cur,analysis_time)
    aws_cost,aws_model=model_aws_baseline_cost(cur,"us-east-1",analysis_time,forecast,registry)
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
            query_source=derive_query_source_profile(cur.query),
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

# Demo fleet: each warehouse instantiates one golden scenario (docs/proposals/golden-scenarios.md, tag golden_scenario).
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
    {"scenario": "C19", "name": "Product Launch", "ws": 0, "type": "PRO", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 30, "epoch_start": "2026-09-28",
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
    {"scenario": "A01", "name": "Serverless Fast Stop", "ws": 0, "type": "SERVERLESS", "size": "SMALL", "min": 1, "max": 2, "auto_stop": 1,
     "work": [{"days": "weekdays", "start": "08:00", "end": "18:00", "every": 10, "dur": 1.0, "conc": 1, "src": "DASHBOARD"}]},
)


def _demo_unit(*parts: Any) -> float:
    """Deterministic pseudo-random number in [0, 1) from the given parts (no global RNG state)."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2.0 ** 64


def _demo_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _demo_day_matches(rule: str, day: dt.date) -> bool:
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
    for start, end, cfg in epochs:
        if start <= t and (end is None or t < end):
            return cfg
    return epochs[0][2]


def _simulate_demo_warehouse(spec: Mapping[str, Any], index: int, analysis_time: str) -> Dict[str, List[Dict[str, Any]]]:
    """Collector rows (one list per collector query label) for one simulated demo warehouse."""
    analysis = parse_datetime(analysis_time)
    assert analysis is not None
    analysis_day = analysis.date()
    t0 = dt.datetime.combine(analysis_day - dt.timedelta(days=366), dt.time(), dt.timezone.utc)
    now_t = (analysis - t0).total_seconds() / 60.0
    window_365 = now_t - 365 * 1440.0
    window_30 = now_t - 30 * 1440.0

    def at(day: dt.date, minute: float = 0.0) -> float:
        return (dt.datetime.combine(day, dt.time(), dt.timezone.utc) - t0).total_seconds() / 60.0 + minute

    def stamp(t: float) -> str:
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

    rows: Dict[str, List[Dict[str, Any]]] = {k: [] for k in ("config_epochs", "query_metrics", "warehouse_events", "idle_gaps", "running_idle", "running_idle_targets", "concurrency", "resource_daily", "billing_daily_cost", "billing_features", "billing_attribution_photon_owner")}
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
            "failure_cancel_pct_30": spec.get("failure_pct", 0.3) if n30 else None, "result_cache_pct_30": spec.get("cache_pct", 10.0) if n30 else None,
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
                                      Thresholds().scale_up_lag_minutes_serverless if cfg["warehouse_type"] == "SERVERLESS" else Thresholds().scale_up_lag_minutes_pro_classic)
        rows["concurrency"].extend({"workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso, "scope": sc, "level": int(level), "weight": w}
                                   for sc, hist in sorted(conc.items()) for level, w in sorted(hist.items(), key=lambda x: int(x[0])))
        # Running-idle evidence (current epoch): query gaps intersected with the simulated running timeline, over the
        # same period as the event totals; queries from a day before the window seed the busy frontier.
        if ep_end is None and cov is not None:
            th = Thresholds()
            ri = running_idle_metrics([(a, b) for a, b, _ in ep_runs if b > cov - 1440.0], [(a, b, cl) for a, b, cl, _, _ in live if cl > 0],
                                      (cov, hi_t), cfg["auto_stop_minutes"], auto_stop_targets(th), th.running_idle_full_window_fraction, th.auto_stop_bridge_margin_pct,
                                      th.kept_alive_gap_tolerance_minutes, th.kept_alive_stopped_gap_minutes)
            key_cols = {"workspace_id": workspace, "warehouse_id": wh, "epoch_start": start_iso}
            rows["running_idle"].append({**key_cols, "running_idle_window_start": stamp(cov), "running_idle_window_end": stamp(hi_t),
                                         "running_idle_queries": int(round(queries_between(cov, hi_t)[0])),
                                         **{k: v for k, v in ri.items() if k != "running_idle_by_target"}})
            rows["running_idle_targets"].extend({**key_cols, "target_min": int(x), **v} for x, v in ri["running_idle_by_target"].items())
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
    total_dbu = sum(r["dbu_quantity"] for r in rows["billing_daily_cost"])
    photon_dbu = total_dbu if spec.get("photon", "ENABLED") == "ENABLED" else 0.0
    rows["billing_features"].append({
        "workspace_id": workspace, "warehouse_id": wh,
        "photon_usage_pct_365": None if spec.get("photon") == "UNKNOWN" or total_dbu <= 0 else round(100.0 * photon_dbu / total_dbu, 4),
        "serverless_usage_pct_365": None if total_dbu <= 0 else (100.0 if spec["type"] == "SERVERLESS" else 0.0),
        "cloud": "AWS", "sku_names": sorted({DEMO_SKU_BY_TYPE[c["warehouse_type"]] for _, _, c in epochs} | ({DEMO_UNPRICED_SKU} if spec.get("unpriced_from") else set())),
    })
    return rows


_DEMO_ROWS_CACHE: Dict[Tuple[int, str], Dict[str, List[Dict[str, Any]]]] = {}


def _demo_fleet_rows(count: int, analysis_time: str) -> Dict[str, List[Dict[str, Any]]]:
    merged: Dict[str, List[Dict[str, Any]]] = {}
    for i, spec in enumerate(DEMO_FLEET[:count], start=1):
        # The simulation is deterministic, so each warehouse is simulated once per process; callers get copies.
        if (i, analysis_time) not in _DEMO_ROWS_CACHE:
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
    rows = _demo_fleet_rows(count, DEMO_ANALYSIS_TIME)

    class _DemoClient:
        def query(self, statement: str, *, label: str = "collector_sql") -> List[Dict[str, Any]]:
            return [dict(r) for r in rows.get(label, [])]

    registry = load_aws_price_registry(str(Path(__file__).resolve().parent / "pricing" / "aws_ec2_on_demand.json"))
    evidence = extract_evidence(_DemoClient(), Thresholds(), "us-east-1", registry, analysis_time=DEMO_ANALYSIS_TIME)  # type: ignore[arg-type]
    for e in evidence:
        e.compatibility_evidence = {"mode": "B_READ_ONLY_API", "status": "AVAILABLE", "workspace_id": e.workspace_id, "enable_serverless_compute": True,
                                    "external_legacy_hive_metastore_detected": False, "external_legacy_hive_metastore_config_keys": [],
                                    "s3_access_policy_status": "UNKNOWN", "enabled_warehouse_types": {"PRO": True, "SERVERLESS": True},
                                    "source": "synthetic compatibility fixture"}
    return evidence


def validate_evidence_payload(payload: Any) -> None:
    """Fail closed unless the payload is an evidence file of exactly EVIDENCE_FORMAT_VERSION that matches the schema.

    The message names the failing path and rule, never the offending value: evidence carries owner emails and tags.
    """
    if not isinstance(payload, Mapping):
        raise ValueError(f"evidence file must be an object with evidence_format_version {EVIDENCE_FORMAT_VERSION}")
    version = payload.get("evidence_format_version")
    if version != EVIDENCE_FORMAT_VERSION:
        raise ValueError(f"unsupported evidence_format_version {version!r}; this publisher reads only {EVIDENCE_FORMAT_VERSION}")
    try:
        import jsonschema  # type: ignore
    except ImportError as exc:
        raise RuntimeError("jsonschema>=4.23 is required to validate evidence files") from exc
    schema = json.loads(_EVIDENCE_SCHEMA_PATH.read_text(encoding="utf-8"))
    error = jsonschema.exceptions.best_match(jsonschema.Draft202012Validator(schema).iter_errors(dict(payload)))
    if error is not None:
        where = "/".join(str(x) for x in error.absolute_path) or "<file>"
        rule = error.message if error.validator == "required" else f"fails '{error.validator}'"
        raise ValueError(f"evidence file invalid at {where}: {rule}")


def save_evidence(path:Path,evidence:Sequence[WarehouseEvidence])->None:
    payload = {"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": PUBLISHER_VERSION, "warehouses": as_serializable(evidence)}
    validate_evidence_payload(payload)
    path.write_text(json.dumps(payload,indent=2,sort_keys=True)+"\n",encoding="utf-8")


def _epoch_from_dict(x:Mapping[str,Any])->EpochEvidence:
    return EpochEvidence(epoch_id=str(x["epoch_id"]),warehouse_id=str(x["warehouse_id"]),workspace_id=str(x.get("workspace_id") or ""),account_id=str(x.get("account_id") or ""),warehouse_name=str(x.get("warehouse_name") or x["warehouse_id"]),
                         epoch_start=str(x["epoch_start"]),epoch_end=x.get("epoch_end"),is_current=bool(x.get("is_current")),config=WarehouseConfig(**x["config"]),tags=dict(x.get("tags") or {}),query=dict(x.get("query") or {}),events=dict(x.get("events") or {}),gaps=dict(x.get("gaps") or {}),cost=dict(x.get("cost") or {}),
                         resource_daily=None if x.get("resource_daily") is None else sorted((dict(r) for r in x["resource_daily"]), key=lambda r: str(r.get("usage_day"))))


def evidence_from_dict(x: Mapping[str, Any]) -> WarehouseEvidence:
    return WarehouseEvidence(
        warehouse_id=str(x["warehouse_id"]),
        workspace_id=str(x.get("workspace_id") or ""),
        account_id=str(x.get("account_id") or ""),
        warehouse_name=str(x.get("warehouse_name") or x["warehouse_id"]),
        source_region=str(x.get("source_region") or "unknown"),
        analysis_time=str(x.get("analysis_time") or ""),
        current_epoch_id=str(x["current_epoch_id"]),
        epochs=[_epoch_from_dict(e) for e in x.get("epochs", [])],
        current_cost_365=None if x.get("current_cost_365") is None else safe_float(x.get("current_cost_365")),
        baseline_forecast_cost_365=None if x.get("baseline_forecast_cost_365") is None else safe_float(x.get("baseline_forecast_cost_365")),
        currency=str(x.get("currency") or "USD"),
        cost_history_days=safe_int(x.get("cost_history_days")),
        cost_cv_84=None if x.get("cost_cv_84") is None else safe_float(x.get("cost_cv_84")),
        forecast_metadata=dict(x.get("forecast_metadata") or {}),
        daily_cost_365=list(x.get("daily_cost_365") or []),
        billing_features=dict(x.get("billing_features") or {}),
        aws_baseline_cost_365=(None if x.get("aws_baseline_cost_365") is None else safe_float(x.get("aws_baseline_cost_365"))),
        aws_cost_model=dict(x.get("aws_cost_model") or {}),
        sql_sku_prices=dict(x.get("sql_sku_prices") or {}),
        compatibility_evidence=dict(x.get("compatibility_evidence") or {}),
        photon_observation=(PhotonObservation(**x["photon_observation"]) if x.get("photon_observation") else None),
        attribution=(WarehouseAttribution(
            owner=clean_owner((x.get("attribution") or {}).get("owner")),
            observed_owners=sorted({o for o in (clean_owner(v) for v in (x.get("attribution") or {}).get("observed_owners") or []) if o is not None}),
            owner_source=str((x.get("attribution") or {}).get("owner_source") or "system.billing.usage.identity_metadata.owned_by"),
            query_source=QuerySourceProfile(**((x.get("attribution") or {}).get("query_source") or {"categories":{},"percentages":{},"primary_category":"UNATTRIBUTED","client_applications":[],"multi_label":True})),
        ) if x.get("attribution") else None),
    )


def load_evidence_files(paths: Sequence[str]) -> List[WarehouseEvidence]:
    """Merge regional evidence with composite workspace/warehouse identity.

    Exact overlaps are de-duplicated. Conflicting observations for the same
    workspace/warehouse/epoch or daily cost key fail loudly rather than being
    averaged or silently preferred. Warehouse IDs may legitimately collide
    across workspaces, so workspace_id is always part of the identity.
    """
    merged: Dict[Tuple[str, str], WarehouseEvidence] = {}

    for filename in paths:
        try:
            payload = json.loads(Path(filename).read_text(encoding="utf-8"))
            validate_evidence_payload(payload)
        except ValueError as exc:
            raise SystemExit(f"{filename}: {exc}") from exc
        for item in payload["warehouses"]:
            e = evidence_from_dict(item)
            key = (e.workspace_id, e.warehouse_id)
            if not e.analysis_time:
                raise SystemExit(f"Evidence is missing analysis_time for {key}")

            if key not in merged:
                merged[key] = e
                continue

            prior = merged[key]
            # Top-level modeled values must describe the same observation cut.
            comparable = (
                prior.analysis_time, prior.current_epoch_id, prior.currency,
                round_or_none(prior.current_cost_365, 6), round_or_none(prior.baseline_forecast_cost_365, 6),
                prior.cost_history_days, round_or_none(prior.cost_cv_84, 6), canonical_json(prior.forecast_metadata),
                None if prior.aws_baseline_cost_365 is None else round(prior.aws_baseline_cost_365, 6),
                canonical_json(prior.aws_cost_model), canonical_json(prior.sql_sku_prices),
                canonical_json(prior.photon_observation), canonical_json(prior.attribution),
            )
            incoming = (
                e.analysis_time, e.current_epoch_id, e.currency,
                round_or_none(e.current_cost_365, 6), round_or_none(e.baseline_forecast_cost_365, 6),
                e.cost_history_days, round_or_none(e.cost_cv_84, 6), canonical_json(e.forecast_metadata),
                None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365, 6),
                canonical_json(e.aws_cost_model), canonical_json(e.sql_sku_prices),
                canonical_json(e.photon_observation), canonical_json(e.attribution),
            )
            if comparable != incoming:
                raise SystemExit(f"Conflicting modeled evidence for workspace/warehouse {key}")

            epochs: Dict[str, EpochEvidence] = {x.epoch_id: x for x in prior.epochs}
            for ep in e.epochs:
                if ep.epoch_id in epochs and canonical_json(epochs[ep.epoch_id]) != canonical_json(ep):
                    raise SystemExit(f"Conflicting epoch evidence for {key}/{ep.epoch_id}")
                epochs.setdefault(ep.epoch_id, ep)

            daily: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            for row in prior.daily_cost_365 + e.daily_cost_365:
                currency_key = "UNPRICED" if _is_unpriced_only_row(row) else str(row.get("currency_code") or prior.currency)
                dk = (str(row.get("usage_date")), str(row.get("epoch_start")), currency_key)
                if dk in daily and canonical_json(daily[dk]) != canonical_json(row):
                    raise SystemExit(f"Conflicting daily cost evidence for {key}/{dk}")
                daily[dk] = row

            features = dict(prior.billing_features)
            for fk, fv in e.billing_features.items():
                if fk in features and canonical_json(features[fk]) != canonical_json(fv):
                    raise SystemExit(f"Conflicting billing feature evidence for {key}/{fk}")
                features[fk] = fv

            regions = sorted({x for x in (prior.source_region.split(",") + e.source_region.split(",")) if x})
            merged[key] = WarehouseEvidence(
                warehouse_id=prior.warehouse_id, workspace_id=prior.workspace_id, account_id=prior.account_id or e.account_id,
                warehouse_name=prior.warehouse_name, source_region=",".join(regions), analysis_time=prior.analysis_time,
                current_epoch_id=prior.current_epoch_id, epochs=sorted(epochs.values(), key=lambda x: x.epoch_start),
                current_cost_365=prior.current_cost_365, baseline_forecast_cost_365=prior.baseline_forecast_cost_365,
                currency=prior.currency, cost_history_days=prior.cost_history_days, cost_cv_84=prior.cost_cv_84,
                forecast_metadata=dict(prior.forecast_metadata), daily_cost_365=list(daily.values()), billing_features=features,
                aws_baseline_cost_365=prior.aws_baseline_cost_365, aws_cost_model=dict(prior.aws_cost_model), sql_sku_prices=dict(prior.sql_sku_prices),
                compatibility_evidence=dict(prior.compatibility_evidence or e.compatibility_evidence),
                photon_observation=prior.photon_observation or e.photon_observation,
                attribution=prior.attribution or e.attribution,
            )

    return sorted(merged.values(), key=lambda e: (e.warehouse_name.lower(), e.workspace_id, e.warehouse_id))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def setup_logging(output_dir: Path, level: str = "INFO") -> Path:
    output_dir.mkdir(parents=True,exist_ok=True)
    log_path=output_dir/"publisher.log"
    logger=logging.getLogger("publisher")
    logger.setLevel(getattr(logging,str(level).upper(),logging.INFO))
    logger.handlers.clear()
    fmt=logging.Formatter("%(asctime)sZ %(levelname)s %(message)s",datefmt="%Y-%m-%dT%H:%M:%S")
    sh=logging.StreamHandler(sys.stderr); sh.setFormatter(fmt); logger.addHandler(sh)
    fh=logging.FileHandler(log_path,encoding="utf-8"); fh.setFormatter(fmt); logger.addHandler(fh)
    logger.propagate=False
    logger.info("publisher.start version=%s",PUBLISHER_VERSION)
    return log_path

def parse_args(argv:Optional[Sequence[str]]=None)->argparse.Namespace:
    from contracts import workbook_contract as wc
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
    default_registry=str(Path(__file__).resolve().parent / "pricing" / "aws_ec2_on_demand.json")
    p.add_argument("--aws-price-registry",default=os.getenv("AWS_PRICE_REGISTRY",default_registry))
    p.add_argument("--batch-size",type=int,default=int(os.getenv("WAREHOUSE_BATCH_SIZE","100")),help="Downstream analysis/report batch size; collector SQL is set-based and aggregated in Databricks")
    p.add_argument("--compatibility-api-mode",choices=("auto","off"),default=os.getenv("COMPATIBILITY_API_MODE","auto"),help="auto=B read-only workspace compatibility API when available, else A HITL fallback")
    p.add_argument("--log-level",choices=("DEBUG","INFO","WARNING","ERROR"),default=os.getenv("PUBLISHER_LOG_LEVEL","INFO"))
    p.add_argument("--demo-count",type=int,default=len(DEMO_FLEET),help=f"Synthetic collector warehouse count for --demo (1..{len(DEMO_FLEET)}, one per golden scenario)")
    p.add_argument("--no-workbook",action="store_true",help="Skip workbook.xlsx generation; locked JSON packages are still emitted")
    p.add_argument("--print-json",action="store_true")
    return p.parse_args(argv)


def aws_region_price_guard(evidence: Sequence[WarehouseEvidence], source_region: str, aws_registry: Mapping[str, Any], *, allow_missing: bool) -> Optional[str]:
    """Return a refusal message when supported Pro/Classic warehouses need AWS prices for a region the registry lacks.

    Without the region's instance prices every Pro/Classic AWS, total and package figure would silently be null (or,
    with a wrong label, priced for the wrong region); a live run stops instead unless the operator opts in.
    """
    if allow_missing:
        return None
    regions = sorted((aws_registry.get("regions") or {}).keys())
    if source_region in regions:
        return None
    pro_classic = [e for e in evidence if e.current_epoch.config.warehouse_type in {"PRO", "CLASSIC"}]
    if not pro_classic:
        return None
    return (f"--source-region {source_region or '(empty)'!r} is not in the AWS price registry (regions: {', '.join(regions) or 'none'}); "
            f"{len(pro_classic)} Pro/Classic warehouse(s) need AWS instance prices. Pass the workspace's AWS region, extend the "
            "registry, or pass --allow-missing-aws-price to publish with AWS cost unavailable.")


def _resolve_execution_args(args: argparse.Namespace) -> Tuple[str,str]:
    supplied=[("warehouse",args.serverless_warehouse_id),("warehouse",args.sql_warehouse_id),("cluster",args.all_purpose_cluster_id)]
    supplied=[x for x in supplied if x[1]]
    if args.execution_id:
        if supplied: raise SystemExit("Use either --execution-id/--execution-kind or one convenience execution-ID flag, not both.")
        if not args.execution_kind: raise SystemExit("--execution-kind is required with --execution-id")
        return args.execution_kind,str(args.execution_id)
    if len(supplied)>1: raise SystemExit("Supply exactly one execution target: serverless/sql warehouse ID or all-purpose cluster ID.")
    if supplied: return supplied[0][0],str(supplied[0][1])
    raise SystemExit("Execution target required: --serverless-warehouse-id, --sql-warehouse-id, --all-purpose-cluster-id, or --execution-kind/--execution-id")


def main(argv:Optional[Sequence[str]]=None)->int:
    args=parse_args(argv); t=Thresholds(); out=Path(args.output_dir)
    log_path=setup_logging(out,args.log_level)
    LOGGER.info("policy.versions policy=%s analyzer=%s optimizer=%s estimator=%s bundle=%s",POLICY_VERSION,ANALYZER_VERSION,OPTIMIZER_VERSION,ESTIMATOR_VERSION,BUNDLE_MODEL_VERSION)
    if args.demo:
        if not (1 <= args.demo_count <= len(DEMO_FLEET)):
            raise SystemExit(f"--demo-count must be between 1 and {len(DEMO_FLEET)}")
        LOGGER.info("mode.demo synthetic_collector_warehouses=%d", args.demo_count)
        evidence=synthetic_demo_evidence(args.demo_count)
    elif args.from_evidence:
        LOGGER.info("mode.from_evidence files=%d",len(args.from_evidence))
        evidence=load_evidence_files(args.from_evidence)
    else:
        kind,eid=_resolve_execution_args(args)
        auth=resolve_cli_oauth(args.profile,kind,eid,args.config_file,args.workspace_id)
        compat=collect_workspace_compatibility_evidence(auth,args.compatibility_api_mode)
        LOGGER.info("compatibility.mode=%s status=%s workspace_id=%s",compat.get("mode"),compat.get("status"),compat.get("workspace_id"))
        registry=load_aws_price_registry(args.aws_price_registry)
        with SqlClient(auth) as client:
            evidence=extract_evidence(client,t,args.source_region,registry,compat,ingestion_cutoff=args.billing_ingested_through)
        refusal=aws_region_price_guard(evidence,args.source_region,registry,allow_missing=args.allow_missing_aws_price)
        if refusal:
            raise SystemExit(refusal)
        if args.allow_missing_aws_price and args.source_region not in (registry.get("regions") or {}):
            LOGGER.warning("aws.region_not_in_registry region=%s: Pro/Classic AWS cost is unavailable (--allow-missing-aws-price)",args.source_region)
    packages=build_recommendation_packages(evidence,t,args.batch_size)
    files=package_locked_contract(out,packages,evidence,generate_workbook=not args.no_workbook)
    candidate_count=sum(len(pkg.candidates) for pkg in packages)
    scenario_count=sum(len(pkg.scenarios) for pkg in packages)
    excluded_count=len(unsupported_warehouse_exclusions(evidence))
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
