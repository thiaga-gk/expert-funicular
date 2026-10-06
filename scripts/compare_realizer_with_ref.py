#!/usr/bin/env python3
"""Compare the working tree's Realizer against any git ref: same scenarios, canonical outputs diffed.

The ref is checked out into a temporary git worktree (removed afterwards). Both trees run every scenario under
``tests/fixtures/realizer/observations/`` (a frozen PKG from ``tests/fixtures/realizer/packages/`` plus a sequence of
observations, policies and replay cuts; the same fixtures for both sides) in a fresh interpreter, and the canonical
outputs (``tests/regression/realizer/canonical.py``: the ledger, RPKG, RSNAP, REVID and RERUN files byte for byte, the
per-run IDs with the legacy compatibility projection, and the workbook-extension projection) are compared aspect by
aspect (lifecycle state, blockers, plan/step status, realized economics, attribution, review result, RLE sequence,
RSNAP, RPKG, report), then file by file. The stored baseline (``tests/fixtures/realizer/expected/``) is checked by the
test suite, not here.

A ref is run with whichever engine it contains: the ``src/realizer`` package, or the single-file Realizer under
``legacy/realizer_single_file/``. The single-file Realizer, the behavioral oracle of the reorganization, is kept only
as the git tag ``realizer-reorg-oracle-1.0.0``; to diagnose a difference against it, compare with that tag:

  python scripts/compare_realizer_with_ref.py --ref realizer-reorg-oracle-1.0.0

Usage:
  python scripts/compare_realizer_with_ref.py --ref master                   # every scenario
  python scripts/compare_realizer_with_ref.py --ref <ref> --scenario GR-026  # only these (repeatable)
Exit status: 0 every scenario identical, 1 a canonical output differs (or a run crashed), 2 an unusable ref, an unknown
scenario, a ref without a Realizer, or invalid arguments.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
ERROR_KEY = "__error__"


def _prepare_worker_path(tree: Path) -> str:
    """Put *tree*'s Realizer first on ``sys.path`` and return its layout (``package`` or ``single-file``)."""
    if (tree / "src" / "realizer" / "realizer.py").is_file():
        layout, first = "package", [tree / "src"]
    elif (tree / "legacy" / "realizer_single_file" / "realizer.py").is_file():
        layout, first = "single-file", [tree / "legacy" / "realizer_single_file", tree / "src"]
    else:
        raise SystemExit(f"no Realizer in {tree}: neither src/realizer nor legacy/realizer_single_file")
    sys.path[:0] = [str(path) for path in first]
    # This tree's fixtures and canonical helpers come last, so that they never shadow the Realizer under test.
    sys.path.extend(str(path) for path in (ROOT, ROOT / "src") if str(path) not in sys.path)
    return layout


def dump_tree(tree: Path, names: List[str], out: Path) -> None:
    """Worker: run *names* with the Realizer in *tree* and write ``{scenario: canonical file map}`` to *out*."""
    layout = _prepare_worker_path(tree)
    from tests.regression.realizer import canonical

    if layout == "package":
        engine = canonical.modular_engine(tree / "src" / "realizer")
    else:
        import importlib

        module = importlib.import_module("realizer")
        if Path(module.__file__).resolve().parent != (tree / "legacy" / "realizer_single_file").resolve():
            raise SystemExit(f"realizer resolved to {module.__file__}, not the single-file Realizer of {tree}")
        engine = canonical.single_file_engine(module, importlib.import_module("contracts.realizer_workbook_contract"))
    result: Dict[str, Dict[str, str]] = {}
    for name in names:
        try:
            result[name] = canonical.produce(engine, name)
        except Exception as exc:  # a crash is a result to compare, not a reason to stop
            result[name] = {ERROR_KEY: f"{type(exc).__name__}: {exc}"}
    out.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")


def run_worker(tree: Path, names: List[str], out: Path) -> Dict[str, Dict[str, str]]:
    """Run the worker for *tree* in a fresh interpreter; raises ``RuntimeError`` when it cannot run at all."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    cmd = [sys.executable, str(Path(__file__).resolve()), "--dump", str(tree), "--out", str(out), *names]
    done = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"the Realizer in {tree.name} could not be run ({done.returncode}):\n{done.stderr[-2000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def checkout(ref: str, target: Path) -> Optional[str]:
    """Check *ref* out into the detached worktree *target*; returns an error message, or ``None``."""
    verify = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=ROOT, capture_output=True, text=True)
    if verify.returncode != 0:
        return f"{ref!r} is not a git commit"
    added = subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(target), ref], cwd=ROOT, capture_output=True, text=True)
    return None if added.returncode == 0 else f"cannot check out {ref!r}: {added.stderr.strip()}"


def differences(ref_maps: Dict[str, Dict[str, str]], work_maps: Dict[str, Dict[str, str]], names: List[str]) -> List[str]:
    """Differences between the two sides, scenario by scenario (a crashed scenario is a difference)."""
    from tests.regression.realizer import canonical

    problems: List[str] = []
    for name in names:
        sides = {"ref": ref_maps[name], "working tree": work_maps[name]}
        crashed = [f"{name} [{side}]: run failed: {files[ERROR_KEY]}" for side, files in sides.items() if ERROR_KEY in files]
        if crashed:
            if len(crashed) == 2 and sides["ref"] == sides["working tree"]:
                continue  # the same failure on both sides is not a difference
            problems += crashed
            continue
        problems += canonical.semantic_diff(f"{name} [ref vs working tree]", sides["ref"], sides["working tree"])
    return problems


def main(argv=None) -> int:
    """Compare the ref and the working tree; 1 on any difference, 2 when the ref or the arguments are unusable."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", help="git ref to compare the working tree against")
    ap.add_argument("--scenario", action="append", default=[], help="compare only this scenario (repeatable)")
    ap.add_argument("--dump", help=argparse.SUPPRESS)   # worker: run the scenarios with the Realizer in this tree
    ap.add_argument("--out", help=argparse.SUPPRESS)
    ap.add_argument("names", nargs="*", help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.dump:
        dump_tree(Path(args.dump), args.names, Path(args.out))
        return 0
    if not args.ref:
        ap.error("--ref is required")
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from tests.regression.realizer import canonical

    names = canonical.scenario_names()
    unknown = sorted(set(args.scenario) - set(names))
    if unknown:
        ap.error(f"unknown scenario(s): {', '.join(unknown)}")
    selected = args.scenario or names

    with tempfile.TemporaryDirectory(prefix="compare-realizer-with-ref-") as tmp:
        tmp_path = Path(tmp)
        base = tmp_path / "ref"
        error = checkout(args.ref, base)
        if error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        try:
            ref_maps = run_worker(base, selected, tmp_path / "ref.json")
            work_maps = run_worker(ROOT, selected, tmp_path / "working.json")
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(base)], cwd=ROOT, check=False, capture_output=True)

    problems = differences(ref_maps, work_maps, selected)
    if problems:
        print("\n".join(problems))
        print(f"{len(problems)} difference(s) across {len(selected)} scenario(s) against {args.ref}")
        return 1
    print(f"{len(selected)} scenario(s) identical to {args.ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
