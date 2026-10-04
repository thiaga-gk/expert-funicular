"""Backfill a synthetic daily resource series into evidence fixtures that predate it (fixtures only).

Usage: python tools/add_resource_daily.py tests/golden/*.json

Each current epoch without `resource_daily` gets publisher.synthesize_resource_daily (its event totals spread evenly over
its observed days), so recent-window capacity facts and AWS run rates equal the old whole-epoch values. Real evidence
is never backfilled: missing series must fail closed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publisher as p  # noqa: E402


def backfill(evidence):
    changed = 0
    for x in evidence:
        e = p.evidence_from_dict(x)
        for raw, ep in zip(x["epochs"], e.epochs):
            if ep.is_current and raw.get("resource_daily") is None:
                raw["resource_daily"] = p.synthesize_resource_daily(ep, e.analysis_time)
                changed += 1
    return changed


def main(paths):
    for name in paths:
        path = Path(name)
        payload = json.loads(path.read_text())
        if isinstance(payload, dict) and "evidence" not in payload:
            continue  # collector-level fixture (raw snapshots)
        evidence = payload["evidence"] if isinstance(payload, dict) else payload
        if backfill(evidence):
            path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            print(f"backfilled {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
