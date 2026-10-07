#!/usr/bin/env python3
"""Build the Realizer's package fixtures from real Publisher output.

The Realizer reads the RecommendationPackage the Publisher writes, so its frozen package fixtures are Publisher
packages, never hand-written ones. This script runs the Publisher demo deterministically (``--demo --flat-output
--no-workbook``; the demo's analysis time is fixed) for the demo-fleet warehouses below, takes the restricted
migration-only package from the golden case ``s02-scope-migration-only``, checks that each package still has the plan
shape its fixture stands for, validates it as the Realizer does (``realizer.config.loader.validate_package``), and
writes it to ``tests/fixtures/realizer/packages/<fixture>.json``.

| Fixture | Source | Shape it stands for |
|---|---|---|
| ``inplace-size-canary`` | demo ``wh-demo-006`` | step 1 a READY_FOR_CANARY size cut that invalidates the cluster knobs; a READY step; a PENDING_REEVALUATION step; a REMEASURE step; a priced warehouse-type migration alternative with a compatibility step |
| ``two-ready-steps`` | demo ``wh-demo-009`` | two authority-eligible steps (READY_FOR_CANARY, then READY) followed by a PENDING_REEVALUATION step |
| ``scope-migration-only`` | golden case ``s02-scope-migration-only`` | ``run_scope`` = migration only: step-less NO_CHANGE_BASELINE recommended, migration an alternative |
| ``unpriced-candidate`` | demo ``wh-demo-003`` | a READY step whose candidate estimate is unavailable |
| ``autostop-scoped-pro`` | demo ``wh-demo-013`` with ``--scope current:auto-stop`` | a Pro warehouse, one READY auto-stop step 30 to 10 with a DBU and modeled AWS range |
| ``autostop-scoped-serverless`` | demo ``wh-demo-002`` with ``--scope current:auto-stop`` | a Serverless warehouse, one READY auto-stop step 30 to 5 (no customer AWS cost) |
| ``autostop-scoped-never-stop`` | demo ``wh-demo-001`` with ``--scope current:auto-stop`` | a Pro warehouse that never stops (auto-stop 0), one READY auto-stop step to 35 |

The scoped fixtures are the packages the live measurement and the end-to-end goldens realize: their warehouses are
simulated after the change by ``publisher.analyzer.collector.demo.simulate_demo_world``.

Rewriting a fixture changes every stored Realizer output that uses it, so the script refuses to write without
``--approved-change``. ``--check`` rebuilds the packages in memory and reports whether the committed fixtures differ
(exit 1), without writing.

Usage:
  python scripts/build_realizer_fixtures.py --approved-change "<why>"
  python scripts/build_realizer_fixtures.py --check
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from realizer.config.loader import validate_package  # noqa: E402

PACKAGES = ROOT / "tests" / "fixtures" / "realizer" / "packages"
GOLDEN_CASES = ROOT / "tests" / "golden" / "cases"
DEMO_SOURCES = {"inplace-size-canary": "wh-demo-006", "two-ready-steps": "wh-demo-009", "unpriced-candidate": "wh-demo-003"}
CASE_SOURCES = {"scope-migration-only": ("s02-scope-migration-only", "wh-demo-001")}
SCOPED_SOURCES = {"autostop-scoped-pro": "wh-demo-013", "autostop-scoped-serverless": "wh-demo-002", "autostop-scoped-never-stop": "wh-demo-001"}
AUTO_STOP_SCOPE = "current:auto-stop"


def _recommended_plan(package: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    scenario_id = package["recommended_plan"]["scenario_id"]
    scenario = next(s for s in package["scenarios"] if s["scenario_id"] == scenario_id)
    return scenario["execution_plan"]


def _roles(package: Mapping[str, Any]) -> List[tuple]:
    return [(s["step_type"], s.get("knob"), s.get("execution_state")) for s in _recommended_plan(package)]


def _candidate(package: Mapping[str, Any], candidate_id: str) -> Mapping[str, Any]:
    return next(c for c in package["candidates"] if c["candidate_id"] == candidate_id)


def _check_inplace(p: Mapping[str, Any]) -> None:
    plan = _recommended_plan(p)
    assert _roles(p)[0] == ("CANDIDATE", "warehouse_size", "READY_FOR_CANARY"), _roles(p)
    assert plan[0]["invalidates"], "step 1 must invalidate downstream knobs"
    states = [s.get("execution_state") for s in plan if s["step_type"] == "CANDIDATE"]
    assert "READY" in states and "PENDING_REEVALUATION" in states, states
    assert any(s["step_type"] == "REMEASURE" for s in plan)
    migration = next(s for s in p["scenarios"] if s["scenario_type"] == "WAREHOUSE_TYPE_MIGRATION")
    assert migration["status"] == "ALTERNATIVE" and migration["portfolio_economics"]["savings_365d"]["total"] is not None
    assert migration["execution_plan"][0]["step_type"] == "COMPATIBILITY_VALIDATION"


def _check_two_ready(p: Mapping[str, Any]) -> None:
    states = [r[2] for r in _roles(p) if r[0] == "CANDIDATE"]
    assert states[:2] in (["READY_FOR_CANARY", "READY"], ["READY", "READY_FOR_CANARY"]), states
    assert "PENDING_REEVALUATION" in states[2:], states


def _check_migration_only(p: Mapping[str, Any]) -> None:
    assert p["run_scope"] is not None and _recommended_plan(p) == []
    recommended = next(s for s in p["scenarios"] if s["scenario_id"] == p["recommended_plan"]["scenario_id"])
    assert recommended["scenario_type"] == "NO_CHANGE_BASELINE"


def _check_scoped_auto_stop(p: Mapping[str, Any]) -> None:
    roles = _roles(p)
    assert [r for r in roles if r[0] == "CANDIDATE"] == [("CANDIDATE", "auto_stop_minutes", "READY")], roles
    step = next(s for s in _recommended_plan(p) if s["step_type"] == "CANDIDATE")
    savings = _candidate(p, step["candidate_id"])["independent_economics"]["savings_365d"]
    assert savings["dbu"] is not None and savings["total"] is not None, "a priced auto-stop step"
    assert p["run_scope"] is not None


def _check_unpriced(p: Mapping[str, Any]) -> None:
    step = next(s for s in _recommended_plan(p) if s["step_type"] == "CANDIDATE")
    assert step["execution_state"] == "READY"
    assert _candidate(p, step["candidate_id"])["independent_economics"]["estimation_status"] == "UNAVAILABLE"


CHECKS: Dict[str, Callable[[Mapping[str, Any]], None]] = {
    "inplace-size-canary": _check_inplace,
    "two-ready-steps": _check_two_ready,
    "scope-migration-only": _check_migration_only,
    "unpriced-candidate": _check_unpriced,
    **{name: _check_scoped_auto_stop for name in SCOPED_SOURCES},
}


def _demo_packages(warehouses, extra=()) -> Dict[str, Dict[str, Any]]:
    """The Publisher demo's packages for *warehouses* (``--demo --flat-output --no-workbook`` plus *extra* arguments),
    by warehouse id."""
    with tempfile.TemporaryDirectory(prefix="realizer-fixtures-") as tmp:
        argv = [sys.executable, "-m", "publisher", "--demo", "--flat-output", "--no-workbook", "--log-level", "WARNING",
                "--output-dir", tmp, *extra]
        for warehouse in warehouses:
            argv += ["--warehouse", warehouse]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
        done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"publisher demo failed:\n{done.stderr[-3000:]}")
        by_warehouse = {}
        for path in sorted((Path(tmp) / "packages").glob("*.json")):
            package = json.loads(path.read_text(encoding="utf-8"))
            by_warehouse[package["warehouse"]["warehouse_id"]] = package
    return by_warehouse


def build() -> Dict[str, Dict[str, Any]]:
    """Every fixture package, freshly built and checked: {fixture name: package}."""
    built: Dict[str, Dict[str, Any]] = {}
    plain, scoped = _demo_packages(DEMO_SOURCES.values()), _demo_packages(SCOPED_SOURCES.values(), ["--scope", AUTO_STOP_SCOPE])
    for name, warehouse in DEMO_SOURCES.items():
        built[name] = plain[warehouse]
    for name, warehouse in SCOPED_SOURCES.items():
        built[name] = scoped[warehouse]
    for name, (case, warehouse) in CASE_SOURCES.items():
        packages = json.loads((GOLDEN_CASES / case / "expected" / "recommendation.json").read_text(encoding="utf-8"))
        built[name] = next(p for p in packages if p["warehouse"]["warehouse_id"] == warehouse)
    for name, package in built.items():
        try:
            CHECKS[name](package)
        except AssertionError as exc:
            raise SystemExit(f"fixture {name}: the Publisher package no longer has the plan shape it stands for ({exc})")
        validate_package(package)
    return built


def dumps(package: Mapping[str, Any]) -> str:
    """The Publisher's own package serialization (indent 2, sorted keys, trailing newline)."""
    return json.dumps(package, indent=2, sort_keys=True) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--approved-change", help="the approved change this rewrite belongs to")
    mode.add_argument("--check", action="store_true", help="report whether the committed fixtures differ; write nothing")
    args = ap.parse_args(argv)
    built = build()
    if args.check:
        stale = [name for name, package in built.items() if not (PACKAGES / f"{name}.json").is_file()
                 or (PACKAGES / f"{name}.json").read_text(encoding="utf-8") != dumps(package)]
        for name in stale:
            print(f"realizer fixture {name}: differs from the Publisher's current output")
        return 1 if stale else 0
    if not args.approved_change.strip():
        ap.error("--approved-change must name the change")
    PACKAGES.mkdir(parents=True, exist_ok=True)
    for stale in sorted(set(PACKAGES.glob("*.json")) - {PACKAGES / f"{n}.json" for n in built}):
        stale.unlink()
        print(f"realizer fixture {stale.stem}: removed")
    for name, package in built.items():
        (PACKAGES / f"{name}.json").write_text(dumps(package), encoding="utf-8")
        print(f"realizer fixture {name}: {package['recommendation_package_id']} ({package['warehouse']['warehouse_id']})")
    print(f"realizer fixtures written for: {args.approved_change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
