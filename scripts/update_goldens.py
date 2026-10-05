#!/usr/bin/env python3
"""Regenerate the stored behavioral baseline: golden ``expected/`` files and the demo expectations.

The baseline pins publisher behavior (repo-reorg-design.md section 13.3). Rewriting it is part of a
decision-affecting change only, in the same commit that bumps the component versions (CLAUDE.md change loop), so the
script refuses to run without ``--approved-change`` naming that change.

Usage:
  python scripts/update_goldens.py --approved-change "<change>"                 # every golden case + the demo
  python scripts/update_goldens.py --approved-change "<change>" --case <name>   # one golden case (repeatable)
  python scripts/update_goldens.py --approved-change "<change>" --demo-only
  python scripts/update_goldens.py --approved-change "<change>" --contract     # committed schema/example digests
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

from publisher import publisher as p  # noqa: E402
from tests.golden import case_dir, case_names  # noqa: E402
from tests.regression import canonical  # noqa: E402


def write_files(target: Path, files: dict) -> None:
    if target.exists():
        shutil.rmtree(target)
    for rel, text in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--approved-change", required=True, help="the approved decision-affecting change this baseline update belongs to")
    ap.add_argument("--case", action="append", default=[], help="regenerate only this golden case (repeatable)")
    ap.add_argument("--demo-only", action="store_true", help="regenerate only the demo expectations")
    ap.add_argument("--contract", action="store_true", help="also re-pin the committed schema and contract example digests")
    args = ap.parse_args(argv)
    if not args.approved_change.strip():
        ap.error("--approved-change must name the change")
    if not args.demo_only:
        for name in args.case or case_names():
            write_files(case_dir(name) / "expected", canonical.golden_expected_files(p, name))
            print(f"golden case {name}: expected/ written")
    if args.demo_only or not args.case:
        files = canonical.demo_expected_files(p)
        write_files(canonical.DEMO_EXPECTED, files)
        print(f"demo: {len(files)} expected files written to {canonical.DEMO_EXPECTED.relative_to(ROOT)}")
    if args.contract:
        write_files(canonical.CONTRACT_EXPECTED, canonical.contract_expected_files())
        print(f"contract digests written to {canonical.CONTRACT_EXPECTED.relative_to(ROOT)}")
    print(f"baseline updated for: {args.approved_change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
