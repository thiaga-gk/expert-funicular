#!/usr/bin/env python3
"""Deterministic Databricks SQL Warehouse Realizer v1.0.0: the engine.

Working copy of the frozen single-file oracle (``legacy/realizer_single_file/realizer.py``) with the same behavior.
The realizer reorganization (docs/architecture/realizer-repo-reorg-design.md, section 22) moves its parts into the
capability subpackages phase by phase; so far the contracts (``realizer.contracts``), settings and loading
(``realizer.config``), the shared helpers (``realizer.common``), the Analyzer (``realizer.analyzer``), the Governor
(``realizer.governor``), the Reconciler (``realizer.reconciler``), the Estimator (``realizer.estimator``), the Reviewer
(``realizer.reviewer``) and the Packager (``realizer.packager``) are extracted, and this module imports them. ``realize``
records the lineage, runs the capabilities in order and hands their results to the Packager, which records the events
and projects the snapshot; the engine then writes the realization package and the snapshot file.

The engine never mutates a RecommendationPackage (PKG-*): it appends immutable ledger facts (RLE-*) and projects a
separate RealizationPackage (RPKG-*) / snapshot (RSNAP-*). The execution path is replay/offline friendly: a
validated PKG plus a normalized post-change observation is enough to exercise the entire application -> validation
-> economics -> attribution -> regeneration loop. Live Databricks collection can feed the same normalized
observation contract.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from realizer.analyzer import analyzer
from realizer.analyzer.collector.collector import demo_observation, load_observation
from realizer.common.logging import setup_logging
from realizer.common.utils import write_json
from realizer.config.loader import load_package, load_policy_mapping, validate_package_shape
from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import RealizationEstimate, ReviewResult, make_rpkg_id
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RealizationRequest, RunContext
from realizer.governor import governor
from realizer.estimator import estimator
from realizer.governor.policy import RealizerPolicy
from realizer.packager import packager
from realizer.reconciler import reconciler
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
import copy  # noqa: F401,E402
import datetime as dt  # noqa: F401,E402  (the engine keeps its module attributes; tests use realizer.dt)
import os  # noqa: F401,E402
from decimal import Decimal  # noqa: F401,E402
from typing import Tuple  # noqa: F401,E402
from realizer.analyzer.analysis import candidate_by_id, candidate_steps, config_hash, package_config, scenario_by_id  # noqa: F401,E402
from realizer.common.logging import LOGGER  # noqa: F401,E402
from realizer.common.utils import decimal, money, ratio_string, utc_iso  # noqa: F401,E402
from realizer.config.settings import (  # noqa: F401,E402
    ANALYZER_GATEWAY_VERSION, ATTRIBUTION_VERSION, ESTIMATOR_GATEWAY_VERSION, EXCLUSIVITY_VERSION, MODELER_GATEWAY_VERSION,
    POLICY_VERSION, PRODUCT_VERSION, PROJECTOR_VERSION, REALIZER_VERSION,
)
from realizer.contracts.realization import make_measurement_id, make_val_id  # noqa: F401,E402
from realizer.contracts.reconciliation import RegenerationRequest  # noqa: F401,E402
from realizer.governor.policy import authority_eligible, economic_quality_result, readiness  # noqa: F401,E402
from realizer.reconciler.lifecycle import regeneration_request  # noqa: F401,E402

# ---------------------------------------------------------------------------
# Realization orchestration
# ---------------------------------------------------------------------------


def build_context(request: RealizationRequest) -> RunContext:
    """Validate the source package, apply the policy default and mint the realization package id."""
    validate_package_shape(request.package)
    policy = request.policy or RealizerPolicy()
    return RunContext(request=request, policy=policy, realization_package_id=make_rpkg_id(request.package["recommendation_package_id"]))


def realize(
    package: Mapping[str, Any], observation: Optional[Mapping[str, Any]], output_dir: Path,
    *, policy: Optional[RealizerPolicy] = None, replay_through: Optional[str] = None,
) -> rc.RealizationPackage:
    context = build_context(RealizationRequest(
        package=package, observation=observation, output_dir=output_dir, policy=policy, replay_through=replay_through,
    ))
    # The lineage entry is recorded first: a malformed observation fails inside the Analyzer after it, as it always did.
    # The Governor, the Reconciler, the Estimator and the Reviewer never fail: each keeps its failure as a ``halt`` with
    # the facts decided before it, and the Packager records those and raises it.
    packager.begin(context)
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    reconciliation = reconciler.run(context, analysis, governance)
    reviewed = review(context, analysis, governance, reconciliation)
    packaged = packager.run(context, reviewed)
    snapshot = packaged.realization
    payload = snapshot.to_dict()
    write_json(output_dir / "realization" / "packages" / f"{context.realization_package_id}.json", payload)
    write_json(output_dir / "realization" / "snapshots" / f"{snapshot.snapshot_id}.json", payload)
    return snapshot


def review(
    context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult, reconciliation: ReconciliationResult,
) -> ReviewResult:
    """The Estimator's and the Reviewer's results when validation is due; an empty review otherwise (a reconciliation
    that failed has none: the Packager raises its failure after the events decided before it)."""
    if reconciliation.halt is None and reconciliation.validation_due:
        estimate = estimator.run(context, analysis, governance, reconciliation)
        return reviewer.run(context, analysis, governance, reconciliation, estimate)
    return ReviewResult(analysis=analysis, governance=governance, reconciliation=reconciliation, estimate=RealizationEstimate())


# ---------------------------------------------------------------------------
# Compatibility projection (legacy Reporter v1.0.0 remains immutable)
# ---------------------------------------------------------------------------


def legacy_compatibility(snapshot: rc.RealizationPackage) -> Dict[str, Any]:
    state = snapshot.current_state["realization_state"]
    actor = snapshot.application.get("actor")
    mapping = {
        "NOT_APPLIED": "NOT_APPLIED",
        "APPLIED": "APPLIED",
        "VALIDATING": "VALIDATING",
        "REALIZED": "REALIZED",
        "UNDER_REALIZED": "REALIZED",
        "VALIDATION_FAILED": "FAILED_VALIDATION",
        "ROLLED_BACK": "ROLLED_BACK",
        "MONITORING": "REALIZED",
    }
    if state not in mapping:
        return {"status": rc.CompatibilityStatus.UNREPRESENTABLE.value, "legacy_realization_status": None, "reasons": [f"REALIZER_STATE_{state}_HAS_NO_LEGACY_MAPPING"]}
    reasons: List[str] = []
    quality = rc.CompatibilityStatus.LOSSLESS
    if state in {"UNDER_REALIZED", "MONITORING"}:
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append(f"{state}_COLLAPSES_TO_LEGACY_REALIZED")
    if snapshot.application.get("state") == "APPLIED" and not snapshot.application.get("actor"):
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN")
    if snapshot.application.get("implementation_conformance") == "MODIFIED":
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("LEGACY_HAS_NO_MODIFIED_CONFORMANCE")
    return {"status": quality.value, "legacy_realization_status": mapping[state], "reasons": reasons}


# ---------------------------------------------------------------------------
# CLI (the demo observation is realizer.analyzer.collector.collector)
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deterministic Databricks SQL Warehouse Realizer v1.0.0")
    p.add_argument("--package", required=True, help="Path to immutable RecommendationPackage JSON")
    p.add_argument("--observation", help="Path to normalized post-change observation JSON")
    p.add_argument("--output-dir", default="./warehouse_optimizer_out")
    p.add_argument("--policy", help="Optional Realizer policy override JSON")
    p.add_argument("--demo", action="store_true", help="Use deterministic synthetic post-change observation")
    p.add_argument("--demo-modified", action="store_true", help="Demo a modified size target when possible")
    p.add_argument("--replay-through", help="Project only ledger entries recorded through this UTC time")
    p.add_argument("--print-json", action="store_true")
    p.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), default="INFO")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    setup_logging(args.log_level)
    package = load_package(args.package)
    observation: Optional[Mapping[str, Any]] = None
    if args.demo or args.demo_modified:
        observation = demo_observation(package, modified=args.demo_modified)
    elif args.observation:
        observation = load_observation(args.observation)
    policy = RealizerPolicy.from_mapping(load_policy_mapping(args.policy))
    snapshot = realize(package, observation, Path(args.output_dir), policy=policy, replay_through=args.replay_through)
    if args.print_json:
        print(json.dumps(snapshot.to_dict(), indent=2, sort_keys=True))
    else:
        print(Path(args.output_dir) / "realization" / "packages" / f"{snapshot.realization_package_id}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
