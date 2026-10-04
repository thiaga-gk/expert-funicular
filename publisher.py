#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse optimizer and publisher.

v2.7 pipeline:
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
POLICY_VERSION = "sql-warehouse-policy-v2.9.0"
ANALYZER_VERSION = "sql-warehouse-analyzer-v2.9.0"
OPTIMIZER_VERSION = "sql-warehouse-optimizer-v2.10.0"
MODEL_VERSION = "epoch-runrate-v2.10.0"
ESTIMATOR_VERSION = "counterfactual-v2.10.0"
BUNDLE_MODEL_VERSION = "stateful-resource-bundle-v2.10.0"
REVIEWER_VERSION = "recommendation-reviewer-v2.9.0"
DECISION_VERSION = "warehouse-decision-graph-v2.9.0"
TECHNIQUE_CATALOG_VERSION = "technique-catalog-v1"
GIB = 1024 ** 3
LOGGER = logging.getLogger("publisher")

SIZE_ORDER = [
    "2X_SMALL", "X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE",
    "2X_LARGE", "3X_LARGE", "4X_LARGE", "5X_LARGE",
]
SIZE_UNITS = {name: 2 ** idx for idx, name in enumerate(SIZE_ORDER)}

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
    "warehouse_channel",
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
    strong_cost_history_days: int = 60
    min_epoch_calibration_days: int = 14
    min_epoch_calibration_queries: int = 100
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
    # Failed/cancelled share of queries; above it, low spill/queue/latency is not trustworthy evidence of headroom.
    failure_cancel_pct_downsize_safe: float = 5.0
    p95_execution_ms_downsize_safe: int = 5_000
    p99_execution_ms_downsize_safe: int = 30_000

    # Min / max clusters
    time_at_min_pct_reduce: float = 25.0
    time_at_max_pct_block_min_reduce: float = 20.0
    time_at_max_pct_safe_reduce: float = 1.0
    # Saturation = real time at the configured ceiling. A peak above max is not evidence (cluster recycling can briefly exceed it).
    time_at_max_pct_saturated: float = 5.0
    # A reduced ceiling must leave growth room above the observed peak: max(clusters, ceil(pct x peak)).
    max_cluster_headroom_clusters: int = 1
    max_cluster_headroom_pct: float = 25.0
    max_clusters_supported_limit: int = 40
    min_clusters_supported_limit: int = 30

    # Auto-stop / cold-start
    cold_start_p95_ms_block: int = 60_000
    cold_starts_per_active_day_block: float = 0.5
    min_idle_gap_sample_size: int = 30
    min_cluster_minutes_for_cost_model: float = 1.0
    auto_stop_savings_cap: float = 0.60
    serverless_auto_stop_target_minutes: int = 10
    low_activity_auto_stop_target_minutes: int = 15
    medium_activity_auto_stop_target_minutes: int = 30
    high_activity_auto_stop_target_minutes: int = 45
    low_activity_queries_per_day: float = 50.0
    medium_activity_queries_per_day: float = 500.0

    # Forecast
    forecast_recent_days: int = 28
    forecast_long_days: int = 84
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
    max_cluster_uplift_fraction_cap: float = 0.50
    max_cluster_base_uplift_weight: float = 0.50
    downsize_runtime_multiplier_conservative: float = 1.55
    downsize_runtime_multiplier_base: float = 1.30
    downsize_runtime_multiplier_optimistic: float = 1.10
    upsize_runtime_multiplier_conservative: float = 1.00
    upsize_runtime_multiplier_base: float = 0.70
    upsize_runtime_multiplier_optimistic: float = 0.50
    epoch_calibration_weight: float = 0.50
    epoch_factor_floor: float = 0.40
    epoch_factor_ceiling: float = 1.60
    cost_factor_floor: float = 0.05
    cost_factor_ceiling: float = 3.00
    cost_normalization_query_count: float = 1000.0
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
    confidence_cap_default: float = 0.80
    remeasure_days_after_invalidating_change: int = 7


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
    x.execution_duration_ms,
    x.waiting_for_compute_duration_ms,
    x.waiting_at_capacity_duration_ms,
    x.spilled_local_bytes,
    x.read_bytes,
    x.read_io_cache_percent,
    x.execution_status,
    x.from_result_cache,
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
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN execution_duration_ms END, 0.95) AS p95_execution_ms_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN execution_duration_ms END, 0.99) AS p99_execution_ms_30,
  MAX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN execution_duration_ms END) AS max_execution_ms_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(waiting_at_capacity_duration_ms,0) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS queued_query_pct_30,
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(spilled_local_bytes,0) > 0)
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS spill_query_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND COALESCE(spilled_local_bytes,0) > 0) AS spill_queries_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND spilled_local_bytes > 0 THEN spilled_local_bytes END,0.95) AS p95_spill_bytes_when_spilled_30,
  SUM(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN COALESCE(spilled_local_bytes,0) ELSE 0 END)/POW(1024,3) AS total_spill_gib_30,
  PERCENTILE_APPROX(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN COALESCE(read_bytes,0) END,0.95) AS p95_read_bytes_30,
  AVG(CASE WHEN start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS THEN read_io_cache_percent END) AS avg_read_io_cache_percent_30,
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
  100.0 * COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND statement_text IS NOT NULL AND statement_text <> '<REDACTED>')
    / NULLIF(COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS),0) AS statement_text_visible_pct_30,
  COUNT_IF(start_time >= CURRENT_TIMESTAMP() - INTERVAL 30 DAYS AND LOWER(COALESCE(statement_text,'')) LIKE '%hive_metastore.%') AS hive_metastore_reference_queries_30,
  PERCENTILE_APPROX(execution_duration_ms,0.95) AS epoch_p95_execution_ms,
  100.0 * COUNT_IF(COALESCE(waiting_at_capacity_duration_ms,0) > 0)/NULLIF(COUNT(*),0) AS epoch_queued_query_pct,
  100.0 * COUNT_IF(COALESCE(spilled_local_bytes,0) > 0)/NULLIF(COUNT(*),0) AS epoch_spill_query_pct
FROM q
GROUP BY workspace_id, warehouse_id, epoch_start
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
  COUNT_IF(event_type='STARTING' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS starts_30,
  COUNT_IF(event_type='SCALED_UP' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS scale_ups_30,
  COUNT_IF(event_type='SCALED_DOWN' AND event_time >= GREATEST(epoch_start,CURRENT_TIMESTAMP()-INTERVAL 30 DAYS)) AS scale_downs_30
FROM durations
GROUP BY workspace_id, warehouse_id, epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)

def build_epoch_gap_metrics_sql(t: Thresholds) -> str:
    """Build the gap collector from centralized auto-stop policy targets."""
    targets = sorted({
        t.serverless_auto_stop_target_minutes,
        t.low_activity_auto_stop_target_minutes,
        t.medium_activity_auto_stop_target_minutes,
        t.high_activity_auto_stop_target_minutes,
    })
    saved = ",\n  ".join(
        f"SUM(GREATEST(LEAST(gap_minutes,CASE WHEN auto_stop_minutes=0 THEN gap_minutes ELSE auto_stop_minutes END)-{target},0)) AS saved_idle_minutes_if_{target}"
        for target in targets
    )
    return f"""
WITH {CONFIG_EPOCHS_CTE},
epochs AS (SELECT workspace_id, warehouse_id, auto_stop_minutes, epoch_start, epoch_end FROM config_epochs),
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
  WHERE x.compute.type='WAREHOUSE' AND x.start_time>=CURRENT_TIMESTAMP()-INTERVAL 365 DAYS
), gaps AS (
  SELECT *, GREATEST(0.0,TIMESTAMPDIFF(SECOND,previous_busy_end_time,start_time)/60.0) AS gap_minutes
  FROM ordered WHERE previous_busy_end_time IS NOT NULL AND start_time>previous_busy_end_time
)
SELECT workspace_id, warehouse_id, epoch_start,
  COUNT(*) AS gap_count,
  PERCENTILE_APPROX(gap_minutes,0.50) AS p50_gap_minutes,
  PERCENTILE_APPROX(gap_minutes,0.90) AS p90_gap_minutes,
  {saved}
FROM gaps GROUP BY workspace_id,warehouse_id,epoch_start
"""

# Usage with no matching list_prices row, or a row without a list price (promotional or not-yet-listed SKU), is kept
# and counted as unpriced DBU:
# an inner join would silently drop it and understate cost. Its cost is unknown (null), never zero. When price rows
# overlap, each usage record takes the latest price_start_time, so it is counted once.
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
    CAST(u.usage_quantity AS DOUBLE)*CAST(p.pricing.effective_list.default AS DOUBLE) AS list_cost
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON p.account_id=u.account_id AND p.sku_name=u.sku_name AND p.cloud=u.cloud
   AND u.usage_end_time>=p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time<p.price_end_time)
  WHERE u.usage_metadata.warehouse_id IS NOT NULL
    AND u.billing_origin_product='SQL'
    AND u.usage_date>=DATE_SUB(CURRENT_DATE(),365) AND u.usage_date<CURRENT_DATE()
  QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY p.price_start_time DESC NULLS LAST, p.currency_code, p.price_end_time) = 1
)
SELECT c.workspace_id,c.warehouse_id,e.epoch_start,c.usage_date,c.currency_code,
  SUM(c.dbu_quantity) AS dbu_quantity, SUM(c.unpriced_dbu_quantity) AS unpriced_dbu_quantity, SUM(c.list_cost) AS list_cost
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
GROUP BY workspace_id, usage_metadata.warehouse_id
"""

# Billing ingestion watermark: the newest usage hour already present in system.billing.usage. Databricks documents
# ORIGINAL records as "typically available within 12 hours" (longer for new workspaces) and gives no bound for
# RETRACTION/RESTATEMENT corrections, so a calendar day is treated as covered only once the watermark has passed its
# end. That is ingestion progress, not finality: corrections can still revise a covered day. Timestamps are UTC.
BILLING_WATERMARK_SQL = r"""
SELECT MAX(usage_end_time) AS billing_watermark
FROM system.billing.usage
WHERE usage_date>=DATE_SUB(CURRENT_DATE(),30) AND billing_origin_product='SQL'
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
    identity_metadata.owned_by AS owned_by
  FROM system.billing.usage
  WHERE usage_metadata.warehouse_id IS NOT NULL
    AND billing_origin_product='SQL'
    AND usage_unit='DBU'
    AND usage_date>=DATE_SUB(CURRENT_DATE(),365) AND usage_date<CURRENT_DATE()
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
  ARRAY_SORT(COLLECT_SET(u.owned_by)) AS observed_owners
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


def normalize_map(value: Any) -> Dict[str, str]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        try:
            obj = json.loads(value)
            if isinstance(obj, Mapping):
                return {str(k): str(v) for k, v in obj.items()}
        except Exception:
            return {"raw": value}
    return {"raw": str(value)}


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


def epoch_id(workspace_id: str, warehouse_id: str, start: str) -> str:
    raw = f"{workspace_id}|{warehouse_id}|{start}"
    return "EPOCH-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def finding_id(workspace_id: str, warehouse_id: str, knob: str, epoch: str, metric: str) -> str:
    raw = f"{ANALYZER_VERSION}|{workspace_id}|{warehouse_id}|{knob}|{epoch}|{metric}"
    return "FND-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


def recommendation_id(workspace_id: str, warehouse_id: str, knob: str, epoch: str, current: Mapping[str, Any], recommended: Mapping[str, Any]) -> str:
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
    raw = canonical_json({"v": DECISION_VERSION, "workspace": workspace_id, "warehouse": warehouse_id, "name": name, "ids": list(recommendation_ids)})
    return "SCN-" + hashlib.sha256(raw.encode()).hexdigest()[:16].upper()


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
    apps = query.get("client_applications_30") or []
    if isinstance(apps, str):
        try:
            parsed = json.loads(apps)
            apps = parsed if isinstance(parsed, list) else [apps]
        except Exception:
            apps = [apps]
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


def size_ratio(current: str, target: str) -> float:
    if current not in SIZE_UNITS or target not in SIZE_UNITS:
        return 1.0
    return SIZE_UNITS[target] / SIZE_UNITS[current]


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


class Modeler:
    """Forward-looking baseline model using zero-filled calendar-day cost history."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def forecast_from_daily_cost(self, rows: Sequence[Dict[str, Any]], current_epoch_start: str, analysis_time: str, billing_watermark: Optional[str] = None) -> Dict[str, Any]:
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

        analysis_date = analysis_dt.astimezone(dt.timezone.utc).date()
        # Half-open window [analysis_date-365, last complete day]. Today is incomplete; yesterday is complete only once
        # the billing watermark has passed its end (records are typically available within 12h, not guaranteed).
        window_start = analysis_date - dt.timedelta(days=365)
        basis_end = analysis_date - dt.timedelta(days=1)
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
        # Unpriced DBU has unknown cost: the trailing actual (and, when inside the forecast basis, the forecast) is
        # unknown rather than understated.
        actual: Optional[float] = None if unpriced_365 > 0 else round(sum(v for d, v in by_date.items() if window_start <= d <= basis_end), 2)
        completeness_meta = {
            "billing_watermark": watermark_dt.astimezone(dt.timezone.utc).isoformat() if watermark_dt else None,
            "billing_completeness": completeness,
            "billing_complete_through": basis_end.isoformat(),
            "excluded_incomplete_days": excluded_incomplete_days,
            "unpriced_dbu_quantity_365": unpriced_365,
            "unpriced_dbu_quantity_in_basis": unpriced_in_basis,
            "dbu_pricing_status": "UNPRICED_USAGE" if unpriced_365 > 0 else "COMPLETE",
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
                "forecast_cost_365": 0.0,
                "currency": currency,
                "cost_history_days": 0,
                "active_cost_days": 0,
                "cost_cv_84": None,
                **completeness_meta,
                "forecast_method": MODEL_VERSION,
                "forecast_basis_start": None,
                "forecast_basis_end": None,
            }

        def mean(x: Sequence[float]) -> float:
            return statistics.mean(x) if x else 0.0

        recent = self.t.forecast_recent_days
        long = self.t.forecast_long_days
        last28 = series[-recent:]
        last84 = series[-long:]
        prev28 = series[-2*recent:-recent] if len(series) >= 2*recent else []
        mean28, mean84 = mean(last28), mean(last84)
        if len(series) >= long:
            daily = self.t.forecast_weight_28_long * mean28 + (1 - self.t.forecast_weight_28_long) * mean84
        elif len(series) >= recent:
            daily = self.t.forecast_weight_28_mid * mean28 + (1 - self.t.forecast_weight_28_mid) * mean(series)
        else:
            daily = mean(series)

        ratio = mean28 / mean(prev28) if prev28 and mean(prev28) > 0 else 1.0
        ratio = clamp(ratio, self.t.trend_ratio_floor, self.t.trend_ratio_ceiling)
        trend_adjustment_factor = 1.0 + self.t.trend_adjustment_weight * (ratio - 1.0)
        daily *= trend_adjustment_factor
        cv = statistics.pstdev(last84) / mean84 if len(last84) >= self.t.forecast_cv_min_days and mean84 > 0 else None
        if unpriced_in_basis > 0:
            # Unpriced days look like $0 days in the cost series: trend, run rates and stability are unknown, and the
            # AWS model gets no trend adjustment instead of a spurious downward one.
            ratio = None
            trend_adjustment_factor = 1.0
            cv = None
        return {
            "current_cost_365": actual,
            "forecast_cost_365": None if unpriced_in_basis > 0 else round(max(0.0, daily * 365), 2),
            "currency": currency,
            "cost_history_days": len(series),
            "active_cost_days": sum(1 for x in series if abs(x) > 1e-12),
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


def model_aws_baseline_cost(current: EpochEvidence, source_region: str, analysis_time: str, forecast_metadata: Mapping[str, Any], aws_registry: Mapping[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
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
    cluster_minutes = safe_float(current.events.get("cluster_minutes"))
    days = observed_epoch_calendar_days(current.epoch_start, current.epoch_end, analysis_time)
    if cluster_minutes <= 0 or days <= 0:
        return None, {**topo, "status": "UNAVAILABLE", "blocker": "INSUFFICIENT_WAREHOUSE_EVENT_CLUSTER_MINUTES", "cluster_minutes": cluster_minutes, "observed_days": days}
    trend = safe_float(forecast_metadata.get("trend_adjustment_factor"), 1.0)
    annual_cluster_minutes = cluster_minutes / days * 365.0 * trend
    cost = annual_cluster_minutes / 60.0 * hourly
    return round(cost, 2), {
        **topo, "status": "MODELED", "method": "current-config-epoch cluster-minute run-rate × documented SQL warehouse AWS topology × versioned regional EC2 price registry",
        "observed_cluster_minutes": round(cluster_minutes, 3), "observed_days": days,
        "trend_adjustment_factor": round(trend, 6), "annualized_cluster_minutes": round(annual_cluster_minutes, 3),
        "annualized_cluster_hours": round(annual_cluster_minutes / 60.0, 3),
    }


def extract_evidence(client: SqlClient, thresholds: Thresholds, source_region: str, aws_registry: Optional[Mapping[str, Any]] = None, compatibility_evidence: Optional[Mapping[str, Any]] = None) -> List[WarehouseEvidence]:
    analysis_time = dt.datetime.now(dt.timezone.utc).isoformat()
    LOGGER.info("collector.start source_region=%s", source_region)
    epoch_rows = client.query(CONFIG_EPOCHS_SQL,label="config_epochs")
    LOGGER.info("collector.discovery current_warehouses=%d epoch_rows=%d", sum(1 for r in epoch_rows if bool(r.get("is_current"))), len(epoch_rows))
    query_idx = _index_epoch_rows(client.query(EPOCH_QUERY_METRICS_SQL,label="query_metrics"))
    event_idx = _index_epoch_rows(client.query(EPOCH_EVENT_METRICS_SQL,label="warehouse_events"))
    gap_idx = _index_epoch_rows(client.query(build_epoch_gap_metrics_sql(thresholds),label="idle_gaps"))
    cost_rows = client.query(EPOCH_DAILY_COST_SQL,label="billing_daily_cost")
    watermark_rows = client.query(BILLING_WATERMARK_SQL,label="billing_watermark")
    billing_watermark = iso(watermark_rows[0].get("billing_watermark")) if watermark_rows else None
    feature_rows = {_warehouse_key(r): r for r in client.query(BILLING_FEATURE_SQL,label="billing_features") if r.get("warehouse_id") is not None}
    attribution_idx = _index_epoch_rows(client.query(EPOCH_BILLING_ATTRIBUTION_SQL,label="billing_attribution_photon_owner"))
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
            query=query_idx.get(key, {}),
            events=event_idx.get(key, {}),
            gaps=gap_idx.get(key, {}),
            cost=epoch_cost,
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
        model = modeler.forecast_from_daily_cost(daily_by_wh.get(wk, []), current.epoch_start, analysis_time, billing_watermark=billing_watermark)
        account_sku_prices = sku_prices_by_account.get(current.account_id, {})
        aws_cost, aws_model = model_aws_baseline_cost(current, source_region, analysis_time, model, aws_registry or {})
        attribution_row = attribution_idx.get((workspace_id, wh, current.epoch_start), {})
        photon_observation = infer_photon_observation(attribution_row, analysis_time, thresholds)
        query_source = derive_query_source_profile(current.query)
        owners = attribution_row.get("observed_owners") or []
        if isinstance(owners, str):
            try:
                parsed = json.loads(owners)
                owners = parsed if isinstance(parsed, list) else [owners]
            except Exception:
                owners = [owners]
        attribution = WarehouseAttribution(
            owner=(str(attribution_row.get("latest_owner")) if attribution_row.get("latest_owner") else None),
            observed_owners=sorted({str(x) for x in owners if x is not None}),
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
            billing_features=feature_rows.get(wk, {}),
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

        # Channel
        fs = [self._f(e,"warehouse_channel","warehouse_channel",cfg.warehouse_channel,table="system.compute.warehouses",field="warehouse_channel",window="current epoch",interpretation="Current release channel from the current configuration epoch.")]
        out["warehouse_channel"] = AnalysisResult(e.warehouse_id,"warehouse_channel",ep.epoch_id,fs,{**common,"channel":cfg.warehouse_channel})

        # Size
        spill_pct = safe_float(q.get("spill_query_pct_30")); spill_p95, spill_n = conditional_severity(q, "spill_queries_30", "p95_spill_bytes_when_spilled_30")
        failure_pct = None if q.get("failure_cancel_pct_30") is None else safe_float(q.get("failure_cancel_pct_30"))
        queue_pct = safe_float(q.get("queued_query_pct_30")); p95 = safe_float(q.get("p95_execution_ms_30")); p99 = safe_float(q.get("p99_execution_ms_30"))
        fs = [
            self._f(e,"warehouse_size","warehouse_size",cfg.warehouse_size,table="system.compute.warehouses",field="warehouse_size",window="current epoch",interpretation="Current warehouse size."),
            self._f(e,"warehouse_size","spill_query_pct_30",round(spill_pct,4),unit="%",threshold=self.t.spill_query_pct_upsize,comparator=">= upsize signal",table="system.query.history",field="spilled_local_bytes",window="last 30 days within current epoch",interpretation="Frequency of queries spilling to local storage."),
            self._f(e,"warehouse_size","spill_queries_30",spill_n,unit="queries",threshold=self.t.severity_min_affected_queries,comparator=">= evidence minimum for upsize",table="system.query.history",field="spilled_local_bytes",window="last 30 days within current epoch",interpretation="Queries that spilled; the spill severity percentile is computed over these queries only."),
            self._f(e,"warehouse_size","p95_spill_bytes_when_spilled_30",round_or_none(spill_p95),unit="bytes",threshold=self.t.spill_p95_bytes_upsize,comparator=">= upsize signal",table="system.query.history",field="spilled_local_bytes",window="last 30 days within current epoch",interpretation="Severity of spill at p95 among queries that spilled."),
            self._f(e,"warehouse_size","queued_query_pct_30",round(queue_pct,4),unit="%",threshold=self.t.queue_query_pct_safe,comparator="<= downsize-safe",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Concurrency pressure during the decision window."),
            self._f(e,"warehouse_size","failure_cancel_pct_30",round_or_none(failure_pct,4),unit="%",threshold=self.t.failure_cancel_pct_downsize_safe,comparator="<= downsize-safe",table="system.query.history",field="execution_status",window="last 30 days within current epoch",interpretation="Failed or cancelled query share; a high rate undermines the no-pressure evidence a downsize relies on."),
            self._f(e,"warehouse_size","p95_execution_ms_30",round(p95),unit="ms",threshold=self.t.p95_execution_ms_downsize_safe,comparator="<= downsize-safe",table="system.query.history",field="execution_duration_ms",window="last 30 days within current epoch",interpretation="Tail execution latency guardrail."),
            self._f(e,"warehouse_size","p99_execution_ms_30",round(p99),unit="ms",threshold=self.t.p99_execution_ms_downsize_safe,comparator="<= downsize-safe",table="system.query.history",field="execution_duration_ms",window="last 30 days within current epoch",interpretation="Extreme tail execution latency guardrail."),
            self._f(e,"warehouse_size","avg_read_io_cache_percent_30",round(safe_float(q.get("avg_read_io_cache_percent_30")),4),unit="%",table="system.query.history",field="read_io_cache_percent",window="last 30 days within current epoch",interpretation="I/O cache evidence for HITL context; not a v2 size decision threshold."),
        ]
        out["warehouse_size"] = AnalysisResult(e.warehouse_id,"warehouse_size",ep.epoch_id,fs,{**common,"spill_pct":spill_pct,"spill_p95":spill_p95,"spill_n":spill_n,"queue_pct":queue_pct,"failure_pct":failure_pct,"p95_exec":p95,"p99_exec":p99})

        # Max clusters
        wait, queue_n = conditional_severity(q, "queued_queries_30", "p95_wait_capacity_ms_when_queued_30"); max_obs = safe_int(ev.get("max_observed_clusters")); at_max = safe_float(ev.get("time_at_max_pct"))
        fs = [
            self._f(e,"max_clusters","max_clusters",cfg.max_clusters,table="system.compute.warehouses",field="max_clusters",window="current epoch",interpretation="Configured concurrency ceiling."),
            self._f(e,"max_clusters","max_observed_clusters",max_obs,unit="clusters",table="system.compute.warehouse_events",field="cluster_count",window="current epoch",interpretation="Highest observed scale level."),
            self._f(e,"max_clusters","time_at_max_pct",round(at_max,4),unit="%",threshold=self.t.time_at_max_pct_safe_reduce,comparator="<= reduce-safe",table="system.compute.warehouse_events",field="cluster_count",window="current epoch",interpretation="Fraction of running time at configured maximum."),
            self._f(e,"max_clusters","queued_query_pct_30",round(queue_pct,4),unit="%",threshold=self.t.queue_query_pct_increase_max,comparator=">= increase signal",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Frequency of capacity queueing."),
            self._f(e,"max_clusters","queued_queries_30",queue_n,unit="queries",threshold=self.t.severity_min_affected_queries,comparator=">= evidence minimum for increase",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Queries that queued at capacity; the wait percentile is computed over these queries only."),
            self._f(e,"max_clusters","p95_wait_capacity_ms_when_queued_30",round_or_none(wait),unit="ms",threshold=self.t.queue_wait_p95_ms_increase_max,comparator=">= increase signal",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Severity of capacity queueing at p95 among queued queries."),
        ]
        out["max_clusters"] = AnalysisResult(e.warehouse_id,"max_clusters",ep.epoch_id,fs,{**common,"queue_pct":queue_pct,"queue_n":queue_n,"wait_capacity":wait,"max_observed":max_obs,"at_max_pct":at_max,"minutes_at_max":safe_float(ev.get("minutes_at_max")),"cluster_minutes":safe_float(ev.get("cluster_minutes"))})

        # Min clusters
        at_min = safe_float(ev.get("time_at_min_pct"))
        fs = [
            self._f(e,"min_clusters","min_clusters",cfg.min_clusters,table="system.compute.warehouses",field="min_clusters",window="current epoch",interpretation="Configured minimum scale."),
            self._f(e,"min_clusters","time_at_min_pct",round(at_min,4),unit="%",threshold=self.t.time_at_min_pct_reduce,comparator=">= reduction signal",table="system.compute.warehouse_events",field="cluster_count",window="current epoch",interpretation="How often the warehouse sits at its minimum."),
            self._f(e,"min_clusters","queued_query_pct_30",round(queue_pct,4),unit="%",threshold=self.t.queue_query_pct_safe,comparator="<= safe",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Queue guardrail before reducing baseline capacity."),
            self._f(e,"min_clusters","p95_wait_capacity_ms_when_queued_30",round_or_none(wait),unit="ms",threshold=self.t.queue_wait_p95_ms_safe,comparator="<= safe",table="system.query.history",field="waiting_at_capacity_duration_ms",window="last 30 days within current epoch",interpretation="Queue latency guardrail among queued queries."),
            self._f(e,"min_clusters","time_at_max_pct",round(at_max,4),unit="%",threshold=self.t.time_at_max_pct_block_min_reduce,comparator=">= blocker",table="system.compute.warehouse_events",field="cluster_count",window="current epoch",interpretation="Sustained scale pressure blocks minimum reduction."),
        ]
        out["min_clusters"] = AnalysisResult(e.warehouse_id,"min_clusters",ep.epoch_id,fs,{**common,"queue_pct":queue_pct,"wait_capacity":wait,"at_min_pct":at_min,"at_max_pct":at_max,"minutes_at_min":safe_float(ev.get("minutes_at_min")),"cluster_minutes":safe_float(ev.get("cluster_minutes"))})

        # Auto stop
        target = (
            self.t.serverless_auto_stop_target_minutes if cfg.warehouse_type=="SERVERLESS"
            else self.t.low_activity_auto_stop_target_minutes if per_day < self.t.low_activity_queries_per_day
            else self.t.medium_activity_auto_stop_target_minutes if per_day < self.t.medium_activity_queries_per_day
            else self.t.high_activity_auto_stop_target_minutes
        )
        saved_idle = safe_float(gaps.get(f"saved_idle_minutes_if_{target}")); uptime = safe_float(ev.get("uptime_minutes")); wait_compute, wait_compute_n = conditional_severity(q, "compute_wait_queries_30", "p95_wait_compute_ms_when_waited_30")
        gap_count=safe_int(gaps.get("gap_count")); cluster_minutes=safe_float(ev.get("cluster_minutes"))
        fs = [
            self._f(e,"auto_stop_minutes","auto_stop_minutes",cfg.auto_stop_minutes,unit="minutes",threshold=target,comparator="> target",table="system.compute.warehouses",field="auto_stop_minutes",window="current epoch",interpretation="Current idle termination setting versus deterministic policy target."),
            self._f(e,"auto_stop_minutes","queries_per_active_day_30",round(per_day,2),unit="queries/day",table="system.query.history",field="start_time",window="last 30 days within current epoch",interpretation="Activity density selects the auto-stop target."),
            self._f(e,"auto_stop_minutes","idle_gap_count",gap_count,unit="gaps",threshold=self.t.min_idle_gap_sample_size,comparator=">= evidence minimum",table="system.query.history",field="start_time/end_time",window="current epoch",interpretation="Non-overlapping inter-query gap sample supporting the auto-stop counterfactual."),
            self._f(e,"auto_stop_minutes","cluster_minutes",round(cluster_minutes,2),unit="cluster-minutes",threshold=self.t.min_cluster_minutes_for_cost_model,comparator=">= evidence minimum",table="system.compute.warehouse_events",field="cluster_count/event_time",window="current epoch",interpretation="Observed resource-time basis used to convert idle time into cost effect."),
            self._f(e,"auto_stop_minutes","compute_wait_queries_30",wait_compute_n,unit="queries",table="system.query.history",field="waiting_for_compute_duration_ms",window="last 30 days within current epoch",interpretation="Queries that waited for compute; the wait percentile is computed over these queries only."),
            self._f(e,"auto_stop_minutes","p95_wait_compute_ms_when_waited_30",round_or_none(wait_compute),unit="ms",threshold=self.t.cold_start_p95_ms_block,comparator=">= cold-start blocker",table="system.query.history",field="waiting_for_compute_duration_ms",window="last 30 days within current epoch",interpretation="Cold-start sensitivity guardrail at p95 among queries that waited for compute."),
            self._f(e,"auto_stop_minutes","starts_per_active_day_30",round(starts_per_day,4),unit="starts/day",threshold=self.t.cold_starts_per_active_day_block,comparator=">= cold-start blocker",table="system.compute.warehouse_events",field="event_type=STARTING",window="last 30 days within current epoch",interpretation="Restart frequency guardrail."),
            self._f(e,"auto_stop_minutes","estimated_additional_off_minutes",round(saved_idle,2),unit="minutes",table="system.query.history",field="inter-query gap",window="current epoch",interpretation="Estimated additional off-time at the candidate target."),
        ]
        out["auto_stop_minutes"] = AnalysisResult(e.warehouse_id,"auto_stop_minutes",ep.epoch_id,fs,{**common,"target":target,"saved_idle":saved_idle,"gap_count":gap_count,"uptime":uptime,"cluster_minutes":cluster_minutes,"current_min_clusters":cfg.min_clusters,"wait_compute":wait_compute,"wait_compute_n":wait_compute_n,"starts_per_day":starts_per_day})

        # Type/serverless
        serverless_observed = bool(e.billing_features.get("workspace_serverless_observed_from_system_compute_warehouses")) or safe_float(e.billing_features.get("serverless_usage_pct_365")) > 0
        pro_classic_observed = bool(e.billing_features.get("workspace_pro_classic_observed_from_system_compute_warehouses"))
        fs = [
            self._f(e,"warehouse_type","warehouse_type",cfg.warehouse_type,table="system.compute.warehouses",field="warehouse_type",window="current epoch",interpretation="Current warehouse type."),
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
            self._f(e,"warehouse_type","hive_metastore_reference_queries_30",safe_int(q.get("hive_metastore_reference_queries_30")),unit="queries",table="system.query.history",field="statement_text (aggregated pattern only)",window="last 30 days within current epoch",interpretation="Potential migration-review signal only; this does not prove an external legacy Hive metastore is configured."),
        ]
        out["warehouse_type"] = AnalysisResult(e.warehouse_id,"warehouse_type",ep.epoch_id,fs,{**common,"warehouse_type":cfg.warehouse_type,"serverless_observed":serverless_observed,"pro_classic_observed":pro_classic_observed,"statement_text_visible_pct_30":safe_float(q.get("statement_text_visible_pct_30")),"hive_metastore_reference_queries_30":safe_int(q.get("hive_metastore_reference_queries_30"))})
        return out

# ---------------------------------------------------------------------------
# Optimizer: config decisions only. No cost arithmetic.
# ---------------------------------------------------------------------------

class Optimizer:
    """Apply deterministic policy thresholds to analyzer facts and propose config decisions."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def optimize(self, e: WarehouseEvidence, analyses: Dict[str, AnalysisResult]) -> List[OptimizationDecision]:
        cfg = e.current_epoch.config
        age = epoch_age_days(e.current_epoch, e.analysis_time)
        out: List[OptimizationDecision] = []

        # Channel first: channel changes can invalidate all tuning assumptions.
        a = analyses["warehouse_channel"]
        if cfg.warehouse_channel == "PREVIEW":
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_channel","STABILITY","REVIEW",
                {"warehouse_channel":cfg.warehouse_channel},{"warehouse_channel":"CURRENT"},
                ["PREVIEW_FEATURE_DEPENDENCY_UNKNOWN"],[],["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
                ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
                "Move to CURRENT only after confirming no workload depends on Preview-only behavior."))
        else:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_channel","STABILITY","NO_CHANGE",
                {"warehouse_channel":cfg.warehouse_channel},{"warehouse_channel":cfg.warehouse_channel},[],[],[],[],
                "No channel change indicated."))

        # Size
        a = analyses["warehouse_size"]; f=a.facts
        blockers: List[str] = []
        if cfg.warehouse_size not in SIZE_ORDER:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","COST_AND_PERFORMANCE","BLOCKED",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["UNKNOWN_WAREHOUSE_SIZE"],
                ["warehouse_channel"],[],[],"Warehouse size is outside the known deterministic ordering."))
        elif f["spill_pct"] >= self.t.spill_query_pct_upsize and f["spill_p95"] is None:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["SPILL_SEVERITY_EVIDENCE_UNAVAILABLE"],["warehouse_channel"],[],[],
                "Spill frequency crosses the upsize threshold, but spill severity was not reported (older collector evidence); no resize is recommended."))
        elif f["spill_pct"] >= self.t.spill_query_pct_upsize and f["spill_p95"] >= self.t.spill_p95_bytes_upsize:
            target = next_size(cfg.warehouse_size)
            # Spill frequency/severity are only trustworthy with a minimum sample from a mature config epoch.
            if age < self.t.current_config_stability_days: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
            if f["queries_30"] < self.t.min_query_sample_size: blockers.append("INSUFFICIENT_QUERY_SAMPLE")
            if f["spill_n"] < self.t.severity_min_affected_queries: blockers.append("INSUFFICIENT_AFFECTED_QUERY_SAMPLE")
            if target and blockers:
                out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                    {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},blockers,["warehouse_channel"],[],[],
                    "Spill crosses the upsize thresholds, but the query sample, spilled-query count or config epoch is too small/new to act on; no resize is recommended."))
            elif target:
                out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","ACTIONABLE",
                    {"warehouse_size":cfg.warehouse_size},{"warehouse_size":target},[],["warehouse_channel"],
                    ["max_clusters","min_clusters","auto_stop_minutes"],["max_clusters","min_clusters","auto_stop_minutes"],
                    "Spill frequency and severity both cross the deterministic upsize thresholds."))
            else:
                out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","PERFORMANCE","BLOCKED",
                    {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},["ALREADY_AT_MAX_KNOWN_SIZE"],
                    ["warehouse_channel"],[],[],"Spill pressure exists but the warehouse is already at the largest known size."))
        else:
            target = previous_size(cfg.warehouse_size)
            if age < self.t.current_config_stability_days: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
            if f["queries_30"] < self.t.strong_query_sample_size: blockers.append("INSUFFICIENT_STRONG_QUERY_SAMPLE")
            if f["spill_pct"] > self.t.spill_query_pct_downsize_safe: blockers.append("SPILL_PRESENT")
            if f["queue_pct"] > self.t.queue_query_pct_safe: blockers.append("QUEUE_PRESSURE_PRESENT")
            if f["failure_pct"] is None: blockers.append("FAILURE_CANCEL_EVIDENCE_UNAVAILABLE")
            elif f["failure_pct"] > self.t.failure_cancel_pct_downsize_safe: blockers.append("FAILURE_CANCEL_RATE_HIGH")
            if f["p95_exec"] > self.t.p95_execution_ms_downsize_safe: blockers.append("P95_LATENCY_TOO_HIGH")
            if f["p99_exec"] > self.t.p99_execution_ms_downsize_safe: blockers.append("P99_LATENCY_TOO_HIGH")
            if target and not blockers:
                out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","COST","TEST_REQUIRED",
                    {"warehouse_size":cfg.warehouse_size},{"warehouse_size":target},["A_B_PERFORMANCE_VALIDATION_REQUIRED"],
                    ["warehouse_channel"],["max_clusters","min_clusters","auto_stop_minutes"],
                    ["max_clusters","min_clusters","auto_stop_minutes"],
                    "Low spill, queueing, and latency make one-size down a controlled canary candidate; absence of pressure is not proof of SLA safety."))
            else:
                out.append(OptimizationDecision(e.warehouse_id,"warehouse_size","COST_AND_PERFORMANCE","NO_CHANGE",
                    {"warehouse_size":cfg.warehouse_size},{"warehouse_size":cfg.warehouse_size},blockers,["warehouse_channel"],[],[],
                    "No deterministic upsize signal and downsize safety criteria are not all satisfied."))

        # Max clusters
        a=analyses["max_clusters"]; f=a.facts
        saturated = f["at_max_pct"] >= self.t.time_at_max_pct_saturated
        if f["queue_pct"] >= self.t.queue_query_pct_increase_max and f["wait_capacity"] is None and saturated:
            out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["QUEUE_WAIT_EVIDENCE_UNAVAILABLE"],
                ["warehouse_size"],[],[],"Queueing and saturation cross the increase thresholds, but capacity-wait severity was not reported (older collector evidence); no change is recommended."))
        elif f["queue_pct"] >= self.t.queue_query_pct_increase_max and f["wait_capacity"] is not None and f["wait_capacity"] >= self.t.queue_wait_p95_ms_increase_max and saturated:
            if cfg.max_clusters >= self.t.max_clusters_supported_limit:
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","BLOCKED",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["MAX_CLUSTER_LIMIT_REACHED"],
                    ["warehouse_size"],[],[],"Concurrency pressure exists but max clusters is at the policy/API limit."))
            elif f["queue_n"] < self.t.severity_min_affected_queries:
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","INSUFFICIENT_EVIDENCE",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},["INSUFFICIENT_AFFECTED_QUERY_SAMPLE"],
                    ["warehouse_size"],[],[],"Queueing crosses the increase thresholds, but too few queries queued for the wait percentile to be reliable; no change is recommended."))
            else:
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","ACTIONABLE",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters+1},[],["warehouse_size"],["min_clusters"],["min_clusters"],
                    "Sustained queueing plus observed saturation at the current ceiling indicates more concurrency capacity."))
        else:
            peak = f["max_observed"]
            headroom = max(self.t.max_cluster_headroom_clusters, math.ceil(self.t.max_cluster_headroom_pct / 100.0 * peak))
            reduced_max = max(cfg.min_clusters, peak + headroom)
            reduce_ok = (
                cfg.max_clusters > cfg.min_clusters and age >= self.t.current_config_stability_days
                and f["queries_30"] >= self.t.min_query_sample_size and f["queue_pct"] <= self.t.queue_query_pct_safe
                and f["at_max_pct"] <= self.t.time_at_max_pct_safe_reduce and peak > 0
                and reduced_max < cfg.max_clusters
            )
            if reduce_ok:
                target=reduced_max
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","COST_GUARDRAIL","ACTIONABLE",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":target},[],["warehouse_size"],["min_clusters"],["min_clusters"],
                    "The configured ceiling has not been used and queue pressure is low; tighten the guardrail without claiming direct savings."))
            else:
                out.append(OptimizationDecision(e.warehouse_id,"max_clusters","PERFORMANCE","NO_CHANGE",
                    {"max_clusters":cfg.max_clusters},{"max_clusters":cfg.max_clusters},[],["warehouse_size"],[],[],
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
            if f["queue_pct"] > self.t.queue_query_pct_safe or (f["wait_capacity"] is not None and f["wait_capacity"] > self.t.queue_wait_p95_ms_safe): blockers.append("QUEUE_PRESSURE_PRESENT")
            if f["wait_capacity"] is None: blockers.append("QUEUE_WAIT_EVIDENCE_UNAVAILABLE")
            if f["at_max_pct"] >= self.t.time_at_max_pct_block_min_reduce: blockers.append("SUSTAINED_SCALE_PRESSURE")
            if f["at_min_pct"] < self.t.time_at_min_pct_reduce: blockers.append("WAREHOUSE_RARELY_AT_MINIMUM")
            if not blockers:
                out.append(OptimizationDecision(e.warehouse_id,"min_clusters","COST","TEST_REQUIRED",
                    {"min_clusters":cfg.min_clusters},{"min_clusters":cfg.min_clusters-1},
                    ["A_B_PERFORMANCE_VALIDATION_REQUIRED","SPARE_FLOOR_CAPACITY_UNPROVEN"],["warehouse_size","max_clusters"],
                    ["auto_stop_minutes"],["auto_stop_minutes"],
                    "Warehouse spends substantial time at minimum capacity without queue/scale pressure; time at the floor does not prove a floor cluster was spare, so the reduction is a canary candidate and its savings an upper-bound range."))
            else:
                out.append(OptimizationDecision(e.warehouse_id,"min_clusters","COST","NO_CHANGE",
                    {"min_clusters":cfg.min_clusters},{"min_clusters":cfg.min_clusters},blockers,["warehouse_size","max_clusters"],[],[],
                    "Minimum-cluster reduction is not sufficiently supported by current-epoch evidence."))

        # Auto stop
        a=analyses["auto_stop_minutes"]; f=a.facts; target=safe_int(f["target"])
        if cfg.auto_stop_minutes != 0 and cfg.auto_stop_minutes <= target:
            out.append(OptimizationDecision(e.warehouse_id,"auto_stop_minutes","COST","NO_CHANGE",
                {"auto_stop_minutes":cfg.auto_stop_minutes},{"auto_stop_minutes":cfg.auto_stop_minutes},[],["min_clusters"],[],[],
                "Current auto-stop is already at or below the deterministic target."))
        else:
            blockers=[]
            if age < self.t.current_config_stability_days: blockers.append("CURRENT_CONFIG_EPOCH_TOO_NEW")
            if f["queries_30"] < self.t.min_query_sample_size: blockers.append("INSUFFICIENT_QUERY_SAMPLE")
            if safe_int(f.get("gap_count")) < self.t.min_idle_gap_sample_size: blockers.append("INSUFFICIENT_IDLE_GAP_SAMPLE")
            if safe_float(f.get("cluster_minutes")) < self.t.min_cluster_minutes_for_cost_model: blockers.append("INSUFFICIENT_EVENT_RESOURCE_EVIDENCE")
            if f["wait_compute"] is None: blockers.append("COLD_START_EVIDENCE_UNAVAILABLE")
            elif f["wait_compute"] >= self.t.cold_start_p95_ms_block and f["starts_per_day"] >= self.t.cold_starts_per_active_day_block:
                blockers.append("COLD_START_SENSITIVE")
            status="ACTIONABLE" if not blockers else "BLOCKED"
            out.append(OptimizationDecision(e.warehouse_id,"auto_stop_minutes","COST",status,
                {"auto_stop_minutes":cfg.auto_stop_minutes},{"auto_stop_minutes":target},blockers,["min_clusters"],[],[],
                "Idle-gap evidence supports a lower auto-stop target." if not blockers else "A lower target exists but guardrails block automatic recommendation."))

        # Warehouse-type migration is always a separate scenario branch.
        a=analyses["warehouse_type"]; f=a.facts
        blockers=["A_B_COST_PERFORMANCE_VALIDATION_REQUIRED"]
        if e.baseline_forecast_cost_365 is None:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","INSUFFICIENT_EVIDENCE",
                {"warehouse_type":cfg.warehouse_type},{"warehouse_type":cfg.warehouse_type},["DBU_COST_UNAVAILABLE"],[],[],[],
                "Migration is judged on a pre-canary cost comparison, but the current DBU cost is unavailable: usage was billed under an SKU with no list price."))
        elif e.cost_history_days < self.t.min_economics_cost_history_days:
            out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","INSUFFICIENT_EVIDENCE",
                {"warehouse_type":cfg.warehouse_type},{"warehouse_type":cfg.warehouse_type},["COST_HISTORY_INSUFFICIENT"],[],[],[],
                f"Migration is judged on a pre-canary cost comparison, but the current epoch has fewer than {self.t.min_economics_cost_history_days} days of billing history to price it."))
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

    def _confidence(self,e:WarehouseEvidence,a:AnalysisResult,signal:float=1.0,cap:float=1.0)->Dict[str,Any]:
        q=safe_float(a.facts.get("queries_30")); coverage=clamp(e.cost_history_days/self.t.confidence_coverage_days,0,1); sample=clamp(q/self.t.strong_query_sample_size,0,1)
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

    def _matched_epoch_factor(self,e:WarehouseEvidence,knob:str,target:Any)->Optional[Dict[str,Any]]:
        cur=e.current_epoch; cur_cfg=cur.config.as_dict(); cur_queries=max(1.0,safe_float(cur.query.get("epoch_queries")))
        cur_cost=safe_float(cur.cost.get("list_cost")); cur_cpk=cur_cost/cur_queries*self.t.cost_normalization_query_count if cur_cost>0 else 0
        if cur_cpk<=0: return None
        matches=[]
        for ep in e.epochs:
            if ep.is_current: continue
            if safe_int(ep.cost.get("cost_days"))<self.t.min_epoch_calibration_days: continue
            q=safe_float(ep.query.get("epoch_queries")); c=safe_float(ep.cost.get("list_cost"))
            if q<self.t.min_epoch_calibration_queries or c<=0: continue
            cfg=ep.config.as_dict()
            if cfg.get(knob)!=target: continue
            if any(cfg.get(k)!=cur_cfg.get(k) for k in ACTIVE_KNOBS if k!=knob): continue
            factor=(c/q*self.t.cost_normalization_query_count)/cur_cpk
            factor=clamp(factor,self.t.epoch_factor_floor,self.t.epoch_factor_ceiling)
            matches.append((safe_int(ep.cost.get("cost_days")),q,factor,ep.epoch_id))
        if not matches: return None
        matches.sort(key=lambda x:(x[1],x[0]),reverse=True); days,q,factor,eid=matches[0]
        return {"factor":factor,"epoch_id":eid,"queries":q,"days":days,"method":"matched historical config epoch normalized by cost per 1,000 queries"}

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
        if knob in {"warehouse_channel","max_clusters"} and d.objective in {"STABILITY","COST_GUARDRAIL"}:
            return Range3(1.0,1.0,1.0),{"method":"guardrail/stability change; no direct savings assigned","bundle_effect":{"type":"no_cost_change"},**meta}
        if e.baseline_forecast_cost_365 is None:
            # Every estimate below scales the forward DBU cost; with unpriced usage in its basis that cost is unknown.
            return None,{
                "method":"estimate withheld: the forward DBU cost is unavailable (usage billed under an SKU with no list price)",
                "estimate_blocker":"DBU_COST_UNAVAILABLE",
                "bundle_effect":{"type":"estimate_withheld","status":"UNAVAILABLE","reason":"The forward DBU cost is unavailable because usage was billed under an SKU with no list price; economics are withheld."},**meta}
        if e.cost_history_days < self.t.min_economics_cost_history_days:
            # Every estimate below scales the forward cost forecast, which is not trustworthy on a very young epoch.
            return None,{
                "method":f"estimate withheld: fewer than {self.t.min_economics_cost_history_days} days of current-epoch cost history",
                "estimate_blocker":"COST_HISTORY_INSUFFICIENT",
                "bundle_effect":{"type":"estimate_withheld","status":"REQUIRES_COST_HISTORY","reason":"At least one step cannot be priced because the current epoch has too little billing history; economics are withheld until it accrues."},**meta}
        if knob=="warehouse_type":
            return None,{"method":"warehouse-type migration uses explicit DBU + AWS pre-canary cost model","bundle_effect":{"type":"migration_cost_override"},**meta}

        if knob=="auto_stop_minutes":
            saved_idle=safe_float(a.facts.get("saved_idle"))
            cluster=safe_float(a.facts.get("cluster_minutes"))
            current_min=max(1,safe_int(a.facts.get("current_min_clusters"),1))
            theoretical_removed=saved_idle*current_min
            raw=clamp(theoretical_removed/cluster,0,self.t.auto_stop_savings_cap) if cluster>0 else 0
            factors=Range3(1-raw*self.t.realization_optimistic,1-raw*self.t.realization_base,1-raw*self.t.realization_conservative)
            return factors,{
                "method":"current-epoch inter-query idle-gap converted to removable cluster-minutes",
                "raw_savings_fraction":round(raw,4),
                # The same cap the candidate applies, as a ceiling on removable cluster-minutes, so both models agree.
                "bundle_effect":{"type":"idle_cluster_minute_reduction","idle_minutes":saved_idle,"max_removable_cluster_minutes":self.t.auto_stop_savings_cap*cluster},
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
            capacity_source="NOMINAL_SIZE_CAPACITY_RATIO"
            capacity=size_ratio(cur,target)
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
                empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
                if isinstance(empirical,Mapping):
                    cur_rate=safe_float(empirical.get(cur),0.0)
                    target_rate=safe_float(empirical.get(target),0.0)
                    if cur_rate>0 and target_rate>0:
                        capacity=target_rate/cur_rate
                        capacity_source="WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR_RATIO"
            if capacity<1:
                runtime=Range3(self.t.downsize_runtime_multiplier_optimistic,self.t.downsize_runtime_multiplier_base,self.t.downsize_runtime_multiplier_conservative)
            else:
                runtime=Range3(self.t.upsize_runtime_multiplier_optimistic,self.t.upsize_runtime_multiplier_base,self.t.upsize_runtime_multiplier_conservative)
            factors=Range3(capacity*runtime.low,capacity*runtime.base,capacity*runtime.high)
            calib=self._matched_epoch_factor(e,"warehouse_size",target)
            if calib:
                f=calib["factor"]
                factors=Range3(
                    (1-self.t.epoch_calibration_weight)*factors.low+self.t.epoch_calibration_weight*f,
                    (1-self.t.epoch_calibration_weight)*factors.base+self.t.epoch_calibration_weight*f,
                    (1-self.t.epoch_calibration_weight)*factors.high+self.t.epoch_calibration_weight*f,
                )
                meta["historical_epoch_calibration"]=calib
            vals=sorted([
                clamp(factors.low,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
                clamp(factors.base,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
                clamp(factors.high,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
            ])
            factors=Range3(vals[0],vals[1],vals[2])
            return factors,{
                "method":"capacity ratio × runtime elasticity, optionally blended with matched config epoch",
                "capacity_ratio":capacity,
                "capacity_ratio_source":capacity_source,
                "runtime_multiplier_range":as_serializable(runtime),
                "bundle_effect":{"type":"multiplicative_cost_factor","resource_runtime_factor_range":as_serializable(runtime)},
                **meta,
            }

        if knob=="max_clusters" and d.recommended_config.get("max_clusters",0)>d.current_config.get("max_clusters",0):
            at=safe_float(a.facts.get("minutes_at_max")); cluster=safe_float(a.facts.get("cluster_minutes"))
            current=safe_int(d.current_config.get("max_clusters"),1); target=safe_int(d.recommended_config.get("max_clusters"),current)
            delta=max(0,target-current)
            raw=clamp((at*delta)/cluster,0,self.t.max_cluster_uplift_fraction_cap) if cluster>0 else 0
            factors=Range3(1.0,1+raw*self.t.max_cluster_base_uplift_weight,1+raw)
            return factors,{
                "method":"upper-bound cost uplift from additional clusters during observed saturation",
                "raw_uplift_fraction":round(raw,4),
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
        ep=e.current_epoch; events=ep.events
        days=observed_epoch_calendar_days(ep.epoch_start,ep.epoch_end,e.analysis_time)
        cluster_minutes=safe_float(events.get("cluster_minutes")); uptime=safe_float(events.get("uptime_minutes")); max_obs=max(1,safe_int(events.get("max_observed_clusters"),1))
        if days<=0 or cluster_minutes<=0 or uptime<=0:
            return None,{"status":"UNAVAILABLE","blocker":"INSUFFICIENT_CURRENT_EPOCH_RESOURCE_EVIDENCE","observed_days":days,"cluster_minutes":cluster_minutes,"uptime_minutes":uptime}
        trend=safe_float(e.forecast_metadata.get("trend_adjustment_factor"),1.0)
        scale=365.0/days*trend
        low=uptime*scale
        base=cluster_minutes*scale
        high=max(base,uptime*max_obs*scale)
        vals=sorted([low,base,high])
        return Range3(vals[0],vals[1],vals[2]),{"status":"MODELED","method":"current-epoch observed resource-time structural bounds annualized forward","observed_days":days,"trend_adjustment_factor":trend,"current_cluster_minutes":cluster_minutes,"current_uptime_minutes":uptime,"max_observed_clusters":max_obs,"annualized_cluster_minutes_range":as_serializable(Range3(vals[0],vals[1],vals[2]))}

    def _migration_costs(self,e:WarehouseEvidence,d:OptimizationDecision)->Dict[str,Any]:
        target=str(d.recommended_config.get("warehouse_type") or "").upper()
        size=e.current_epoch.config.warehouse_size
        resources,resource_meta=self._annualized_resource_bounds(e)
        if resources is None:
            return {"available":False,"blocker":resource_meta.get("blocker"),"resource_model":resource_meta}
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
        dbu_cost=Range3(*(round(x/60.0*dbu_per_hour*unit_price,2) for x in (resources.low,resources.base,resources.high)))
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
        return {"available":True,"target_type":target,"warehouse_size":size,"dbu_cost_range":dbu_cost,"aws_cost_range":aws_cost,"total_cost_range":total_cost,"dbu_per_cluster_hour":dbu_per_hour,"dbu_rate_source":rate_source,"unit_price_per_dbu":unit_price,"resource_model":resource_meta,"sku_price_model":price_meta,"aws_model":aws_meta,"estimate_status":"PRE_CANARY_MODELED_RANGE"}

    def candidates(self,e:WarehouseEvidence,analyses:Dict[str,AnalysisResult],decisions:Sequence[OptimizationDecision])->List[CandidateRecommendation]:
        out=[]; ep=e.current_epoch
        dbu_baseline=round_or_none(e.baseline_forecast_cost_365,2); aws_baseline=None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365,2)
        total_baseline=None if aws_baseline is None or dbu_baseline is None else round(dbu_baseline+aws_baseline,2)
        for d in decisions:
            a=analyses[d.knob]; factors,meta=self._factors(e,d,a)
            dbu_costs=dbu_savings=None; aws_costs=aws_savings=None; total_costs=total_savings=None
            est=save=None
            if d.knob=="warehouse_type" and d.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"} and d.current_config!=d.recommended_config:
                migration=self._migration_costs(e,d); meta={**meta,"migration_model":as_serializable(migration),"bundle_effect":{"type":"migration_cost_override"}}
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
            cap=self.t.confidence_cap_test_required if d.status=="TEST_REQUIRED" else self.t.confidence_cap_actionable if d.status=="ACTIONABLE" else self.t.confidence_cap_default
            confidence=self._confidence(e,a,signal=signal,cap=cap)
            current=d.current_config; recommended=d.recommended_config
            rid=recommendation_id(e.workspace_id,e.warehouse_id,d.knob,ep.epoch_id,current,recommended)
            out.append(CandidateRecommendation(
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
            ))
        return out



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
            if not 0<=sc_score<=1:
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
PRICING_CONTINGENT = "CONTINGENT_PENDING_REEVALUATION"


@dataclass
class _ChainState:
    """Evolving cost/resource state for one pricing chain; `halt` = (status, method) once it cannot be priced further."""
    dbu_tracks: List[float]
    aws_tracks: Optional[List[float]]
    resource_tracks: List[float]
    current_min: int
    halt: Optional[Tuple[str, str]] = None


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
        elif typ in {"min_cluster_reduction","idle_cluster_minute_reduction"}:
            if typ=="min_cluster_reduction":
                theoretical=max(0.0,safe_float(effect.get("cluster_minutes_to_remove")))
                target_min=max(1,safe_int(effect.get("target_min_clusters"),st.current_min))
            else:
                theoretical=max(0.0,safe_float(effect.get("idle_minutes")))*st.current_min
                if effect.get("max_removable_cluster_minutes") is not None:
                    theoretical=min(theoretical,max(0.0,safe_float(effect.get("max_removable_cluster_minutes"))))
                target_min=st.current_min
            effect_realizations=[safe_float(x) for x in effect["realization"]] if effect.get("realization") else realizations
            for i,realization in enumerate(effect_realizations):
                before=st.resource_tracks[i]
                if before<=0: continue
                removed=min(before,theoretical*realization)
                after=max(0.0,before-removed)
                ratio=after/before
                st.dbu_tracks[i]*=ratio
                if st.aws_tracks is not None: st.aws_tracks[i]*=ratio
                st.resource_tracks[i]=after
            st.current_min=target_min
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
            st=_ChainState([dbu_base]*3,[aws_base]*3 if aws_base is not None else None,[original_resource]*3,max(1,e.current_epoch.config.min_clusters))
            if dbu_base is None:
                st.halt=("UNAVAILABLE","The forward DBU cost is unavailable (usage billed under an SKU with no list price); plan economics are withheld.")
            return st
        auth,cont=fresh(),fresh()
        realizations=[self.t.realization_optimistic,self.t.realization_base,self.t.realization_conservative]

        for step in steps:
            c=candidate_by_id[step.recommendation_id]
            effect=c.metadata.get("bundle_effect") or {"type":"no_cost_change"}
            pending=step.execution_state=="PENDING_REEVALUATION"
            step.pricing_basis=PRICING_CONTINGENT if pending else PRICING_AUTHORITATIVE
            if not pending: self._apply(auth,c,effect,original_resource,realizations)
            self._apply(cont,c,effect,original_resource,realizations)
            chain=cont if pending else auth
            snap=self._portfolio(e,"RUNNING",chain.dbu_tracks,chain.aws_tracks,"Running stateful plan estimate.") if chain.halt is None else None
            step.running_dbu_cost_range_365=snap.dbu_365d_estimated_cost_range if snap else None
            step.running_aws_cost_range_365=snap.aws_365d_estimated_cost_range if snap else None
            step.running_dbu_savings_range_365=snap.dbu_365d_savings_range if snap else None
            step.running_aws_savings_range_365=snap.aws_365d_savings_range if snap else None
            step.running_estimated_cost_range_365=(snap.total_365d_estimated_cost_range or snap.dbu_365d_estimated_cost_range) if snap else None
            step.running_estimated_savings_range_365=(snap.total_365d_savings_range or snap.dbu_365d_savings_range) if snap else None

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
            return {"score":1.0,"level":"HIGH","method":"no-change/empty scenario"}
        score=min(scores)
        return {"score":round(score,3),"level":"HIGH" if score>=self.t.confidence_high_threshold else "MEDIUM" if score>=self.t.confidence_medium_threshold else "LOW","method":"bottleneck minimum of step confidences"}

    def _gate(self,c:CandidateRecommendation)->str:
        if c.knob=="warehouse_channel": return "HITL_CONFIRM_PREVIEW_DEPENDENCIES"
        if c.knob=="warehouse_size" and c.status=="TEST_REQUIRED": return "A_B_CANARY_LATENCY_COST_ERROR"
        if c.knob=="min_clusters" and c.status=="TEST_REQUIRED": return "A_B_CANARY_QUEUE_LATENCY_COST"
        if c.knob=="warehouse_type": return "A_B_CANARY_COST_PERFORMANCE_COMPATIBILITY"
        return "HITL_APPROVAL_AND_POST_CHANGE_VALIDATION"

    def _compatibility(self,e:WarehouseEvidence,target_type:str)->Dict[str,Any]:
        q=e.current_epoch.query; checks=[]
        target=target_type.upper(); api=dict(e.compatibility_evidence or {})
        if target=="SERVERLESS":
            region=(e.source_region or "").split(",")[0]
            checks.append({"check":"aws_region_serverless_availability","status":"PASS" if region and region != "unknown" else "UNKNOWN","evidence":{"region":region,"policy":"Databricks documents Serverless SQL warehouse support across AWS regions."}})
            observed=bool(e.billing_features.get("workspace_serverless_observed_from_system_compute_warehouses"))
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
            visible=safe_float(q.get("statement_text_visible_pct_30")); hive=safe_int(q.get("hive_metastore_reference_queries_30"))
            checks.append({"check":"legacy_hive_metastore_sql_reference","status":"REVIEW" if hive>0 else "PASS_WITH_LIMITATION" if visible>0 else "UNKNOWN","evidence":{"visible_pct":visible,"reference_queries":hive},"source":"system.query.history"})
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
        plan_order=["warehouse_channel","warehouse_size","max_clusters","min_clusters","auto_stop_minutes"]
        steps=[]; pending_invalidated=set()
        for knob in plan_order:
            c=by_knob[knob]
            if c.status not in {"ACTIONABLE","TEST_REQUIRED","REVIEW"}: continue
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
        target=str(type_c.recommended_config.get("warehouse_type") or "UNKNOWN").upper()
        plan_b_name=f"Plan B — {target.title()} migration experiment"
        plan_b_id=scenario_id(e.workspace_id,e.warehouse_id,"Plan B",[s.recommendation_id for s in bsteps])
        compat=self._compatibility(e,target)
        pe=self.bundle.estimate(e,bsteps,by_id) if bsteps else self.bundle.estimate(e,[],by_id)
        plan_b=Scenario(plan_b_id,e.warehouse_id,e.workspace_id,plan_b_name,f"Canary {target} as a separate branch. The pre-canary cost range uses current-epoch resource bounds and target DBU/AWS pricing; successful migration invalidates current-type tuning and triggers re-analysis.",bsteps,pe,type_c.blockers if bsteps else [],False,compat,self._scenario_confidence(bsteps))
        return [plan_a,plan_b],plan_a.scenario_id


# ---------------------------------------------------------------------------
# Locked Reporter JSON Contract v1.2.0 adapter / canonical JSON packaging
# ---------------------------------------------------------------------------


def _scalar_config(d: Mapping[str, Any]) -> Any:
    """Return the scalar from a one-key config mapping; preserve composite mappings."""
    if len(d) == 1:
        return next(iter(d.values()))
    return dict(d)


_CONTRACT_SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "recommendation-package.schema.json"
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
    """Cumulative stateful savings after this step, or None when the chain could not price it."""
    if step.running_dbu_savings_range_365 is None and step.running_aws_savings_range_365 is None:
        return None
    total = step.running_estimated_savings_range_365 if step.running_aws_savings_range_365 is not None else None
    return rc.StepProjection(
        pricing_basis=rc.PricingBasis(step.pricing_basis),
        running_savings_365d=_contract_component_ranges(
            step.running_dbu_savings_range_365, step.running_aws_savings_range_365, total
        ),
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
        method="current_epoch_runrate_evidence",
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
        p95 = safe_float(q.get("p95_execution_ms_30"))
        latency_class = (
            rc.WorkloadLatencyClass.INTERACTIVE if p95 <= 5_000
            else rc.WorkloadLatencyClass.BATCH if p95 >= 60_000
            else rc.WorkloadLatencyClass.MIXED
        )
        queued = safe_float(q.get("queued_query_pct_30"))
        concurrency = (
            rc.ConcurrencyPattern.STEADY if queued <= t.queue_query_pct_safe
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
    if knob == "warehouse_channel":
        return "WH-CHANNEL-CURRENT-001" if rec == "CURRENT" else "WH-CHANNEL-STABILITY-001"
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
    if assessment in {"CURRENT_TYPE", "COMPATIBLE"}:
        status = rc.CompatibilityStatus.COMPATIBLE
    elif assessment in {"INCOMPATIBLE", "BLOCKED"}:
        status = rc.CompatibilityStatus.INCOMPATIBLE
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


def _assessment_contract(e: WarehouseEvidence, candidates: Sequence[rc.CandidateRecommendation], t: Thresholds) -> rc.Assessment:
    counts = {status: 0 for status in rc.RecommendationStatus}
    for c in candidates:
        counts[c.status] += 1
    if counts[rc.RecommendationStatus.ACTIONABLE] or counts[rc.RecommendationStatus.TEST_REQUIRED]:
        overall = rc.AssessmentStatus.OPTIMIZATION_AVAILABLE
    elif counts[rc.RecommendationStatus.REVIEW]:
        overall = rc.AssessmentStatus.REVIEW_REQUIRED
    elif counts[rc.RecommendationStatus.BLOCKED]:
        overall = rc.AssessmentStatus.BLOCKED
    elif counts[rc.RecommendationStatus.INSUFFICIENT_EVIDENCE]:
        overall = rc.AssessmentStatus.INSUFFICIENT_EVIDENCE
    else:
        overall = rc.AssessmentStatus.NO_CHANGE

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
            "Earliest only: billing ingestion lag and query-sample gates can delay it."
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
        decision_summary=f"{recommended.scenario_name} is the deterministic execution path; apply validation gates and regenerate after invalidating changes.",
        reevaluation=reevaluation,
    )


def _build_locked_recommendation_package(
    e: WarehouseEvidence,
    candidates: Sequence[CandidateRecommendation],
    scenarios: Sequence[Scenario],
    thresholds: Thresholds,
) -> rc.RecommendationPackage:
    """Build one immutable Reporter JSON Contract v1.2.0 package from reviewed decisions."""
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
        "Forward DBU cost uses the deterministic current-epoch run-rate model.",
        "AWS forward cost is modeled from observed current-epoch resource time and the configured regional price registry when available.",
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
        assessment=_assessment_contract(e, candidate_contracts, thresholds),
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


def _prepare_workspace_calibration(evidence: Sequence[WarehouseEvidence]) -> None:
    """Attach workspace-level capability/calibration observations derived only from collected system-table evidence."""
    serverless_workspaces = {x.workspace_id for x in evidence if any(ep.config.warehouse_type == "SERVERLESS" for ep in x.epochs)}
    pro_classic_workspaces = {x.workspace_id for x in evidence if any(ep.config.warehouse_type in {"PRO", "CLASSIC"} for ep in x.epochs)}
    pro_rates: Dict[str, Dict[str, List[float]]] = {}
    for x in evidence:
        ep = x.current_epoch
        if ep.config.warehouse_type not in {"PRO", "CLASSIC"}:
            continue
        dbu = safe_float(ep.cost.get("dbu_quantity"))
        cluster_minutes = safe_float(ep.events.get("cluster_minutes"))
        if dbu > 0 and cluster_minutes > 0:
            pro_rates.setdefault(x.workspace_id, {}).setdefault(ep.config.warehouse_size, []).append(dbu / (cluster_minutes / 60.0))
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
    _prepare_workspace_calibration(supported)
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
        raise RuntimeError("jsonschema>=4.23 is required to validate Reporter Contract v1.2.0 output") from exc
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
    """Write locked JSON packages plus the Workbook Contract v1.3.0 projection.

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
        workbook_path = write_workbook(output_dir / "workbook.xlsx", packages)
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
        "canonical_json_semantics": "Each packages/*.json file is exactly one Reporter JSON Contract v1.2.0 RecommendationPackage.",
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

def _demo_daily(start: dt.date, days: int, total: float, workspace_id: str, warehouse_id: str, epoch_start: str) -> List[Dict[str, Any]]:
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
    return rows


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
        },
        events={
            "uptime_minutes": 8000.0, "cluster_minutes": 12000.0, "minutes_at_min": 6000.0,
            "minutes_at_max": 0.0, "time_at_min_pct": 75.0, "time_at_max_pct": 0.0,
            "max_observed_clusters": 2,
        },
        gaps={},
        cost={"list_cost": 3900.0, "dbu_quantity": 4800.0, "cost_days": 106, "active_cost_days": 106, "currency": "USD"},
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
        },
        events={
            "uptime_minutes": 20000.0, "cluster_minutes": 30000.0, "minutes_at_min": 16000.0,
            "minutes_at_max": 0.0, "time_at_min_pct": 80.0, "time_at_max_pct": 0.0,
            "max_observed_clusters": 2, "starts_30": 8, "scale_ups_30": 3, "scale_downs_30": 3,
        },
        gaps={
            "gap_count": 480, "p50_gap_minutes": 8.0, "p90_gap_minutes": 95.0,
            "saved_idle_minutes_if_10": 12500.0, "saved_idle_minutes_if_15": 11500.0,
            "saved_idle_minutes_if_30": 9000.0, "saved_idle_minutes_if_45": 6500.0,
        },
        cost={"list_cost": 21100.0, "dbu_quantity": 20000.0, "cost_days": 259, "active_cost_days": 259, "currency": "USD"},
    )

    daily = (
        _demo_daily(dt.date(2025, 10, 1), 106, 3900.0, workspace, wh, old_start)
        + _demo_daily(dt.date(2026, 1, 15), 259, 21100.0, workspace, wh, cur_start)
    )
    forecast={
        "forecast_method": MODEL_VERSION, "active_cost_days": 259,
        "forecast_basis_start": "2026-01-15", "forecast_basis_end": "2026-09-30",
        "run_rate_28": 28000.0, "run_rate_84": 26500.0, "zero_cost_days_in_basis": 0,
        "trend_ratio": 1.0, "trend_adjustment_factor": 1.0,
    }
    registry=load_aws_price_registry(str(Path(__file__).resolve().parent / "pricing" / "aws_ec2_on_demand.json"))
    aws_cost,aws_model=model_aws_baseline_cost(cur,"us-east-1",analysis_time,forecast,registry)
    e=WarehouseEvidence(
        warehouse_id=wh, workspace_id=workspace, account_id=cur.account_id, warehouse_name=cur.warehouse_name,
        source_region="us-east-1", analysis_time=analysis_time, current_epoch_id=cur.epoch_id, epochs=[old, cur],
        current_cost_365=25000.0, baseline_forecast_cost_365=27000.0, currency="USD",
        cost_history_days=259, cost_cv_84=0.25, forecast_metadata=forecast, daily_cost_365=daily,
        billing_features={"photon_usage_pct_365": 100.0, "serverless_usage_pct_365": 0.0, "cloud": "AWS"},
        aws_baseline_cost_365=aws_cost, aws_cost_model=aws_model,
        sql_sku_prices={
            "PREMIUM_SQL_PRO_COMPUTE":{"effective_list_price":0.22,"currency_code":"USD","cloud":"AWS","usage_unit":"DBU","classification":"PRO"},
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



def synthetic_demo_evidence(count: int = 15) -> List[WarehouseEvidence]:
    """Return up to 15 deterministic warehouse observations at the collector boundary.

    The fixture intentionally spans materially different operating conditions so
    every downstream layer (epochs, analyzers, candidates, scenarios, package
    contract, workbook projection) is exercised by one `--demo` run.
    """
    if not (1 <= count <= 15):
        raise ValueError("demo count must be between 1 and 15")
    analysis_time = "2026-10-01T12:00:00+00:00"
    registry = load_aws_price_registry(str(Path(__file__).resolve().parent / "pricing" / "aws_ec2_on_demand.json"))
    specs = [
        # name, type, size, min, max, autostop, q30, queue%, wait_ms, spill%, spill_p95, p95, p99, min%, max%, observed_max, starts, source, baseline, photon, epoch_start
        ("BI - Sales", "PRO", "LARGE", 2, 4, 120, 12000, 0.0, 0, 0.0, 0, 2500, 10000, 80, 0, 2, 8, "DASHBOARD", 27000, "ENABLED", "2026-01-15T00:00:00+00:00"),
        ("Finance Executive", "PRO", "MEDIUM", 1, 4, 30, 9000, 0.2, 1000, 0.0, 0, 2200, 7000, 62, 1, 2, 7, "DASHBOARD", 18000, "ENABLED", "2026-02-01T00:00:00+00:00"),
        ("Marketing Ad Hoc", "PRO", "LARGE", 1, 3, 120, 900, 0.0, 0, 0.0, 0, 2800, 11000, 55, 0, 1, 5, "NOTEBOOK", 14500, "ENABLED", "2026-02-15T00:00:00+00:00"),
        ("ETL Heavy", "PRO", "X_LARGE", 2, 8, 60, 6500, 0.8, 5000, 6.5, 3*GIB, 14000, 48000, 25, 12, 7, 5, "JOB", 64000, "ENABLED", "2026-01-10T00:00:00+00:00"),
        ("Finance Concurrency", "PRO", "MEDIUM", 1, 2, 30, 18000, 12.0, 65000, 0.0, 0, 3600, 15000, 30, 45, 2, 4, "DASHBOARD", 36000, "ENABLED", "2026-01-20T00:00:00+00:00"),
        ("Executive Dashboards", "PRO", "LARGE", 3, 6, 120, 15000, 0.0, 0, 0.0, 0, 1900, 6500, 88, 0, 3, 6, "DASHBOARD", 48000, "ENABLED", "2026-03-01T00:00:00+00:00"),
        ("New Product Launch", "PRO", "SMALL", 1, 2, 120, 2400, 0.0, 0, 0.0, 0, 2400, 8000, 60, 0, 1, 3, "DASHBOARD", 12000, "ENABLED", "2026-09-28T00:00:00+00:00"),
        ("Serverless Analytics", "SERVERLESS", "MEDIUM", 1, 10, 10, 22000, 0.0, 0, 0.0, 0, 1800, 5500, 0, 0, 4, 0, "DASHBOARD", 52000, "ENABLED", "2026-01-05T00:00:00+00:00"),
        ("Photon Migration Lab", "PRO", "SMALL", 1, 4, 60, 7200, 0.1, 1000, 0.0, 0, 4200, 16000, 55, 2, 2, 5, "NOTEBOOK", 21000, "DISABLED", "2026-02-10T00:00:00+00:00"),
        ("Cold Start BI", "PRO", "MEDIUM", 1, 4, 120, 4800, 0.0, 0, 0.0, 0, 3000, 9000, 60, 0, 2, 24, "DASHBOARD", 17000, "ENABLED", "2026-01-25T00:00:00+00:00"),
        ("Sandbox Sparse", "PRO", "SMALL", 1, 2, 120, 45, 0.0, 0, 0.0, 0, 1600, 4000, 45, 0, 1, 2, "NOTEBOOK", 3200, "UNKNOWN", "2026-06-01T00:00:00+00:00"),
        ("Supply Chain BI", "PRO", "LARGE", 1, 5, 60, 11000, 0.1, 800, 0.0, 0, 2300, 8500, 65, 0, 2, 6, "POWER_BI", 33000, "ENABLED", "2026-01-15T00:00:00+00:00"),
        ("Customer 360", "PRO", "LARGE", 2, 8, 45, 9800, 1.0, 9000, 4.2, 2*GIB, 11000, 36000, 35, 15, 7, 5, "JOB", 58000, "ENABLED", "2026-01-12T00:00:00+00:00"),
        ("Ops Dashboards", "PRO", "MEDIUM", 1, 8, 30, 13000, 0.0, 0, 0.0, 0, 2100, 7200, 58, 0, 2, 6, "DASHBOARD", 26000, "ENABLED", "2026-01-18T00:00:00+00:00"),
        ("HR Reporting", "PRO", "SMALL", 2, 2, 120, 3200, 0.0, 0, 0.0, 0, 2000, 6000, 90, 0, 2, 4, "DASHBOARD", 15500, "ENABLED", "2026-01-22T00:00:00+00:00"),
    ]
    owners = ["sales-bi","finance-bi","marketing-analytics","data-engineering","finance-platform","exec-analytics","product-analytics","serverless-analytics","platform-lab","bi-platform","sandbox","supply-chain","customer360","operations","hr-analytics"]
    workspace_ids = ["1234567890123456"]*8 + ["2234567890123456"]*7
    out: List[WarehouseEvidence] = []
    for i, spec in enumerate(specs[:count], start=1):
        name,wtype,size,minc,maxc,autostop,q30,queue_pct,wait_ms,spill_pct,spill_p95,p95,p99,min_pct,max_pct,observed_max,starts,source,baseline,photon,epoch_start = spec
        workspace = workspace_ids[i-1]
        wh = f"wh-synth-{i:03d}"
        age_days=max(1,(parse_datetime(analysis_time)-parse_datetime(epoch_start)).days)  # type: ignore[operator]
        active_days=min(30,max(1,30 if q30>=300 else max(1,int(q30/10))))
        epoch_queries=max(q30,int(q30*age_days/30))
        source_counts={"dashboard_queries_30":0,"job_queries_30":0,"notebook_queries_30":0,"genie_queries_30":0,"alert_queries_30":0,"sql_query_source_queries_30":0,"power_bi_queries_30":0,"tableau_queries_30":0,"looker_queries_30":0,"databricks_sql_editor_client_queries_30":0}
        source_key={"DASHBOARD":"dashboard_queries_30","JOB":"job_queries_30","NOTEBOOK":"notebook_queries_30","GENIE":"genie_queries_30","POWER_BI":"power_bi_queries_30"}.get(source,"sql_query_source_queries_30")
        source_counts[source_key]=int(q30*0.75)
        source_counts["sql_query_source_queries_30"]=source_counts["sql_query_source_queries_30"] or int(q30*0.08)
        if source=="DASHBOARD": source_counts["power_bi_queries_30"]=int(q30*0.25)
        wait_compute=85000 if name=="Cold Start BI" else 3500
        queued_n=round(queue_pct/100.0*q30); spill_n=round(spill_pct/100.0*q30)
        compute_wait_n=starts  # compute waits come from the first query after each warehouse (re)start
        q={
            "epoch_queries":epoch_queries,"epoch_active_days":min(age_days,250),"queries_30":q30,"active_days_30":active_days,
            "p95_total_ms_30":p95+1000,"compute_wait_queries_30":compute_wait_n,"p95_wait_compute_ms_when_waited_30":wait_compute if compute_wait_n else None,
            "queued_queries_30":queued_n,"p95_wait_capacity_ms_when_queued_30":wait_ms if queued_n else None,
            "p95_execution_ms_30":p95,"p99_execution_ms_30":p99,"max_execution_ms_30":max(p99*2,120000),
            "queued_query_pct_30":queue_pct,"queue_time_share_pct_30":min(queue_pct,25.0),"spill_query_pct_30":spill_pct,"spill_queries_30":spill_n,"p95_spill_bytes_when_spilled_30":spill_p95 if spill_n else None,
            "total_spill_gib_30":round(spill_pct*2,2),"p95_read_bytes_30":20*1024**2,"avg_read_io_cache_percent_30":35.0,
            "failure_cancel_pct_30":0.3,"result_cache_pct_30":10.0,"tagged_query_pct_30":70.0,
            "statement_text_visible_pct_30":100.0,"hive_metastore_reference_queries_30":0,
            "epoch_p95_execution_ms":p95,"epoch_queued_query_pct":queue_pct,"epoch_spill_query_pct":spill_pct,
            **source_counts,
            "client_applications_30":[source.replace("_"," ").title()] if source in {"POWER_BI"} else [],
        }
        uptime=20000.0 if wtype!="SERVERLESS" else 16000.0
        avg_clusters=max(float(minc),min(float(maxc),max(1.0,float(observed_max)-0.5)))
        cluster_minutes=uptime*avg_clusters
        events={"uptime_minutes":uptime,"cluster_minutes":cluster_minutes,"minutes_at_min":uptime*min_pct/100.0,"minutes_at_max":uptime*max_pct/100.0,"time_at_min_pct":float(min_pct),"time_at_max_pct":float(max_pct),"max_observed_clusters":observed_max,"starts_30":starts,"scale_ups_30":max(0,observed_max-minc),"scale_downs_30":max(0,observed_max-minc)}
        idle_factor=0.55 if autostop>=120 else 0.25 if autostop>=60 else 0.12
        gaps={"gap_count":max(5,int(q30/25)),"p50_gap_minutes":10.0,"p90_gap_minutes":90.0 if autostop>=60 else 35.0,"saved_idle_minutes_if_10":uptime*idle_factor,"saved_idle_minutes_if_15":uptime*idle_factor*0.92,"saved_idle_minutes_if_30":uptime*idle_factor*0.72,"saved_idle_minutes_if_45":uptime*idle_factor*0.50}
        config=WarehouseConfig(wtype,"CURRENT",size,minc,maxc,autostop)
        epoch=EpochEvidence(epoch_id=epoch_id(workspace,wh,epoch_start),warehouse_id=wh,workspace_id=workspace,account_id="acct-demo",warehouse_name=name,epoch_start=epoch_start,epoch_end=None,is_current=True,config=config,tags={"environment":"production" if i not in {9,11} else "nonprod","team":owners[i-1]},query=q,events=events,gaps=gaps,cost={"list_cost":round(baseline*0.88,2),"dbu_quantity":round(baseline/0.22,2),"cost_days":age_days,"active_cost_days":age_days,"currency":"USD"})
        forecast={"forecast_method":MODEL_VERSION,"active_cost_days":age_days,"forecast_basis_start":epoch_start[:10],"forecast_basis_end":"2026-09-30","run_rate_28":baseline*1.03,"run_rate_84":baseline*0.99,"zero_cost_days_in_basis":0,"trend_ratio":1.0,"trend_adjustment_factor":1.0}
        aws,aws_model=model_aws_baseline_cost(epoch,"us-east-1",analysis_time,forecast,registry)
        true_dbu=1000.0 if photon=="ENABLED" else 0.0
        false_dbu=1000.0 if photon=="DISABLED" else 0.0
        null_dbu=1000.0 if photon=="UNKNOWN" else 0.0
        coverage=0.0 if photon=="UNKNOWN" else 100.0
        enabled_pct=100.0 if photon=="ENABLED" else 0.0 if photon=="DISABLED" else None
        conf={"score":0.96 if photon!="UNKNOWN" else 0.25,"level":"HIGH" if photon!="UNKNOWN" else "LOW","components":{"feature_coverage":coverage/100.0,"freshness":1.0,"dbu_volume":1.0 if photon!="UNKNOWN" else 0.2,"state_purity":1.0 if photon!="UNKNOWN" else 0.0}}
        daily=_demo_daily(parse_datetime(epoch_start).date(),max(1,min(age_days,365)),round(baseline*0.88,2),workspace,wh,epoch_start)  # type: ignore[union-attr]
        billing={"photon_usage_pct_365":enabled_pct,"serverless_usage_pct_365":100.0 if wtype=="SERVERLESS" else 0.0,"cloud":"AWS","workspace_serverless_observed_from_system_compute_warehouses": wtype=="SERVERLESS","workspace_pro_classic_observed_from_system_compute_warehouses":wtype!="SERVERLESS"}
        e=WarehouseEvidence(warehouse_id=wh,workspace_id=workspace,account_id="acct-demo",warehouse_name=name,source_region="us-east-1",analysis_time=analysis_time,current_epoch_id=epoch.epoch_id,epochs=[epoch],current_cost_365=round(baseline*0.88,2),baseline_forecast_cost_365=float(baseline),currency="USD",cost_history_days=age_days,cost_cv_84=(None if age_days<Thresholds().forecast_cv_min_days else 0.18 if q30>=100 else 0.55),forecast_metadata=forecast,daily_cost_365=daily,billing_features=billing,aws_baseline_cost_365=aws,aws_cost_model=aws_model,sql_sku_prices={"PREMIUM_SQL_PRO_COMPUTE":{"effective_list_price":0.22,"currency_code":"USD","cloud":"AWS","usage_unit":"DBU","classification":"PRO"},"PREMIUM_SQL_SERVERLESS_COMPUTE":{"effective_list_price":0.70,"currency_code":"USD","cloud":"AWS","usage_unit":"DBU","classification":"SERVERLESS"}},compatibility_evidence={"mode":"B_READ_ONLY_API","status":"AVAILABLE","workspace_id":workspace,"enable_serverless_compute":True,"external_legacy_hive_metastore_detected":False,"external_legacy_hive_metastore_config_keys":[],"s3_access_policy_status":"UNKNOWN","enabled_warehouse_types":{"PRO":True},"source":"synthetic compatibility fixture"},photon_observation=PhotonObservation(status=photon,enabled_usage_pct=enabled_pct,known_feature_coverage_pct=coverage,true_dbu=true_dbu,false_dbu=false_dbu,null_dbu=null_dbu,latest_usage_time="2026-09-30T23:00:00+00:00",freshness_days=0,confidence=conf),attribution=WarehouseAttribution(owner=f"{owners[i-1]}@databricks.example",observed_owners=[f"{owners[i-1]}@databricks.example"],owner_source="system.billing.usage.identity_metadata.owned_by",query_source=derive_query_source_profile(q)))
        out.append(e)
    return out

def save_evidence(path:Path,evidence:Sequence[WarehouseEvidence])->None:
    path.write_text(json.dumps(as_serializable(evidence),indent=2,sort_keys=True)+"\n",encoding="utf-8")


def _epoch_from_dict(x:Mapping[str,Any])->EpochEvidence:
    return EpochEvidence(epoch_id=str(x["epoch_id"]),warehouse_id=str(x["warehouse_id"]),workspace_id=str(x.get("workspace_id") or ""),account_id=str(x.get("account_id") or ""),warehouse_name=str(x.get("warehouse_name") or x["warehouse_id"]),
                         epoch_start=str(x["epoch_start"]),epoch_end=x.get("epoch_end"),is_current=bool(x.get("is_current")),config=WarehouseConfig(**x["config"]),tags=dict(x.get("tags") or {}),query=dict(x.get("query") or {}),events=dict(x.get("events") or {}),gaps=dict(x.get("gaps") or {}),cost=dict(x.get("cost") or {}))


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
        # Evidence written before CV became Optional stored 0.0 for short histories; treat that as unknown.
        cost_cv_84=None if (x.get("cost_cv_84") is None or safe_int(x.get("cost_history_days")) < Thresholds().forecast_cv_min_days) else safe_float(x.get("cost_cv_84")),
        forecast_metadata=dict(x.get("forecast_metadata") or {}),
        daily_cost_365=list(x.get("daily_cost_365") or []),
        billing_features=dict(x.get("billing_features") or {}),
        aws_baseline_cost_365=(None if x.get("aws_baseline_cost_365") is None else safe_float(x.get("aws_baseline_cost_365"))),
        aws_cost_model=dict(x.get("aws_cost_model") or {}),
        sql_sku_prices=dict(x.get("sql_sku_prices") or {}),
        compatibility_evidence=dict(x.get("compatibility_evidence") or {}),
        photon_observation=(PhotonObservation(**x["photon_observation"]) if x.get("photon_observation") else None),
        attribution=(WarehouseAttribution(
            owner=(x.get("attribution") or {}).get("owner"),
            observed_owners=list((x.get("attribution") or {}).get("observed_owners") or []),
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
        payload = json.loads(Path(filename).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise SystemExit(f"Evidence file must contain a list: {filename}")
        for item in payload:
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
    p=argparse.ArgumentParser(description=f"Deterministic Databricks SQL Warehouse optimizer v{PUBLISHER_VERSION} (Reporter Contract v{rc.CONTRACT_VERSION} + Workbook Contract v1.3.0)")
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
    p.add_argument("--source-region",default=os.getenv("DATABRICKS_REGION","unknown"))
    default_registry=str(Path(__file__).resolve().parent / "pricing" / "aws_ec2_on_demand.json")
    p.add_argument("--aws-price-registry",default=os.getenv("AWS_PRICE_REGISTRY",default_registry))
    p.add_argument("--batch-size",type=int,default=int(os.getenv("WAREHOUSE_BATCH_SIZE","100")),help="Downstream analysis/report batch size; collector SQL is set-based and aggregated in Databricks")
    p.add_argument("--compatibility-api-mode",choices=("auto","off"),default=os.getenv("COMPATIBILITY_API_MODE","auto"),help="auto=B read-only workspace compatibility API when available, else A HITL fallback")
    p.add_argument("--log-level",choices=("DEBUG","INFO","WARNING","ERROR"),default=os.getenv("PUBLISHER_LOG_LEVEL","INFO"))
    p.add_argument("--demo-count",type=int,default=15,help="Synthetic collector warehouse count for --demo (1..15)")
    p.add_argument("--no-workbook",action="store_true",help="Skip workbook.xlsx generation; locked JSON packages are still emitted")
    p.add_argument("--print-json",action="store_true")
    return p.parse_args(argv)


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
        if not (1 <= args.demo_count <= 15):
            raise SystemExit("--demo-count must be between 1 and 15")
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
            evidence=extract_evidence(client,t,args.source_region,registry,compat)
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
        print(f"Analyzed {len(evidence)-excluded_count} warehouse(s); emitted {len(packages)} locked Reporter Contract v1.2.0 package(s), {candidate_count} candidates, and {scenario_count} scenarios; excluded {excluded_count} unsupported warehouse(s) (see manifest).")
        for k,v in files.items(): print(f"{k}: {v}")
        print(f"log: {log_path}")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
