#!/usr/bin/env python3
"""Deterministic, read-only Databricks SQL Warehouse optimizer: the publisher facade.

    Analyzer -> Governor -> Optimizer -> Reviewer (candidate gate) -> Packager -> Reviewer (package gate) -> Reporter

``publish`` orchestrates the capabilities and nothing else: SQL, policy, decisions, estimation, scenarios, review
rules and rendering live in their packages (docs/architecture/repo-reorg-design.md). The runtime is read-only and has
no LLM calls; identical normalized evidence and policy/model/contract versions produce identical packages.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from publisher.analyzer import analyzer
from publisher.common.logging import LOGGER, setup_logging
from publisher.config.loader import parse_args, request_from_args
from publisher.config.versions import ANALYZER_VERSION, BUNDLE_MODEL_VERSION, ESTIMATOR_VERSION, OPTIMIZER_VERSION, POLICY_VERSION
from publisher.contracts import reporter_contract as rc
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.report import ReportContract
from publisher.contracts.request import MODE_REPLAY, PublisherRequest, RunContext
from publisher.contracts.review import CandidateReviewResult, WarehouseOptimization
from publisher.contracts.source import WarehouseEvidence
from publisher.governor import governor
from publisher.governor.thresholds import Thresholds
from publisher.optimizer import optimizer
from publisher.packager import packager
from publisher.reporter import reporter
from publisher.reviewer import reviewer

# Transitional: names re-exported for tests that still read them through the facade.
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
from publisher.optimizer import optimizer
from publisher.reviewer import reviewer
from publisher.packager import packager
from publisher.reporter import reporter
from publisher.contracts.review import CandidateReviewResult
from publisher.contracts.request import MODE_REPLAY, PublisherRequest
from publisher.contracts.candidate import OptimizationResult
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
    ACTIVE_KNOBS, AWS_SQL_WAREHOUSE_TOPOLOGY, GIB, SIZE_ORDER, SQL_DBU_PER_CLUSTER_HOUR, SUPPORTED_WAREHOUSE_TYPES,
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
from publisher.governor.thresholds import (  # noqa: F401
    Thresholds,
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
from publisher.optimizer.optimizer import (  # noqa: F401
    Optimizer,
)
from publisher.optimizer.techniques.size import (  # noqa: F401
    next_size, previous_size,
)
from publisher.optimizer.techniques.scaling import (  # noqa: F401
    MAX_INCREASE_COST_BOUNDS,
)
from publisher.optimizer.estimator.cost import (  # noqa: F401
    aws_cluster_hourly_from_evidence, resolve_target_sku_price, validated_size_rate_ratio,
)
from publisher.optimizer.estimator.candidate import (  # noqa: F401
    Estimator,
)
from publisher.reviewer.reviewer import (  # noqa: F401
    Reviewer,
)
from publisher.contracts.scenario import (  # noqa: F401
    PRICING_AUTHORITATIVE, PRICING_CONTINGENT,
)
from publisher.packager.estimator.state_transition import (  # noqa: F401
    _ChainState,
)
from publisher.packager.estimator.scenario import (  # noqa: F401
    StatefulBundleEstimator,
)
from publisher.packager.scenarios import (  # noqa: F401
    ScenarioPlanner,
)
from publisher.packager.sequencing import (  # noqa: F401
    PLAN_KNOB_RANK, _canary_sequencing_note, _rollback_knobs, _rollback_sequencing_note,
)
from publisher.packager.execution_plan import (  # noqa: F401
    _recommended_plan,
)
from publisher.packager.selection import (  # noqa: F401
    _overall_status,
)
from publisher.packager.estimator.package import (  # noqa: F401
    _economics_point_from_scenario, _package_economics,
)
from publisher.packager.evidence import (  # noqa: F401
    _add_evidence, _build_evidence_registry,
)
from publisher.packager.adapter import (  # noqa: F401
    _CLIENT_QUERY_SOURCE_CATEGORIES, _CONTRACT_EXECUTION_STATE, _ENTITY_QUERY_SOURCE_CATEGORIES, _actual_cost_breakdown, _assessment_contract, _candidate_contracts, _compatibility_contract, _contract_component_ranges, _contract_confidence, _contract_range, _dependency_refs, _estimation_status, _forecast_confidence, _forward_cost_breakdown, _objectives, _photon_config_observation, _recommendation_status, _scalar_config, _scenario_contracts, _scenario_estimation_status, _step_projection, _technique_id, _tunable_knobs, _workload_profile, compute_reevaluation, derive_observed_change,
)
from publisher.packager.packager import (  # noqa: F401
    _build_locked_recommendation_package,
)
from publisher.reviewer.package import (  # noqa: F401
    validate_locked_package_schema,
)
from publisher.reporter.json_reporter import (  # noqa: F401
    _safe_artifact_name, package_locked_contract,
)


# ---------------------------------------------------------------------------
# System-table SQL
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Utilities / auth / collection
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Analyzer: facts and evidence only; no config recommendations.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Optimizer: config decisions only. No cost arithmetic.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Estimator: independent candidate counterfactuals, then stateful bundle.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Locked Reporter JSON Contract adapter / canonical JSON packaging
# ---------------------------------------------------------------------------


def build_context(request: PublisherRequest, thresholds: Optional[Thresholds] = None) -> RunContext:
    return RunContext(request=request, thresholds=thresholds if thresholds is not None else Thresholds())


def _log_pipeline_start(analysis: FleetAnalysis, governance: GovernanceResult, batch_size: int) -> None:
    LOGGER.info("pipeline.start warehouses=%d batch_size=%d", len(analysis.evidence), batch_size)
    for excluded in governance.exclusions:
        LOGGER.warning(
            "warehouse.excluded workspace_id=%s warehouse_id=%s warehouse_type=%s reason=%s",
            excluded["workspace_id"], excluded["warehouse_id"], excluded["warehouse_type"], excluded["reason_code"],
        )


def publish(request: PublisherRequest) -> ReportContract:
    """One deterministic publisher run: every capability once, in order."""
    context = build_context(request)
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    _log_pipeline_start(analysis, governance, request.batch_size)
    optimization = optimizer.run(context, analysis, governance)
    reviewed_candidates = reviewer.review_candidates(context, analysis, governance, optimization)
    package = packager.run(context, analysis, governance, reviewed_candidates)
    reviewed_package = reviewer.review_package(context, analysis, governance, package)
    return reporter.run(context, analysis, governance, reviewed_package)


def _reviewed_candidates(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int
) -> Tuple[RunContext, FleetAnalysis, GovernanceResult, CandidateReviewResult]:
    """Evidence already in hand (a replay) through the Analyzer, Governor, Optimizer and the candidate gate."""
    context = build_context(PublisherRequest(mode=MODE_REPLAY, output_dir=Path("."), batch_size=batch_size), thresholds)
    analysis = FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds))
    governance = governor.govern(analysis)
    _log_pipeline_start(analysis, governance, batch_size)
    optimization = optimizer.run(context, analysis, governance)
    return context, analysis, governance, reviewer.review_candidates(context, analysis, governance, optimization)


def evaluate_warehouses(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[WarehouseOptimization]:
    """Evidence in hand through both Reviewer gates, without assembling public packages."""
    context, _, _, reviewed = _reviewed_candidates(evidence, thresholds, batch_size)
    optimizations = packager.plan(reviewed, thresholds)
    reviewer.review_plans(context, optimizations)
    return optimizations


def build_recommendation_packages(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[rc.RecommendationPackage]:
    """Evidence in hand to reviewed public packages, without writing anything."""
    context, analysis, governance, reviewed = _reviewed_candidates(evidence, thresholds, batch_size)
    return reviewer.review_package(context, analysis, governance, packager.run(context, analysis, governance, reviewed)).packages


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: python -m publisher."""
    args = parse_args(argv)
    log_path = setup_logging(Path(args.output_dir), args.log_level)
    LOGGER.info("policy.versions policy=%s analyzer=%s optimizer=%s estimator=%s bundle=%s",
                POLICY_VERSION, ANALYZER_VERSION, OPTIMIZER_VERSION, ESTIMATOR_VERSION, BUNDLE_MODEL_VERSION)
    report = publish(request_from_args(args))
    LOGGER.info("publisher.done warehouses=%d excluded=%d packages=%d candidates=%d scenarios=%d log=%s",
                report.analyzed_count, report.excluded_count, len(report.packages), report.candidate_count, report.scenario_count, log_path)
    reporter.print_summary(report, log_path, print_json=args.print_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
