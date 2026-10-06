#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse Realizer v1.0.0: the facade (the CLI is ``realizer.cli``).

    Analyzer -> Governor -> Reconciler -> Estimator -> Reviewer -> Packager -> Reporter

``realize_request`` orchestrates the capabilities and nothing else: normalization, policy, matching and lifecycle,
realized-savings estimation, review, the ledger and the snapshot, and the files written live in their packages
(docs/architecture/realizer/repo-reorg-design.md). The lineage entry is recorded first (``packager.begin``): a malformed
observation fails in the Analyzer after it. The Governor, the Reconciler, the Estimator, the Reviewer and the Packager
never lose a failure: each keeps it as a ``halt`` with the facts decided before it, and the Packager records those and
raises it, so a failed run leaves its half-written ledger. The Estimator and the Reviewer return an empty result when no
validation is due.

The engine never mutates a RecommendationPackage (PKG-*): it appends immutable ledger facts (RLE-*) and projects a
separate RealizationPackage (RPKG-*) / snapshot (RSNAP-*). The execution path is replay/offline friendly: a
validated PKG plus a normalized post-change observation is enough to exercise the entire application -> validation
-> economics -> attribution -> regeneration loop. Live Databricks collection can feed the same normalized
observation contract.

The facade re-exports only what the CLI needs: the policy type and the demo and replay observation loaders. Everything
else is imported from its capability package.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from realizer.analyzer import analyzer
from realizer.analyzer.collector.collector import demo_observation, load_observation
from realizer.config.loader import validate_package
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

# ``RealizerPolicy``, ``demo_observation`` and ``load_observation`` are re-exported for the CLI, which reaches the core only here.
__all__ = ["RealizerPolicy", "build_context", "demo_observation", "load_observation", "realize", "realize_request"]


def build_context(request: RealizationRequest) -> RunContext:
    """Validate the source package, apply the policy default and mint the realization package id."""
    validate_package(request.package)
    policy = request.policy or RealizerPolicy()
    return RunContext(request=request, policy=policy, realization_package_id=make_rpkg_id(request.package["recommendation_package_id"]))


def realize_request(request: RealizationRequest) -> RealizationReport:
    """One realize run: every capability once, in order, returning what the Reporter wrote.

    Side effects are the Packager's (the ledger, the evidence and regeneration files) and the Reporter's (the realization
    package and the snapshot) under ``request.output_dir``. This function sequences the capabilities and holds no domain
    logic.
    """
    context = build_context(request)
    packager.begin(context)
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    reconciliation = reconciler.run(context, analysis, governance)
    estimate = estimator.run(context, analysis, governance, reconciliation)
    reviewed = reviewer.run(context, analysis, governance, reconciliation, estimate)
    packaged = packager.run(context, reviewed)
    return reporter.run(context, packaged)


def realize(
    package: Mapping[str, Any], observation: Optional[Mapping[str, Any]], output_dir: Path,
    *, policy: Optional[RealizerPolicy] = None, replay_through: Optional[str] = None,
) -> rc.RealizationPackage:
    """Positional entry point over ``realize_request`` (the signature of the single-file release), returning the snapshot."""
    return realize_request(RealizationRequest(
        package=package, observation=observation, output_dir=output_dir, policy=policy, replay_through=replay_through,
    )).realization


if __name__ == "__main__":
    from realizer.cli.main import main as _main
    raise SystemExit(_main())
