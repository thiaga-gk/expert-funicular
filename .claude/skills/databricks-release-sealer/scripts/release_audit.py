#!/usr/bin/env python3
"""Release hygiene scan: stale or forbidden text and tracked generated files in a publisher repository.

Scans the files git tracks (every file under the root when it is not a git checkout), so ignored caches, local
outputs and other worktrees are never reported. Exit 1 with one line per finding, else prints "release audit: OK".

  python release_audit.py <repo> [--pattern REGEX ...] [--allow PATH_SUBSTRING ...]
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Text that should not survive a release: transitional paths, removal and fix-me comments, a static token in a file.
DEFAULT_BAD = [r"TODO: remove", r"#\s*(FIXME|XXX)\b", r"legacy_report", r"_legacy_package",
               r"DATABRICKS_TOKEN\s*=\s*[A-Za-z0-9]", r"dapi[0-9a-f]{32}"]
TEXT_SUFFIXES = {".py", ".md", ".yaml", ".yml", ".json", ".toml", ".txt", ".cfg", ".ini"}
GENERATED = re.compile(r"(^|/)(__pycache__/|.*\.pyc$|\.DS_Store$|.*\.egg-info/)")


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
        return [root / name for name in out.decode().split("\0") if name]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("repo")
    ap.add_argument("--pattern", action="append", default=[], help="extra regex to forbid (repeatable)")
    ap.add_argument("--allow", action="append", default=[], help="skip paths containing this text (repeatable)")
    ns = ap.parse_args(argv)
    root = Path(ns.repo).resolve()
    pats = [re.compile(x, re.I) for x in DEFAULT_BAD + ns.pattern]
    errs = []
    for p in tracked_files(root):
        rel = p.relative_to(root).as_posix()
        if any(a in rel for a in ns.allow):
            continue
        if GENERATED.search(rel):
            errs.append(f"{rel}: generated file is tracked")
            continue
        # Evals and this script name the forbidden patterns on purpose.
        if "evals" in p.parts or p.name == "release_audit.py" or p.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        errs += [f"{rel}: matched {rx.pattern}" for rx in pats if rx.search(text)]
    if errs:
        print("\n".join(errs))
        return 1
    print("release audit: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
