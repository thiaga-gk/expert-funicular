#!/usr/bin/env python3
"""Deterministic, read-only Databricks SQL Warehouse optimizer: the publisher facade (the CLI is publisher.cli).

    Analyzer -> Governor -> Optimizer -> Reviewer (candidate gate) -> Packager -> Reviewer (package gate) -> Reporter

``publish`` orchestrates the capabilities and nothing else: SQL, policy, decisions, estimation, scenarios, review
rules and rendering live in their packages (docs/architecture/repo-reorg-design.md). The runtime is read-only and has
no LLM calls; identical normalized evidence and policy/model/contract versions produce identical packages.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from publisher.analyzer import analyzer
from publisher.analyzer.collector.demo import DEMO_FLEET
from publisher.common.logging import LOGGER
from publisher.config.cost_profiles import parse_cost_profile_flags
from publisher.config.loader import resolve_execution_mode, resolve_scope
from publisher.contracts import reporter_contract as rc
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.report import ReportContract
from publisher.contracts.request import MODE_REPLAY, PublisherRequest, RunContext
from publisher.contracts.review import CandidateReviewResult, WarehouseOptimization
from publisher.contracts.scope import RunScope
from publisher.contracts.source import WarehouseEvidence
from publisher.governor import governor
from publisher.governor.thresholds import Thresholds
from publisher.optimizer import optimizer
from publisher.packager import packager
from publisher.reporter import reporter
from publisher.reviewer import reviewer


def build_context(request: PublisherRequest, thresholds: Optional[Thresholds] = None, scope: Optional[RunScope] = None) -> RunContext:
    """The immutable context every capability receives: the request, the policy thresholds (defaults when None) and
    the run scope (resolved from the request's scope inputs when None; no inputs is the full scope).

    Raises PublisherError when the request's execution mode, its --cost-profile flags or its scope inputs are invalid
    (all checked by the config layer before any evidence is collected).
    """
    resolve_execution_mode(request.execution_mode)
    parse_cost_profile_flags(request.cost_profiles)
    if scope is None:
        scope = resolve_scope(request.scope_specs, request.scope_file, request.warehouses)
    return RunContext(request=request, thresholds=thresholds if thresholds is not None else Thresholds(), scope=scope)


def demo_fleet_size() -> int:
    """Simulated warehouses in the demo fleet (one per golden scenario); the CLI's --demo-count default and bound."""
    return len(DEMO_FLEET)


def _log_pipeline_start(analysis: FleetAnalysis, governance: GovernanceResult, batch_size: int) -> None:
    """Log the fleet size once and a WARNING per warehouse the governor excluded (identity and reason code only)."""
    LOGGER.info("pipeline.start warehouses=%d batch_size=%d", len(analysis.evidence), batch_size)
    for excluded in governance.exclusions:
        LOGGER.warning(
            "warehouse.excluded workspace_id=%s warehouse_id=%s warehouse_type=%s reason=%s",
            excluded["workspace_id"], excluded["warehouse_id"], excluded["warehouse_type"], excluded["reason_code"],
        )


def publish(request: PublisherRequest) -> ReportContract:
    """One deterministic publisher run: every capability once, in order, returning what the Reporter wrote.

    Side effects are the Reporter's: packages/, evidence.json, manifest.json and (unless disabled) workbook.xlsx under
    ``request.output_dir``. Raises PublisherError for a refused run (bad input, unusable source, policy refusal); the CLI
    maps that to exit status 2. This function sequences the capabilities and holds no domain logic.
    """
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
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int, cost_profiles: Sequence[str],
    scope: Optional[RunScope] = None,
) -> Tuple[RunContext, FleetAnalysis, GovernanceResult, CandidateReviewResult]:
    """Evidence already in hand (a replay) through the Analyzer, Governor, Optimizer and the candidate gate.

    Shared by the in-memory entry points below; writes nothing (the request's output_dir is a placeholder).
    ``cost_profiles`` are raw --cost-profile flags; without them every warehouse gets its tag default. ``scope`` None is
    the full scope.
    """
    request = PublisherRequest(mode=MODE_REPLAY, output_dir=Path("."), batch_size=batch_size, cost_profiles=tuple(cost_profiles))
    context = build_context(request, thresholds, scope)
    analysis = FleetAnalysis(list(evidence), analyzer.analyze(evidence, thresholds, parse_cost_profile_flags(request.cost_profiles)))
    governance = governor.govern(analysis, context.scope)
    _log_pipeline_start(analysis, governance, batch_size)
    optimization = optimizer.run(context, analysis, governance)
    return context, analysis, governance, reviewer.review_candidates(context, analysis, governance, optimization)


def evaluate_warehouses(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100, *, cost_profiles: Sequence[str] = (),
    scope: Optional[RunScope] = None,
) -> List[WarehouseOptimization]:
    """Evidence in hand through both Reviewer gates, without assembling public packages."""
    context, _, _, reviewed = _reviewed_candidates(evidence, thresholds, batch_size, cost_profiles, scope)
    optimizations = packager.plan(reviewed, thresholds, context.scope)
    reviewer.review_plans(context, optimizations)
    return optimizations


def build_recommendation_packages(
    evidence: Sequence[WarehouseEvidence], thresholds: Thresholds, batch_size: int = 100, *, cost_profiles: Sequence[str] = (),
    scope: Optional[RunScope] = None,
) -> List[rc.RecommendationPackage]:
    """Evidence in hand to reviewed public packages, without writing anything."""
    context, analysis, governance, reviewed = _reviewed_candidates(evidence, thresholds, batch_size, cost_profiles, scope)
    return reviewer.review_package(context, analysis, governance, packager.run(context, analysis, governance, reviewed)).packages
