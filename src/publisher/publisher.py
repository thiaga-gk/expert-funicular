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
