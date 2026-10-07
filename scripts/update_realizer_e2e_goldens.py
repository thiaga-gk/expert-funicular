#!/usr/bin/env python3
"""Regenerate the stored output of the end-to-end golden cases (Publisher to Realizer), deliberately.

Each case under ``tests/golden/realizer/cases/<name>/`` stores two stages (``tests/golden/realizer/e2e.py``):

- ``publisher``: ``expected/publisher/package.json``, the Publisher demo's package for the case's warehouse under its
  run scope and cost profile (one demo run per distinct scope and profile);
- ``realizer``: ``expected/realizer/``, the live path on the case's simulated world from the stored package: the
  measurement evidence, the observation, the oracle and the realization files (or the refusal).

The stages are regenerated separately, so a Publisher change and a Realizer change each show their own diff; ``all``
writes the Publisher stage first and builds the Realizer stage from the new packages. Rewriting stored output records a
change of behavior, so the script refuses to write without ``--approved-change``; ``--check`` reports the cases whose
stored output differs from the current output (exit 1) and writes nothing.

Usage:
  python scripts/update_realizer_e2e_goldens.py --approved-change "<why>"                          # both stages, every case
  python scripts/update_realizer_e2e_goldens.py --approved-change "<why>" --stage realizer --case <name>
  python scripts/update_realizer_e2e_goldens.py --check [--stage publisher|realizer|all] [--case <name>]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.golden.realizer import e2e  # noqa: E402


def write_files(target: Path, files: dict) -> None:
    """Replace *target* with the given {relative path: text} files (the old directory is removed first)."""
    if target.exists():
        shutil.rmtree(target)
    for rel, text in files.items():
        path = target / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def main(argv=None) -> int:
    """Regenerate (or, with --check, compare) the selected stages of the selected cases."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--approved-change", help="the approved change this update belongs to")
    mode.add_argument("--check", action="store_true", help="report the cases whose stored output differs; write nothing")
    ap.add_argument("--stage", choices=("publisher", "realizer", "all"), default="all", help="which stage to regenerate (default all)")
    ap.add_argument("--case", action="append", default=[], help="only this case (repeatable)")
    args = ap.parse_args(argv)
    if args.approved_change is not None and not args.approved_change.strip():
        ap.error("--approved-change must name the change")
    names = e2e.case_names()
    unknown = sorted(set(args.case) - set(names))
    if unknown:
        ap.error(f"unknown case(s): {', '.join(unknown)}")
    selected = args.case or names
    drift = []
    packages = {}
    if args.stage in ("publisher", "all"):
        packages = e2e.publisher_packages(selected)
        for name in selected:
            path = e2e.case_dir(name) / e2e.PACKAGE_FILE
            stored = path.read_text(encoding="utf-8") if path.is_file() else None
            if args.check:
                if stored != packages[name]:
                    drift.append(f"{name}: publisher stage differs")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(packages[name], encoding="utf-8")
                print(f"{name}: expected/publisher written")
    if args.stage in ("realizer", "all"):
        for name in selected:
            # --check compares each stage on its own: the Realizer stage always runs from the stored package.
            package = json.loads(packages[name]) if name in packages and not args.check else None
            produced = e2e.realizer_stage(name, package)
            if args.check:
                if e2e.stored_realizer_files(name) != produced:
                    drift.append(f"{name}: realizer stage differs")
            else:
                write_files(e2e.case_dir(name) / e2e.REALIZER_DIR, produced)
                print(f"{name}: expected/realizer written ({len(produced)} files)")
    if args.check:
        for line in drift:
            print(line)
        print(f"{len(drift)} stage(s) differ" if drift else "stored output matches")
        return 1 if drift else 0
    print(f"end-to-end goldens updated for: {args.approved_change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
