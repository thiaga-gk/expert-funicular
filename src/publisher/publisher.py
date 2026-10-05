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
from publisher.optimizer import optimizer
from publisher.reviewer import reviewer
from publisher.packager import packager
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


def log_pipeline_start(analysis: FleetAnalysis, governance: GovernanceResult, batch_size: int) -> None:
    LOGGER.info("pipeline.start warehouses=%d batch_size=%d", len(analysis.evidence), batch_size)
    for excluded in governance.exclusions:
        LOGGER.warning(
            "warehouse.excluded workspace_id=%s warehouse_id=%s warehouse_type=%s reason=%s",
            excluded["workspace_id"], excluded["warehouse_id"], excluded["warehouse_type"], excluded["reason_code"],
        )


def evaluate_analysis(
    analysis: FleetAnalysis, thresholds: Thresholds, batch_size: int = 100, governance: Optional[GovernanceResult] = None
) -> List[WarehouseOptimization]:
    """Evaluate the eligible warehouses through both reviewer gates without constructing reporter objects."""
    governance = governance if governance is not None else governor.govern(analysis)
    log_pipeline_start(analysis, governance, batch_size)
    # Evidence already in hand is a replay: the context carries only the policy and the batch size.
    context = RunContext(PublisherRequest(mode=MODE_REPLAY, output_dir=Path("."), batch_size=batch_size), thresholds)
    optimization = optimizer.run(context, analysis, governance)
    optimizations = packager.plan(reviewer.review_candidates(context, analysis, governance, optimization), thresholds)
    for w in optimizations:
        Reviewer(thresholds).review_scenarios(w.evidence, w.candidates, w.scenarios, w.recommended_scenario_id)
    return optimizations


def evaluate_warehouses(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[WarehouseOptimization]:
    """Analyze and evaluate warehouses from evidence already in hand (the CLI runs analyzer.run first)."""
    return evaluate_analysis(FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds)), thresholds, batch_size)


def build_packages_from_analysis(
    analysis: FleetAnalysis, thresholds: Thresholds, batch_size: int = 100, governance: Optional[GovernanceResult] = None
) -> List[rc.RecommendationPackage]:
    """Run the deterministic decision pipeline on an analyzed fleet and directly assemble locked public packages."""
    return packager.assemble(analysis, evaluate_analysis(analysis, thresholds, batch_size, governance), thresholds)


def build_recommendation_packages(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[rc.RecommendationPackage]:
    """Analyze evidence already in hand and assemble locked public packages (the CLI runs analyzer.run first)."""
    return build_packages_from_analysis(FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds)), thresholds, batch_size)


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
    log_pipeline_start(analysis,governance,args.batch_size)
    optimization=optimizer.run(context,analysis,governance)
    reviewed=reviewer.review_candidates(context,analysis,governance,optimization)
    packaging=packager.run(context,analysis,governance,reviewed)
    packages=reviewer.review_package(context,analysis,governance,packaging).packages
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
