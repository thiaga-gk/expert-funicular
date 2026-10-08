#!/usr/bin/env python3
"""Create a new golden case folder from one warehouse of a live (or replayed) publisher run.

Usage:
  python .claude/skills/databricks-golden-miner/scripts/make_case.py --run-dir mining_out --warehouse <ws>/<wh>
      --row C59 --slug serverless-churn --description "<one sentence: the pattern and the expected behavior>"

Writes tests/golden/publisher/cases/<row>-<slug>/ with:
  case.yaml            name, description, origin: live, collected: <analysis date>, the run's cost-profile flag when
                       the package's profile came from the CLI, and a DRAFT expect block that pins today's decision per
                       knob (status, recommended value, leading blockers). Edit it down to the assertions that state the
                       row's expected behavior before approval: pinned values are a starting point, not the contract.
  source/evidence.json the warehouse's evidence exactly as collected (bare list, schema-checked). Live evidence holds
                       real identities, owner emails and tags; that is the agreed form of live cases.
Never overwrites: an existing case folder is an error (existing goldens change only through scripts/update_goldens.py
with an approved change). Prints the update_goldens command that generates expected/. Exit 0, or 2 on bad input.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

SKILL_DIR = Path(__file__).resolve().parents[1]
REPO = SKILL_DIR.parents[2]
for p in (REPO, REPO / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mine_patterns import find_run, load_run  # noqa: E402
from publisher.analyzer.collector.replay import validate_evidence_payload  # noqa: E402
from publisher.config.versions import EVIDENCE_FORMAT_VERSION, PUBLISHER_VERSION  # noqa: E402

CASES = REPO / "tests" / "golden" / "publisher" / "cases"


def draft_assertions(pkg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Today's decision per knob, as golden DSL assertions (sorted by knob)."""
    out: List[Dict[str, Any]] = []
    for c in sorted(pkg.get("candidates") or [], key=lambda c: c["knob"]):
        base = f"candidates[knob={c['knob']}]"
        out.append({"path": f"{base}.status", "equals": c["status"]})
        out.append({"path": f"{base}.recommended_value", "equals": c.get("recommended_value")})
        if c.get("blockers"):
            out.append({"path": f"{base}.blockers", "includes": list(c["blockers"][:2])})
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Create a golden case from a live warehouse (never overwrites)")
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--warehouse", required=True, help="workspace_id/warehouse_id (or a warehouse_id unique in the run)")
    ap.add_argument("--row", required=True, help="registry row id (C## or A##, e.g. C04 or a new C59) or a scope-case id (S##)")
    ap.add_argument("--slug", required=True, help="short kebab-case name, e.g. keepalive-polling-live")
    ap.add_argument("--description", required=True)
    ap.add_argument("--cases-dir", type=Path, default=CASES, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if not re.fullmatch(r"[CAS]\d{2}", a.row) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", a.slug):
        print("error: --row must look like C59, A13 or S04 (registry C##/A## rows and S## scope cases) and --slug be kebab-case", file=sys.stderr)
        return 2
    name = f"{a.row.lower()}-{a.slug}"
    folder = a.cases_dir / name
    if folder.exists():
        print(f"error: {folder} exists; existing cases change only through scripts/update_goldens.py", file=sys.stderr)
        return 2
    warehouses, packages = load_run(find_run(a.run_dir))
    ws, _, wh = a.warehouse.rpartition("/")
    hits = [w for w in warehouses if w["warehouse_id"] == wh and (not ws or w["workspace_id"] == ws)]
    if len(hits) != 1:
        print(f"error: {a.warehouse} matches {len(hits)} warehouses in the run; use workspace_id/warehouse_id", file=sys.stderr)
        return 2
    w = hits[0]
    pkg = packages.get((w["workspace_id"], w["warehouse_id"]))
    if pkg is None:
        print(f"error: the run has no package for {a.warehouse} (excluded or out of scope); see manifest.json", file=sys.stderr)
        return 2
    validate_evidence_payload({"evidence_format_version": EVIDENCE_FORMAT_VERSION, "publisher_version": PUBLISHER_VERSION, "warehouses": [w]})
    case: Dict[str, Any] = {"name": name, "description": a.description, "origin": "live", "collected": w["analysis_time"][:10]}
    profile = pkg.get("cost_profile") or {}
    if str(profile.get("source", "")).startswith("CLI"):
        case["cost_profiles"] = [f"{profile['name']}={w['workspace_id']}/{w['warehouse_id']}"]
    case["expect"] = {"scenario": a.row, "warehouse": w["warehouse_id"], "assertions": draft_assertions(pkg)}
    (folder / "source").mkdir(parents=True)
    (folder / "source" / "evidence.json").write_text(json.dumps([w], indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (folder / "case.yaml").write_text(yaml.safe_dump(case, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
    if pkg.get("run_scope"):
        print("warning: the run was scoped; the golden case runs under the full scope unless case.yaml gets a `scope` list")
    print(f"created {folder} (draft expect: {len(case['expect']['assertions'])} assertions)")
    print(f'next, after approval: python scripts/update_goldens.py --case {name} --approved-change "<why>"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
