#!/usr/bin/env python3
"""Compare the working tree against any git ref end to end: same inputs, canonical outputs diffed.

Both trees run through their CLI on the same inputs: the demo fleet, and every evidence-level golden case wrapped in
an evidence file (collector-level cases have no CLI input; their expected/ output covers them). The ref is
checked out into a temporary worktree and removed afterwards; refs from before the src/publisher layout run through
their single-file ``publisher.py``. Canonical outputs (packages, manifest, evidence.json,
workbook cell dump) must be identical; the log and stdout are compared after removing timestamps and output paths and
are reported separately, as they are not canonical.

Usage:
  python scripts/compare_with_ref.py --ref master                 # master vs the working tree
  python scripts/compare_with_ref.py --ref <ref> --case <name>    # only these golden cases (repeatable), plus the demo
  python scripts/compare_with_ref.py --ref <ref> --no-demo        # golden cases only
Exit status 1 when any canonical output differs.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from tests.golden.publisher import case_names, load_case  # noqa: E402
from tests.regression.publisher import canonical  # noqa: E402

EVIDENCE_FORMAT_VERSION = "1.0.0"
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z ")


def entrypoint(tree: Path) -> Tuple[List[str], Dict[str, str]]:
    """The tree's CLI: the single-file ``publisher.py`` or the ``src/publisher`` package."""
    env = dict(os.environ)
    if (tree / "publisher.py").is_file():
        return [sys.executable, str(tree / "publisher.py")], env
    if (tree / "src" / "publisher" / "__main__.py").is_file():
        env["PYTHONPATH"] = os.pathsep.join(x for x in (str(tree / "src"), env.get("PYTHONPATH", "")) if x)
        return [sys.executable, "-m", "publisher"], env
    raise SystemExit(f"no publisher entrypoint in {tree}")


def run(tree: Path, args: List[str], out: Path) -> str:
    """Run *tree*'s CLI with *args* into *out* and return its stdout; exits the script when the run fails."""
    cmd, env = entrypoint(tree)
    proc = subprocess.run(cmd + args + ["--output-dir", str(out)], cwd=tree, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"{tree.name}: {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    return proc.stdout


def run_dir(out: Path) -> Path:
    """Where a run wrote its outputs: *out* itself (flat output, and refs before run folders), else the latest
    ``run_date=*/run_ts=*`` folder under it."""
    if (out / "publisher.log").is_file():
        return out
    folders = sorted(out.glob("run_date=*/run_ts=*"))
    if not folders:
        raise SystemExit(f"no publisher.log under {out}")
    return folders[-1]


def non_canonical(text: str, out: Path) -> List[str]:
    """Lines with the leading log timestamp removed and the output directory replaced, for like-for-like comparison."""
    return [_TIMESTAMP.sub("", line).replace(str(out), "<out>") for line in text.splitlines()]


def inputs(work: Path, cases: List[str], demo: bool) -> List[Tuple[str, List[str]]]:
    """The CLI jobs to compare as (label, arguments): the demo, and each evidence-level golden case as a --from-evidence file written under *work*."""
    jobs: List[Tuple[str, List[str]]] = [("demo", ["--demo"])] if demo else []
    for name in cases:
        case = load_case(name)
        if "evidence" not in case:
            continue
        path = work / f"{name}.evidence.json"
        path.write_text(json.dumps({"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": "compare",
                                    "warehouses": case["evidence"]}), encoding="utf-8")
        scope = [arg for spec in case.get("scope") or () for arg in ("--scope", spec)]
        jobs.append((f"case:{name}", ["--from-evidence", str(path), *scope]))
    return jobs


def compare(label: str, old: Dict[str, str], new: Dict[str, str]) -> List[str]:
    """Differences between two canonical file maps: files on one side only, and unified diffs (first 40 lines) of changed files."""
    problems = []
    for rel in sorted(set(old) | set(new)):
        if rel not in old or rel not in new:
            problems.append(f"{label}: {rel} only in {'working tree' if rel in new else 'ref'}")
        elif old[rel] != new[rel]:
            diff = list(difflib.unified_diff(old[rel].splitlines(), new[rel].splitlines(), "ref", "working", lineterm="", n=1))
            problems.append(f"{label}: {rel} differs\n" + "\n".join(diff[:40]))
    return problems


def main(argv=None) -> int:
    """Compare the ref and the working tree; returns 1 when any canonical output differs, else 0."""
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", required=True, help="git ref to compare the working tree against")
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--no-demo", action="store_true")
    args = ap.parse_args(argv)
    cases = args.case or case_names()
    with tempfile.TemporaryDirectory(prefix="compare-with-ref-") as tmp:
        tmp_path = Path(tmp)
        base = tmp_path / "ref"
        subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(base), args.ref], cwd=ROOT, check=True)
        try:
            jobs = inputs(tmp_path, cases, not args.no_demo)
            failures: List[str] = []
            notes: List[str] = []
            for label, cli in jobs:
                outs = {}
                for side, tree in (("ref", base), ("working", ROOT)):
                    out = tmp_path / "out" / side / label.replace(":", "_")
                    stdout = run(tree, cli, out)
                    folder = run_dir(out)
                    log = (folder / "publisher.log").read_text(encoding="utf-8")
                    outs[side] = (canonical.output_dir_canonical(folder), non_canonical(stdout, folder), non_canonical(log, folder))
                failures += compare(label, outs["ref"][0], outs["working"][0])
                for idx, name in ((1, "stdout"), (2, "publisher.log")):
                    if outs["ref"][idx] != outs["working"][idx]:
                        diff = difflib.unified_diff(outs["ref"][idx], outs["working"][idx], "ref", "working", lineterm="", n=0)
                        notes.append(f"{label}: {name} differs (not canonical)\n" + "\n".join(list(diff)[:20]))
                print(f"{label}: {'OK' if not any(f.startswith(label + ':') for f in failures) else 'DIFF'}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(base)], cwd=ROOT, check=False)
    for note in notes:
        print(f"NOTE {note}")
    for failure in failures:
        print(f"DIFF {failure}")
    print(f"{len(jobs)} runs compared against {args.ref}: {len(failures)} canonical diff(s), {len(notes)} non-canonical note(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
