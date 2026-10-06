"""Backfill floor-demand maps into evidence fixtures that predate them (fixtures only).

Usage: python scripts/backfill_floor_demand.py tests/golden/cases/*   (case folders, or case names)

Reads and rewrites each case's source/evidence.json (a JSON list of evidence items); cases without one are skipped.

Each current-epoch `resource_daily` row without `floor_minutes_by_demand_clusters` gets
publisher.analyzer.collector.demo.synthesize_floor_demand for its own minutes at the floor: idle time (the epoch's
running minus busy minutes) is demand 0, and the busy rest follows the recent concurrency histogram's demand clusters,
below the floor only. An epoch without running time or a recent histogram is left without maps (its floor cut is not
priced). Real evidence is never backfilled: missing maps withhold the floor cut's economics.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.golden import evidence_source_path  # noqa: E402
from publisher.analyzer.collector.demo import synthesize_floor_demand  # noqa: E402
from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.analyzer.metrics import FLOOR_DEMAND_FIELD  # noqa: E402
from publisher.common.utils import safe_float  # noqa: E402


def backfill(evidence):
    """Add maps to each current epoch's rows lacking one, in place; returns how many rows changed."""
    changed = 0
    for x in evidence:
        e = evidence_from_dict(x)
        for raw, ep in zip(x["epochs"], e.epochs):
            for row in (raw.get("resource_daily") or []) if ep.is_current else []:
                if FLOOR_DEMAND_FIELD in row:
                    continue
                floor_map = synthesize_floor_demand(ep, safe_float(row.get("minutes_at_min")))
                if floor_map is not None:
                    row[FLOOR_DEMAND_FIELD] = floor_map
                    changed += 1
    return changed


def main(cases):
    """Rewrite each named case's evidence fixture when anything was backfilled; prints one line per file changed."""
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
