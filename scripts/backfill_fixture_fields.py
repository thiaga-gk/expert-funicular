"""Backfill collector fields into evidence fixtures that predate them (fixtures only).

Usage: python scripts/backfill_fixture_fields.py tests/golden/cases/*   (case folders, or case names)

Reads and rewrites each case's source/evidence.json (a JSON list of evidence items); cases without one are skipped.

- Each daily cost row without `dbu_quantity` gets its list cost times its epoch's DBU-to-cost ratio (epoch
  `cost.dbu_quantity / cost.list_cost`), and each epoch without `events.event_coverage_start` is marked as covered from
  its start, as synthetic fixtures are; workspace DBU-per-cluster-hour rates then equal the old epoch totals.
- Each current epoch without `query.decision_queries_30` gets the non-result-cache share of its 30-day queries
  (queries_30 x (1 - result-cache %)).
- Each current epoch with a daily resource series and no `events.resource_coverage_from` gets it as null (synthetic
  fixtures have no billed days before their first event).
- Each current epoch with spilled queries and no `query.top_spill_source_share_pct_30` gets spill spread evenly over one
  statement source per 50 spilled queries (the demo simulator's default): the top three carry min(3, n) / n of it.
Real evidence is never backfilled: missing fields fail closed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.golden import evidence_source_path  # noqa: E402


def _decision(query):
    return int(round(query["queries_30"] * max(0.0, 1.0 - (query.get("result_cache_pct_30") or 0.0) / 100.0)))


def backfill(evidence):
    changed = 0
    for x in evidence:
        ratio = {}
        for ep in x["epochs"]:
            cost = ep.get("cost") or {}
            if cost.get("list_cost") and cost.get("dbu_quantity"):
                ratio[ep["epoch_start"]] = cost["dbu_quantity"] / cost["list_cost"]
            events = ep.setdefault("events", {})
            if ep.get("is_current") and ep.get("resource_daily") and "resource_coverage_from" not in events:
                events["resource_coverage_from"] = None
                changed += 1
            if events and "event_coverage_start" not in events:
                events["event_coverage_start"] = ep["epoch_start"]
                changed += 1
            query = ep.get("query") or {}
            if ep.get("is_current") and (query.get("spill_queries_30") or 0) > 0 and "top_spill_source_share_pct_30" not in query:
                n = max(1, int(round(query["spill_queries_30"] / 50.0)))
                query["spill_sources_30"] = n
                query["top_spill_source_share_pct_30"] = round(100.0 * min(3, n) / n, 3)
                changed += 1
            if ep.get("is_current") and "queries_30" in query and "decision_queries_30" not in query:
                query["decision_queries_30"] = _decision(query)
                changed += 1
        for row in x.get("daily_cost_365") or []:
            r = ratio.get(row.get("epoch_start"))
            if "dbu_quantity" not in row and r is not None and row.get("list_cost") is not None:
                row["dbu_quantity"] = round(row["list_cost"] * r, 6)
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
