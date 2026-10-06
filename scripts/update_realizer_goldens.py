#!/usr/bin/env python3
"""Regenerate the stored Realizer expectations from the current engine, deliberately.

Two sets of stored outputs pin the Realizer's behavior (docs/proposals/realizer/repo-reorg-parity.md):

- the scenario baseline, ``tests/fixtures/realizer/expected/<scenario>/``: the canonical output tree of each scenario in
  ``tests/fixtures/realizer/observations/`` (``tests/regression/realizer/canonical.py``), compared by
  ``tests/regression/realizer/test_stored_baseline.py`` and ``scripts/compare_realizer_with_ref.py``;
- the unit-test expectations, ``tests/fixtures/realizer/expectations/<test module>.json``: results of the grids,
  malformed-input cases and failed-run trees that the unit tests check (``tests/realizer_expect.py``), rebuilt by
  running those tests in record mode (``--expectations``).

Both were first generated from the single-file Realizer (git tag ``realizer-reorg-oracle-1.0.0``) and are now rebuilt from
the engine in this checkout. Rewriting them records a change of behavior, so the script refuses to run without
``--approved-change`` naming the approved change; review the resulting diff before committing it, and bump the Realizer
versions in ``src/realizer/config/settings.py`` when the change affects decisions.

Usage:
  python scripts/update_realizer_goldens.py --approved-change "<change>"                       # every scenario
  python scripts/update_realizer_goldens.py --approved-change "<change>" --scenario <name>     # one scenario (repeatable)
  python scripts/update_realizer_goldens.py --approved-change "<change>" --expectations        # the unit-test expectations
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.regression.realizer import canonical  # noqa: E402


def write_files(target: Path, files: dict) -> None:
    """Replace *target* with the given {relative path: text} files (the old directory is removed first)."""
    if target.exists():
        shutil.rmtree(target)
    for rel, text in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def expectation_modules() -> List[str]:
    """Dotted names of the test modules that check stored expectations (they import ``realizer_expect``)."""
    modules = []
    for path in sorted((ROOT / "tests").rglob("test_*.py")):
        if "realizer_expect" in path.read_text(encoding="utf-8"):
            modules.append(".".join(path.relative_to(ROOT).with_suffix("").parts))
    return modules


def update_expectations() -> int:
    """Rebuild every stored unit-test expectation by running its tests in record mode."""
    store = canonical.FIXTURES / "expectations"
    modules = expectation_modules()
    if store.is_dir():
        shutil.rmtree(store)
    env = dict(os.environ, REALIZER_EXPECTATIONS="record", PYTHONPATH=os.pathsep.join([str(ROOT / "src"), str(ROOT)]))
    done = subprocess.run([sys.executable, "-m", "unittest", *modules], cwd=ROOT, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stderr[-3000:], file=sys.stderr)
        print("error: the tests failed in record mode; the expectations directory is incomplete, restore it with git", file=sys.stderr)
        return 1
    files = sorted(store.glob("*.json"))
    print(f"realizer expectations: {len(files)} file(s) written for {len(modules)} test module(s), {sum(p.stat().st_size for p in files)} bytes")
    return 0


def main(argv=None) -> int:
    """Regenerate the selected scenarios' expected/ output (or, with --expectations, the unit-test expectations); returns 0."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--approved-change", required=True, help="the approved change this update belongs to")
    ap.add_argument("--scenario", action="append", default=[], help="regenerate only this scenario (repeatable)")
    ap.add_argument("--expectations", action="store_true", help="regenerate the unit-test expectations instead of the scenario baseline")
    args = ap.parse_args(argv)
    if not args.approved_change.strip():
        ap.error("--approved-change must name the change")
    if args.expectations:
        if args.scenario:
            ap.error("--scenario applies to the scenario baseline, not to --expectations")
        status = update_expectations()
        if status == 0:
            print(f"realizer expectations updated for: {args.approved_change}")
        return status
    names = canonical.scenario_names()
    unknown = sorted(set(args.scenario) - set(names))
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")
    engine = canonical.modular_engine()
    selected = args.scenario or names
    for name in selected:
        write_files(canonical.EXPECTED / name, canonical.produce(engine, name))
        print(f"realizer scenario {name}: expected/ written")
    if not args.scenario and canonical.EXPECTED.is_dir():
        for stale in sorted(p for p in canonical.EXPECTED.iterdir() if p.is_dir() and p.name not in names):
            shutil.rmtree(stale)
            print(f"realizer scenario {stale.name}: removed (no fixture)")
    print(f"realizer baseline updated for: {args.approved_change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
