#!/usr/bin/env python3
"""Regenerate the stored Realizer compatibility baseline (``tests/fixtures/realizer/expected/``) from the frozen oracle.

The baseline pins the single-file Realizer's behavior (realizer-repo-reorg-design.md 21.4 and Phase 0) while the
modular ``src/realizer`` is extracted. The oracle is frozen, so rewriting the baseline is only for an approved change
to the oracle or to its fixtures (a new scenario, a documented oracle defect fix recorded in
``docs/proposals/realizer-repo-reorg-parity.md``); the script refuses to run without ``--approved-change`` naming it.

Usage:
  python scripts/update_realizer_goldens.py --approved-change "<change>"                     # every scenario
  python scripts/update_realizer_goldens.py --approved-change "<change>" --scenario <name>   # one scenario (repeatable)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

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


def main(argv=None) -> int:
    """Regenerate the selected scenarios' expected/ output from the oracle; returns 0. Writes only under tests/fixtures/realizer/expected/."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--approved-change", required=True, help="the approved change this baseline update belongs to")
    ap.add_argument("--scenario", action="append", default=[], help="regenerate only this scenario (repeatable)")
    args = ap.parse_args(argv)
    if not args.approved_change.strip():
        ap.error("--approved-change must name the change")
    names = canonical.scenario_names()
    unknown = sorted(set(args.scenario) - set(names))
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")
    engine = canonical.oracle_engine()
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
