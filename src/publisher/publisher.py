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


def review_and_plan(optimization: OptimizationResult, thresholds: Thresholds) -> List[WarehouseOptimization]:
    """Reviewer candidate gate, scenario planning and the scenario gate for each optimized warehouse, in order."""
    results: List[WarehouseOptimization] = []
    for w in optimization.warehouses:
        e, candidates = w.evidence, w.candidates
        reviewer = Reviewer(thresholds)
        planner = ScenarioPlanner(thresholds)
        reviewer.review(e, candidates)
        scenarios, recommended = planner.scenarios(e, candidates)
        reviewer.review_scenarios(e, candidates, scenarios, recommended)
        status_counts: Dict[str, int] = {}
        for candidate in candidates:
            status_counts[candidate.status] = status_counts.get(candidate.status, 0) + 1
        LOGGER.info(
            "warehouse.decisions workspace_id=%s warehouse_id=%s statuses=%s scenarios=%d recommended_scenario=%s",
            e.workspace_id, e.warehouse_id, canonical_json(status_counts), len(scenarios), recommended or "NONE",
        )
        results.append(WarehouseOptimization(e.warehouse_id, e.workspace_id, e, candidates, scenarios, recommended))
    return results


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
    return review_and_plan(optimizer.optimize_eligible(governance.eligible, thresholds, batch_size), thresholds)


def evaluate_warehouses(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100
) -> List[WarehouseOptimization]:
    """Analyze and evaluate warehouses from evidence already in hand (the CLI runs analyzer.run first)."""
    return evaluate_analysis(FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds)), thresholds, batch_size)


def build_packages_from_analysis(
    analysis: FleetAnalysis, thresholds: Thresholds, batch_size: int = 100, governance: Optional[GovernanceResult] = None
) -> List[rc.RecommendationPackage]:
    """Run the deterministic decision pipeline on an analyzed fleet and directly assemble locked public packages."""
    return build_packages(analysis, evaluate_analysis(analysis, thresholds, batch_size, governance), thresholds)


def build_packages(
    analysis: FleetAnalysis, optimizations: Sequence[WarehouseOptimization], thresholds: Thresholds
) -> List[rc.RecommendationPackage]:
    """Assemble one locked public package per reviewed, planned warehouse."""
    evidence = analysis.evidence
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
    log_pipeline_start(analysis,governance,args.batch_size)
    optimization=optimizer.run(context,analysis,governance)
    packages=build_packages(analysis,review_and_plan(optimization,t),t)
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
