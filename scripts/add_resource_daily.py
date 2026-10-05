"""Backfill a synthetic daily resource series into evidence fixtures that predate it (fixtures only).

Usage: python scripts/add_resource_daily.py tests/golden/cases/*   (case folders, or case names)

Reads and rewrites each case's source/evidence.json (a JSON list of evidence items); cases without one are skipped.

Each current epoch without `resource_daily` gets publisher.synthesize_resource_daily (its event totals spread evenly over
its observed days), so recent-window capacity facts and AWS run rates equal the old whole-epoch values. Real evidence
is never backfilled: missing series must fail closed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.golden import evidence_source_path  # noqa: E402
from publisher.analyzer.collector.demo import synthesize_resource_daily  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402



def backfill(evidence):
    changed = 0
    for x in evidence:
        e = evidence_from_dict(x)
        for raw, ep in zip(x["epochs"], e.epochs):
            if ep.is_current and raw.get("resource_daily") is None:
                raw["resource_daily"] = synthesize_resource_daily(ep, e.analysis_time)
                changed += 1
    return changed


def main(cases):
    for case in cases:
        path = evidence_source_path(case)
        if not path.is_file():
            continue  # collector-level case (raw rows go through the collector)
        evidence = json.loads(path.read_text())
        if backfill(evidence):
            path.write_text(json.dumps(evidence, indent=2) + "\n")
            print(f"backfilled {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
