#!/usr/bin/env python3
"""Compare a candidate Realizer against the frozen single-file oracle on the same fixtures.

The oracle is ``legacy/realizer_single_file/realizer.py`` (realizer-repo-reorg-design.md 21.4). Every scenario under
``tests/fixtures/realizer/observations/`` (a frozen PKG from ``tests/fixtures/realizer/packages/`` plus a sequence of
observations, policies and replay cuts) runs through both implementations into fresh directories, and the canonical
outputs (``tests/regression/realizer/canonical.py``) are compared aspect by aspect: lifecycle state, blockers,
plan/step status, realized economics, attribution, review result, RLE sequence, RSNAP, RPKG and report, then file by
file. The oracle's output is also checked against the stored baseline in ``tests/fixtures/realizer/expected/``.

The candidate is resolved lazily from ``--candidate`` (default ``realizer.realizer``, the modular ``src/realizer``
engine): ``module:attr`` names an ``Engine`` or a factory returning one, a bare module exposes ``ENGINE`` or the
single-file API (its workbook projection may live in the package's ``contracts.report``). If the candidate package
cannot be found, the script compares the oracle against the stored baseline only and says so.

Usage:
  python scripts/compare_realizer_single_file.py                                # every scenario
  python scripts/compare_realizer_single_file.py --scenario GR-026              # only these (repeatable)
  python scripts/compare_realizer_single_file.py --candidate pkg.module:ENGINE  # another candidate
  python scripts/compare_realizer_single_file.py --oracle-only                  # skip the candidate
Exit status 1 when any canonical output differs.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.regression.realizer import canonical  # noqa: E402


def main(argv=None) -> int:
    """Compare the oracle with the stored baseline and, when present, the candidate with the oracle; 1 on any difference."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", action="append", default=[], help="compare only this scenario (repeatable)")
    ap.add_argument("--candidate", default=canonical.DEFAULT_CANDIDATE, help="candidate engine: module or module:attr")
    ap.add_argument("--oracle-only", action="store_true", help="compare the oracle with the stored baseline only")
    args = ap.parse_args(argv)
    names = canonical.scenario_names()
    unknown = sorted(set(args.scenario) - set(names))
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")
    selected = args.scenario or names

    candidate = None
    if args.oracle_only:
        print("candidate: skipped (--oracle-only)")
    elif canonical.candidate_available(args.candidate):
        candidate = canonical.candidate_engine(args.candidate)
        print(f"candidate: {candidate.name} ({args.candidate})")
    else:
        print(f"candidate: {args.candidate} not found; comparing the oracle against the stored baseline only")

    oracle = canonical.oracle_engine()
    failures: List[str] = []
    for name in selected:
        reference = canonical.produce(oracle, name)
        failures += canonical.semantic_diff(f"{name} [oracle vs stored]", canonical.stored_expected(name), reference)
        if candidate is not None:
            try:
                produced = canonical.produce(candidate, name)
            except Exception as exc:  # report every scenario rather than stopping at the first crash
                failures.append(f"{name} [candidate]: run failed: {type(exc).__name__}: {exc}")
                continue
            failures += canonical.semantic_diff(f"{name} [oracle vs candidate]", reference, produced)

    sides = "oracle = stored baseline" + (" = candidate" if candidate is not None else "")
    if failures:
        print("\n".join(failures))
        print(f"{len(failures)} difference(s) across {len(selected)} scenario(s)")
        return 1
    print(f"{len(selected)} scenario(s): {sides}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
