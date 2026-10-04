"""Backfill billed DBU per day and event coverage into evidence fixtures that predate them (fixtures only).

Usage: python tools/add_billed_dbu.py tests/golden/*.json

Each daily cost row without `dbu_quantity` gets its list cost times its epoch's DBU-to-cost ratio (epoch
`cost.dbu_quantity / cost.list_cost`), and each epoch without `events.event_coverage_start` is marked as covered from
its start, as synthetic fixtures are. Workspace DBU-per-cluster-hour rates then equal the old epoch totals. Real
evidence is never backfilled: rates fail closed without these fields.
"""
import json
import sys
from pathlib import Path


def backfill(evidence):
    changed = 0
    for x in evidence:
        ratio = {}
        for ep in x["epochs"]:
            cost = ep.get("cost") or {}
            if cost.get("list_cost") and cost.get("dbu_quantity"):
                ratio[ep["epoch_start"]] = cost["dbu_quantity"] / cost["list_cost"]
            events = ep.setdefault("events", {})
            if events and "event_coverage_start" not in events:
                events["event_coverage_start"] = ep["epoch_start"]
                changed += 1
        for row in x.get("daily_cost_365") or []:
            r = ratio.get(row.get("epoch_start"))
            if "dbu_quantity" not in row and r is not None and row.get("list_cost") is not None:
                row["dbu_quantity"] = round(row["list_cost"] * r, 6)
                changed += 1
    return changed


def main(paths):
    for name in paths:
        path = Path(name)
        payload = json.loads(path.read_text())
        if isinstance(payload, dict) and "evidence" not in payload:
            continue  # collector-level fixture (raw rows go through the collector)
        evidence = payload["evidence"] if isinstance(payload, dict) else payload
        if backfill(evidence):
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            print(f"backfilled {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
