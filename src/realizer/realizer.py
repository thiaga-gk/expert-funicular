#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse Realizer v1.1.0: the facade (the CLI is ``realizer.cli``).

    Analyzer -> Governor -> Reconciler -> Estimator -> Reviewer -> Packager -> Reporter

``realize_request`` orchestrates the capabilities and nothing else: normalization, policy, matching and lifecycle,
realized-savings estimation, review, the ledger and the snapshot, and the files written live in their packages
(docs/architecture/realizer/repo-reorg-design.md). The inputs (package, observation, replay cut) are checked before
anything is written (``build_context``); the run then holds the lock of its ledger (``packager.lock``) until the
Reporter has written, and records the lineage entry first (``packager.begin``). The Governor, the Reconciler, the Estimator, the Reviewer and the Packager
never lose a failure: each keeps it as a ``halt`` with the facts decided before it, and the Packager records those and
raises it, so a failed run leaves its half-written ledger. The Estimator and the Reviewer return an empty result when no
validation is due.

The engine never mutates a RecommendationPackage (PKG-*): it appends immutable ledger facts (RLE-*) and projects a
separate RealizationPackage (RPKG-*) / snapshot (RSNAP-*). The execution path is replay/offline friendly: a
validated PKG plus a normalized post-change observation is enough to exercise the entire application -> validation
-> economics -> attribution -> regeneration loop. Live Databricks collection can feed the same normalized
observation contract.

The facade re-exports only what the CLI needs: the policy type, the demo, live and replay observation sources and the
backtest entry points. Everything else is imported from its capability package.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from realizer.analyzer import analyzer
from realizer.analyzer.backtest import summary_lines as backtest_summary
from realizer.analyzer.collector.backtest_collector import backtest_demo, backtest_from_evidence, backtest_live
from realizer.analyzer.collector.collector import load_observation, measure_live, observe_from_evidence
from realizer.analyzer.collector.demo_world import measure_demo
from realizer.config.loader import validate_observation, validate_package, validate_replay_through
from realizer.contracts import realization as rc
from realizer.contracts.realization import make_rpkg_id
from realizer.contracts.report import RealizationReport
from realizer.contracts.request import RealizationRequest, RunContext
from realizer.estimator import estimator
from realizer.governor import governor
from realizer.governor.policy import RealizerPolicy
from realizer.packager import packager
from realizer.reconciler import reconciler
from realizer.reporter import reporter
from realizer.reviewer import reviewer

__all__ = ["RealizerPolicy", "backtest_demo", "backtest_from_evidence", "backtest_live", "backtest_summary", "build_context",
           "load_observation", "measure_demo", "measure_live", "observe_from_evidence", "realize", "realize_package", "realize_request"]

# ``RealizerPolicy``, ``load_observation``, ``measure_demo``, ``measure_live`` and ``observe_from_evidence`` are re-exported for
# the CLI, which reaches the core only here; so are the backtest entry points (``backtest_live``, ``backtest_from_evidence``,
# ``backtest_demo`` and ``backtest_summary``), which use no package and run no capability pipeline.


def build_context(request: RealizationRequest) -> RunContext:
    """Validate the source package and the observation (before anything is written), apply the policy default and mint
    the realization package id."""
    validate_package(request.package)
    if request.observation:
        validate_observation(request.observation, request.package)
    validate_replay_through(request.replay_through, request.package)
    policy = request.policy or RealizerPolicy()
    return RunContext(request=request, policy=policy, realization_package_id=make_rpkg_id(request.package["recommendation_package_id"]))


def realize_request(request: RealizationRequest) -> RealizationReport:
    """One realize run: every capability once, in order, returning what the Reporter wrote.

    Side effects are the Packager's (the ledger, the evidence and regeneration files, under the state directory
    ``request.state_root``) and the Reporter's (the realization package and the snapshot under ``request.output_dir``,
    the state index under the state directory), made while the run holds the lock of its ledger. This function
    sequences the capabilities and holds no domain logic.
    """
    context = build_context(request)
    with packager.lock(context):
        packager.begin(context)
        analysis = analyzer.run(context)
        governance = governor.run(context, analysis)
        reconciliation = reconciler.run(context, analysis, governance)
        estimate = estimator.run(context, analysis, governance, reconciliation)
        reviewed = reviewer.run(context, analysis, governance, reconciliation, estimate)
        packaged = packager.run(context, reviewed)
        return reporter.run(context, packaged)


def realize_package(request: RealizationRequest) -> RealizationReport:
    """One CLI realize run of one package: ``realize_request`` while holding the lock of the package's warehouse in the
    state (``packager.warehouse_lock``), so it never runs alongside a fleet run or another CLI run of that warehouse."""
    build_context(request)  # the inputs are refused before the lock file is written
    warehouse = request.package["warehouse"]
    with packager.warehouse_lock(request.state_root, warehouse["workspace_id"], warehouse["warehouse_id"]):
        return realize_request(request)


def realize(
    package: Mapping[str, Any], observation: Optional[Mapping[str, Any]], output_dir: Path,
    *, policy: Optional[RealizerPolicy] = None, replay_through: Optional[str] = None, state_dir: Optional[Path] = None,
) -> rc.RealizationPackage:
    """Positional entry point over ``realize_request`` (the signature of the single-file release), returning the snapshot.
    *state_dir* keeps the history apart from the projections (``None``: everything under *output_dir*)."""
    return realize_request(RealizationRequest(
        package=package, observation=observation, output_dir=output_dir, policy=policy, replay_through=replay_through,
        state_dir=state_dir,
    )).realization


if __name__ == "__main__":
    from realizer.cli.main import main as _main
    raise SystemExit(_main())
