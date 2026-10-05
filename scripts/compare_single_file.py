#!/usr/bin/env python3
"""Compare the working tree against the single-file oracle end to end (repo-reorg-design.md sections 13.4, 21.3).

Both trees run through their CLI on the same inputs: the demo fleet, and every evidence-level golden case wrapped in
an evidence file (collector-level cases have no CLI input; their expected/ output covers them). The oracle tag is
checked out into a temporary worktree and removed afterwards. Canonical outputs (packages, manifest, evidence.json,
workbook cell dump) must be identical; the log and stdout are compared after removing timestamps and output paths and
are reported separately, as they are not canonical.

Usage:
  python scripts/compare_single_file.py                      # oracle tag reorg-oracle-2.10.0 vs the working tree
  python scripts/compare_single_file.py --ref <git-ref>      # another baseline
  python scripts/compare_single_file.py --case <name> ...    # only these golden cases (repeatable), plus the demo
  python scripts/compare_single_file.py --no-demo            # golden cases only
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

from tests.golden import case_names, load_case  # noqa: E402
from tests.regression import canonical  # noqa: E402

ORACLE_REF = "reorg-oracle-2.10.0"
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
    cmd, env = entrypoint(tree)
    proc = subprocess.run(cmd + args + ["--output-dir", str(out)], cwd=tree, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise SystemExit(f"{tree.name}: {' '.join(args)} failed ({proc.returncode}):\n{proc.stderr[-2000:]}")
    return proc.stdout


def non_canonical(text: str, out: Path) -> List[str]:
    return [_TIMESTAMP.sub("", line).replace(str(out), "<out>") for line in text.splitlines()]


def inputs(work: Path, cases: List[str], demo: bool) -> List[Tuple[str, List[str]]]:
    jobs: List[Tuple[str, List[str]]] = [("demo", ["--demo"])] if demo else []
    for name in cases:
        case = load_case(name)
        if "evidence" not in case:
            continue
        path = work / f"{name}.evidence.json"
        path.write_text(json.dumps({"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": "compare",
                                    "warehouses": case["evidence"]}), encoding="utf-8")
        jobs.append((f"case:{name}", ["--from-evidence", str(path)]))
    return jobs


def compare(label: str, old: Dict[str, str], new: Dict[str, str]) -> List[str]:
    problems = []
    for rel in sorted(set(old) | set(new)):
        if rel not in old or rel not in new:
            problems.append(f"{label}: {rel} only in {'working tree' if rel in new else 'oracle'}")
        elif old[rel] != new[rel]:
            diff = list(difflib.unified_diff(old[rel].splitlines(), new[rel].splitlines(), "oracle", "working", lineterm="", n=1))
            problems.append(f"{label}: {rel} differs\n" + "\n".join(diff[:40]))
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default=ORACLE_REF)
    ap.add_argument("--case", action="append", default=[])
    ap.add_argument("--no-demo", action="store_true")
    args = ap.parse_args(argv)
    cases = args.case or case_names()
    with tempfile.TemporaryDirectory(prefix="compare-single-file-") as tmp:
        tmp_path = Path(tmp)
        oracle = tmp_path / "oracle"
        subprocess.run(["git", "worktree", "add", "--detach", "--quiet", str(oracle), args.ref], cwd=ROOT, check=True)
        try:
            jobs = inputs(tmp_path, cases, not args.no_demo)
            failures: List[str] = []
            notes: List[str] = []
            for label, cli in jobs:
                outs = {}
                for side, tree in (("oracle", oracle), ("working", ROOT)):
                    out = tmp_path / "out" / side / label.replace(":", "_")
                    stdout = run(tree, cli, out)
                    log = (out / "publisher.log").read_text(encoding="utf-8")
                    outs[side] = (canonical.output_dir_canonical(out), non_canonical(stdout, out), non_canonical(log, out))
                failures += compare(label, outs["oracle"][0], outs["working"][0])
                for idx, name in ((1, "stdout"), (2, "publisher.log")):
                    if outs["oracle"][idx] != outs["working"][idx]:
                        diff = difflib.unified_diff(outs["oracle"][idx], outs["working"][idx], "oracle", "working", lineterm="", n=0)
                        notes.append(f"{label}: {name} differs (not canonical)\n" + "\n".join(list(diff)[:20]))
                print(f"{label}: {'OK' if not any(f.startswith(label + ':') for f in failures) else 'DIFF'}")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(oracle)], cwd=ROOT, check=False)
    for note in notes:
        print(f"NOTE {note}")
    for failure in failures:
        print(f"DIFF {failure}")
    print(f"{len(jobs)} runs compared against {args.ref}: {len(failures)} canonical diff(s), {len(notes)} non-canonical note(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
