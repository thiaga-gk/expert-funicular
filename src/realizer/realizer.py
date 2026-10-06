#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse Realizer v1.0.0: the facade (the CLI is ``realizer.cli``).

    Analyzer -> Governor -> Reconciler -> Estimator -> Reviewer -> Packager -> Reporter

``realize_request`` orchestrates the capabilities and nothing else: normalization, policy, matching and lifecycle,
realized-savings estimation, review, the ledger and the snapshot, and the files written live in their packages
(docs/architecture/realizer/repo-reorg-design.md). The lineage entry is recorded first (``packager.begin``): a malformed
observation fails in the Analyzer after it. The Governor, the Reconciler, the Estimator, the Reviewer and the Packager
never lose a failure: each keeps it as a ``halt`` with the facts decided before it, and the Packager records those and
raises it, so a failed run leaves the ledger the single-file Realizer left. The Estimator and the Reviewer return an empty
result when no validation is due.

The engine never mutates a RecommendationPackage (PKG-*): it appends immutable ledger facts (RLE-*) and projects a
separate RealizationPackage (RPKG-*) / snapshot (RSNAP-*). The execution path is replay/offline friendly: a
validated PKG plus a normalized post-change observation is enough to exercise the entire application -> validation
-> economics -> attribution -> regeneration loop. Live Databricks collection can feed the same normalized
observation contract.

Behavior is that of the frozen single-file oracle (``legacy/realizer_single_file/realizer.py``). Every name the
single-file engine had stays importable from here (``realizer.realizer.<name>``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from realizer.analyzer import analyzer
from realizer.config.loader import validate_package_shape
from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import ReviewResult, make_rpkg_id
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.report import RealizationReport
from realizer.contracts.request import RealizationRequest, RunContext
from realizer.estimator import estimator
from realizer.governor import governor
from realizer.governor.policy import RealizerPolicy
from realizer.packager import packager
from realizer.reconciler import reconciler
from realizer.reporter import reporter
from realizer.reviewer import reviewer

# The engine keeps every name it had; the extracted ones stay importable from here (``realizer.realizer.<name>``).
from realizer.analyzer.analysis import normalize_scalar, observed_config, photon_bool_from_package, recommended_scenario  # noqa: F401,E402
from realizer.governor.policy import counterfactual_eligible, policy_to_dict, savings_improvement_material, validate_performance  # noqa: F401,E402
from realizer.packager.ledger import (  # noqa: F401,E402
    append_if_absent, append_regeneration, latest_event, ledger_business_key, ledger_checkpoint, ledger_path, load_ledger,
    make_ledger_entry,
)
from realizer.packager.packager import request_regeneration  # noqa: F401,E402
from realizer.packager.snapshot import project_snapshot  # noqa: F401,E402
from realizer.common.utils import load_json  # noqa: F401,E402
from realizer.config.settings import CONFIG_KNOBS, SIZE_ORDER  # noqa: F401,E402
from realizer.contracts.realization import make_app_id, make_evidence_id  # noqa: F401,E402
from realizer.reconciler.matching import _directional_relation, choose_application, compare_step  # noqa: F401,E402
from realizer.estimator.economics import calculate_economics, expected_annual_for_exact_step, make_cf_id  # noqa: F401,E402
from realizer.estimator.attribution import attribution_result, authoritative_cumulative_realization  # noqa: F401,E402
# (and the names the engine imported for them before they moved)
import argparse  # noqa: F401,E402
import copy  # noqa: F401,E402
import datetime as dt  # noqa: F401,E402  (the engine keeps its module attributes; tests use realizer.dt)
import json  # noqa: F401,E402
import os  # noqa: F401,E402
from decimal import Decimal  # noqa: F401,E402
from typing import Dict, List, Sequence, Tuple  # noqa: F401,E402
from realizer.analyzer.analysis import candidate_by_id, candidate_steps, config_hash, package_config, scenario_by_id  # noqa: F401,E402
from realizer.analyzer.collector.collector import demo_observation, load_observation  # noqa: F401,E402
from realizer.common.logging import LOGGER, setup_logging  # noqa: F401,E402
from realizer.common.utils import decimal, money, ratio_string, utc_iso, write_json  # noqa: F401,E402
from realizer.config.loader import load_package, load_policy_mapping  # noqa: F401,E402
from realizer.config.settings import (  # noqa: F401,E402
    ANALYZER_GATEWAY_VERSION, ATTRIBUTION_VERSION, ESTIMATOR_GATEWAY_VERSION, EXCLUSIVITY_VERSION, MODELER_GATEWAY_VERSION,
    POLICY_VERSION, PRODUCT_VERSION, PROJECTOR_VERSION, REALIZER_VERSION,
)
from realizer.contracts.realization import RealizationEstimate, make_measurement_id, make_val_id  # noqa: F401,E402
from realizer.contracts.reconciliation import RegenerationRequest  # noqa: F401,E402
from realizer.governor.policy import authority_eligible, economic_quality_result, readiness  # noqa: F401,E402
from realizer.reconciler.lifecycle import regeneration_request  # noqa: F401,E402
from realizer.reporter.reporter import legacy_compatibility  # noqa: F401,E402

# ---------------------------------------------------------------------------
# Realization orchestration
# ---------------------------------------------------------------------------


def build_context(request: RealizationRequest) -> RunContext:
    """Validate the source package, apply the policy default and mint the realization package id."""
    validate_package_shape(request.package)
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
    """The single-file Realizer's entry point: ``realize_request`` over the same inputs, returning the snapshot."""
    return realize_request(RealizationRequest(
        package=package, observation=observation, output_dir=output_dir, policy=policy, replay_through=replay_through,
    )).realization


def review(
    context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult, reconciliation: ReconciliationResult,
) -> ReviewResult:
    """The Estimator's and the Reviewer's stages as one call (each returns an empty result when validation is not due).
    ``realize_request`` calls the stages itself; this helper only keeps the engine's name for callers that use it."""
    return reviewer.run(context, analysis, governance, reconciliation, estimator.run(context, analysis, governance, reconciliation))


def __getattr__(name: str) -> Any:
    """``parse_args`` and ``main`` stay importable from here; they live in ``realizer.cli`` (which imports this module)."""
    if name == "parse_args":
        from realizer.cli.realize import parse_args
        return parse_args
    if name == "main":
        from realizer.cli.main import main
        return main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    from realizer.cli.main import main as _main
    raise SystemExit(_main())
