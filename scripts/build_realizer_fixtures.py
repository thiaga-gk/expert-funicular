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
| ``migration-priced`` | demo ``wh-demo-001`` | a Pro warehouse (auto-stop 0, MEDIUM) whose recommended plan is in place (auto-stop 0 to 35, size to SMALL) and whose warehouse-type migration alternative to Serverless is priced (a range with a negative low) with a READY_FOR_CANARY ``warehouse_type`` step |
| ``unpriced-candidate`` | demo ``wh-demo-003`` | a READY step whose candidate estimate is unavailable |
| ``autostop-scoped-pro`` | demo ``wh-demo-013`` with ``--scope current:auto-stop`` | a Pro warehouse, one READY auto-stop step 30 to 10 with a DBU and modeled AWS range |
| ``autostop-scoped-serverless`` | demo ``wh-demo-002`` with ``--scope current:auto-stop`` | a Serverless warehouse, one READY auto-stop step 30 to 5 (no customer AWS cost) |
| ``autostop-scoped-never-stop`` | demo ``wh-demo-001`` with ``--scope current:auto-stop`` | a Pro warehouse that never stops (auto-stop 0), one READY auto-stop step to 35 |
| ``canary-rollback`` | demo ``wh-demo-040`` | a canary rollback: the size cut LARGE to MEDIUM breached its p95 guard, one READY step back to LARGE (candidate blocker ``CANARY_GUARDRAIL_BREACHED``, no economics) and ``observed_change`` naming the cut |
| ``capacity-add-scoped`` | demo ``wh-demo-008`` with ``--scope current:max-clusters`` | a capacity add: one READY max-clusters step 2 to 5, objective PERFORMANCE, priced as a cost increase |
| ``capacity-cut-scoped`` | demo ``wh-demo-006`` with ``--scope current:max-clusters`` | a zero-priced cut: one READY max-clusters step 4 to 2, objective COST_GUARDRAIL, savings 0 |
| ``min-cut-scoped`` | demo ``wh-demo-011`` with ``--scope current:min-clusters`` | a floor cut: one authority-eligible min-clusters step 3 to 1, priced as a saving with a DBU and modeled AWS range |
| ``autostop-daily-oct02`` | demo ``wh-demo-039`` with ``--scope current:auto-stop --cost-profile balanced``, analysis time 2026-10-02 | the next day's run of the end-to-end golden ``ge07-profile-balanced-exact`` package: same epoch, one READY auto-stop step 365 to 210 |
| ``autostop-daily-oct03`` | the same, analysis time 2026-10-03 | the run of the day after: same epoch and step |
| ``successor-after-autostop`` | demo ``wh-demo-039`` with ``--cost-profile balanced``, analysis time 2026-10-15, its auto-stop set to 210 on 2026-10-08 14:32 | the package after the auto-stop change: ``observed_change`` from the daily packages' epoch, a size step |
| ``canary-before-cut`` | demo ``wh-demo-040`` before its size cut (LARGE since 2025-10-01), analysis time 2026-09-20 | the package of the epoch the ``canary-rollback`` package's ``observed_change`` starts from: step 1 the READY_FOR_CANARY size cut LARGE to MEDIUM |

The scoped fixtures are the packages the live measurement and the end-to-end goldens realize: their warehouses are
simulated after the change by ``publisher.analyzer.collector.demo.simulate_demo_world``.

The dated fixtures are packages of several Publisher runs of one warehouse, the lineages a daily run leaves: the demo
run in a subprocess with its analysis time moved (``demo.DEMO_ANALYSIS_TIME``) and, for a run after or before a
configuration change, the warehouse's fleet entry set to the configuration it had then (the epochs the simulator
builds follow the fleet entry, not the analysis time).

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
DEMO_SOURCES = {"inplace-size-canary": "wh-demo-006", "two-ready-steps": "wh-demo-009", "unpriced-candidate": "wh-demo-003",
                "canary-rollback": "wh-demo-040", "migration-priced": "wh-demo-001"}
CASE_SOURCES = {"scope-migration-only": ("s02-scope-migration-only", "wh-demo-001")}
SCOPED_SOURCES = {"autostop-scoped-pro": "wh-demo-013", "autostop-scoped-serverless": "wh-demo-002", "autostop-scoped-never-stop": "wh-demo-001"}
AUTO_STOP_SCOPE = "current:auto-stop"
MAX_CLUSTER_SOURCES = {"capacity-add-scoped": "wh-demo-008", "capacity-cut-scoped": "wh-demo-006"}
MAX_CLUSTER_SCOPE = "current:max-clusters"
MIN_CLUSTER_SOURCES = {"min-cut-scoped": "wh-demo-011"}
MIN_CLUSTER_SCOPE = "current:min-clusters"
# Dated runs: fixture -> (analysis time, fleet index, fleet entry patch (None removes a key), warehouse, extra arguments).
DAILY_ARGS = ("--scope", AUTO_STOP_SCOPE, "--cost-profile", "balanced")
DATED_SOURCES = {
    "autostop-daily-oct02": ("2026-10-02T12:00:00+00:00", 39, {}, "wh-demo-039", DAILY_ARGS),
    "autostop-daily-oct03": ("2026-10-03T12:00:00+00:00", 39, {}, "wh-demo-039", DAILY_ARGS),
    "successor-after-autostop": ("2026-10-15T12:00:00+00:00", 39, {
        "auto_stop": 210, "epoch_start": "2026-10-08", "epoch_start_minute": 14 * 60 + 32,
        "prior": [{"start": "2025-07-01", "type": "PRO", "size": "MEDIUM", "min": 1, "max": 2, "auto_stop": 365}],
    }, "wh-demo-039", ("--cost-profile", "balanced")),
    "canary-before-cut": ("2026-09-20T12:00:00+00:00", 40, {"size": "LARGE", "epoch_start": "2025-10-01", "prior": None, "after_change": None},
                          "wh-demo-040", ()),
}
# The publisher demo in a subprocess with its analysis time moved and one fleet entry patched.
DATED_RUN = """
import json, sys
from publisher.analyzer.collector import demo
demo.DEMO_ANALYSIS_TIME = sys.argv[1]
spec = demo.DEMO_FLEET[int(sys.argv[2]) - 1]
for key, value in json.loads(sys.argv[3]).items():
    spec.pop(key, None) if value is None else spec.__setitem__(key, value)
from publisher.cli.main import main
raise SystemExit(main(sys.argv[4:]))
"""


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


def _check_migration_priced(p: Mapping[str, Any]) -> None:
    migration = next(s for s in p["scenarios"] if s["scenario_type"] == "WAREHOUSE_TYPE_MIGRATION")
    assert migration["status"] == "ALTERNATIVE" and migration["portfolio_economics"]["savings_365d"]["total"] is not None
    steps = [(s.get("knob"), s.get("current_value"), s.get("recommended_value"), s.get("execution_state")) for s in migration["execution_plan"]]
    assert ("warehouse_type", "PRO", "SERVERLESS", "READY_FOR_CANARY") in steps, steps
    assert p["warehouse"]["warehouse_id"] == "wh-demo-001" and p["run_scope"] is None
    assert [r[1] for r in _roles(p) if r[0] == "CANDIDATE"][:2] == ["auto_stop_minutes", "warehouse_size"], _roles(p)


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


def _only_step(p: Mapping[str, Any]) -> Mapping[str, Any]:
    steps = [s for s in _recommended_plan(p) if s["step_type"] == "CANDIDATE"]
    assert len(steps) == 1, _roles(p)
    return steps[0]


def _check_canary_rollback(p: Mapping[str, Any]) -> None:
    step = _only_step(p)
    candidate = _candidate(p, step["candidate_id"])
    assert "CANARY_GUARDRAIL_BREACHED" in candidate["blockers"], candidate["blockers"]
    change = next(c for c in p["observed_change"]["changes"] if c["knob"] == step["knob"])
    assert step["recommended_value"] == change["previous_value"] and step["execution_state"] == "READY"


def _check_capacity_add(p: Mapping[str, Any]) -> None:
    step = _only_step(p)
    candidate = _candidate(p, step["candidate_id"])
    assert (step["knob"], step["execution_state"], candidate["objectives"]) == ("max_clusters", "READY", ["PERFORMANCE"])
    assert candidate["independent_economics"]["savings_365d"]["total"]["base"] < 0, "priced as a cost increase"


def _daily_step(p: Mapping[str, Any]) -> Mapping[str, Any]:
    step = _only_step(p)
    assert (step["knob"], step["current_value"], step["recommended_value"], step["execution_state"]) == ("auto_stop_minutes", 365, 210, "READY"), step
    return step


def _check_daily(p: Mapping[str, Any]) -> None:
    _daily_step(p)
    assert p["observed_change"] is None and p["run_scope"] is not None


def _check_successor(p: Mapping[str, Any]) -> None:
    change = p["observed_change"]
    assert [(c["knob"], c["previous_value"], c["current_value"]) for c in change["changes"]] == [("auto_stop_minutes", 365, 210)], change
    assert _only_step(p)["knob"] == "warehouse_size"


def _check_canary_before_cut(p: Mapping[str, Any]) -> None:
    step = _recommended_plan(p)[0]
    assert (step["knob"], step["current_value"], step["recommended_value"], step["execution_state"]) == ("warehouse_size", "LARGE", "MEDIUM", "READY_FOR_CANARY"), step
    assert p["observed_change"] is None


def _check_capacity_cut(p: Mapping[str, Any]) -> None:
    step = _only_step(p)
    candidate = _candidate(p, step["candidate_id"])
    assert (step["knob"], step["execution_state"], candidate["objectives"]) == ("max_clusters", "READY", ["COST_GUARDRAIL"])
    assert candidate["independent_economics"]["savings_365d"]["total"]["base"] == 0, "a zero-priced cut"


def _check_min_cut(p: Mapping[str, Any]) -> None:
    from realizer.governor.policy import authority_eligible
    step = _only_step(p)
    assert (step["knob"], step["current_value"], step["recommended_value"]) == ("min_clusters", 3, 1), step
    assert authority_eligible(step), step["execution_state"]
    savings = _candidate(p, step["candidate_id"])["independent_economics"]["savings_365d"]
    assert savings["dbu"] is not None and savings["total"] is not None and savings["total"]["low"] > 0, "a priced floor cut"


CHECKS: Dict[str, Callable[[Mapping[str, Any]], None]] = {
    "canary-rollback": _check_canary_rollback,
    "capacity-add-scoped": _check_capacity_add,
    "capacity-cut-scoped": _check_capacity_cut,
    "min-cut-scoped": _check_min_cut,
    "inplace-size-canary": _check_inplace,
    "two-ready-steps": _check_two_ready,
    "scope-migration-only": _check_migration_only,
    "unpriced-candidate": _check_unpriced,
    "migration-priced": _check_migration_priced,
    **{name: _check_scoped_auto_stop for name in SCOPED_SOURCES},
    "autostop-daily-oct02": _check_daily,
    "autostop-daily-oct03": _check_daily,
    "successor-after-autostop": _check_successor,
    "canary-before-cut": _check_canary_before_cut,
}


def _dated_package(analysis_time: str, index: int, patch: Mapping[str, Any], warehouse: str, extra) -> Dict[str, Any]:
    """The Publisher demo's package for *warehouse* at *analysis_time*, its fleet entry *index* patched."""
    with tempfile.TemporaryDirectory(prefix="realizer-fixtures-dated-") as tmp:
        argv = [sys.executable, "-c", DATED_RUN, analysis_time, str(index), json.dumps(patch),
                "--demo", "--flat-output", "--no-workbook", "--log-level", "WARNING", "--output-dir", tmp, "--warehouse", warehouse, *extra]
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
        done = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True)
        if done.returncode != 0:
            raise SystemExit(f"publisher demo at {analysis_time} failed:\n{done.stderr[-3000:]}")
        (path,) = sorted((Path(tmp) / "packages").glob("*.json"))
        return json.loads(path.read_text(encoding="utf-8"))


def _check_lineages(built: Mapping[str, Mapping[str, Any]]) -> None:
    """The dated fixtures relate to each other and to the stored packages as the lineages they stand for."""
    daily_epoch = built["autostop-daily-oct02"]["warehouse"]["current_epoch"]["epoch_id"]
    golden = json.loads((ROOT / "tests" / "golden" / "realizer" / "cases" / "ge07-profile-balanced-exact" / "expected" / "publisher" / "package.json").read_text(encoding="utf-8"))
    assert golden["warehouse"]["current_epoch"]["epoch_id"] == daily_epoch == built["autostop-daily-oct03"]["warehouse"]["current_epoch"]["epoch_id"]
    assert built["successor-after-autostop"]["observed_change"]["previous_epoch_id"] == daily_epoch
    assert built["canary-rollback"]["observed_change"]["previous_epoch_id"] == built["canary-before-cut"]["warehouse"]["current_epoch"]["epoch_id"]


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
    max_scoped = _demo_packages(MAX_CLUSTER_SOURCES.values(), ["--scope", MAX_CLUSTER_SCOPE])
    for name, warehouse in MAX_CLUSTER_SOURCES.items():
        built[name] = max_scoped[warehouse]
    min_scoped = _demo_packages(MIN_CLUSTER_SOURCES.values(), ["--scope", MIN_CLUSTER_SCOPE])
    for name, warehouse in MIN_CLUSTER_SOURCES.items():
        built[name] = min_scoped[warehouse]
    for name, (case, warehouse) in CASE_SOURCES.items():
        packages = json.loads((GOLDEN_CASES / case / "expected" / "recommendation.json").read_text(encoding="utf-8"))
        built[name] = next(p for p in packages if p["warehouse"]["warehouse_id"] == warehouse)
    for name, source in DATED_SOURCES.items():
        built[name] = _dated_package(*source)
    for name, package in built.items():
        try:
            CHECKS[name](package)
        except AssertionError as exc:
            raise SystemExit(f"fixture {name}: the Publisher package no longer has the plan shape it stands for ({exc})")
        validate_package(package)
    try:
        _check_lineages(built)
    except AssertionError as exc:
        raise SystemExit(f"dated fixtures no longer relate as the lineages they stand for ({exc})")
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
