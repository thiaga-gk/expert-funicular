"""Backfill per-minute concurrency histograms into fixtures that predate them (fixtures only).

Usage: python scripts/backfill_concurrency.py tests/golden/publisher/cases/*   (case folders, or case names)

Reads and rewrites each case's source/evidence.json (a JSON list of evidence items); cases without one are skipped.

Each epoch without `query.concurrency_histogram` gets one built from its observed cluster peaks, as if demand had
filled exactly the clusters the warehouse ran at (documented 10 queries per cluster): busy minutes (the gap
collector's busy minutes, else 1,000) at level 1, with 1% of them at 10 x the peak cluster count, so the P99.5 equals
that peak. RECENT uses the recent daily series' peak (else the epoch peak), EPOCH the epoch peak; BUSY_START has 30
busy periods starting at one cluster of demand (10 queries). Real evidence is never backfilled: missing concurrency
evidence fails closed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.golden.publisher import evidence_source_path  # noqa: E402


def _hist(busy, peak_clusters):
    """Two-level histogram {1: 99% of busy minutes, 10 x peak clusters: 1%} so the P99.5 equals the peak."""
    peak = 10 * max(1, int(peak_clusters or 1))
    if peak == 1:
        return {"1": float(busy)}
    return {"1": round(busy * 0.99, 3), str(peak): round(busy * 0.01, 3)}


def backfill(evidence):
    """Add the missing concurrency histograms in place; returns how many epochs changed."""
    changed = 0
    for x in evidence:
        for ep in x["epochs"]:
            query = ep.setdefault("query", {})
            if "concurrency_histogram" in query:
                continue
            events = ep.get("events") or {}
            busy = float((ep.get("gaps") or {}).get("busy_minutes") or 1000.0)
            epoch_peak = events.get("max_observed_clusters_within_ceiling") or events.get("max_observed_clusters") or 1
            hist = {"EPOCH": _hist(busy, epoch_peak)}
            if ep.get("is_current"):
                days = sorted(ep.get("resource_daily") or [], key=lambda d: d["usage_day"])[-30:]
                recent_peak = max((d.get("max_cluster_count") or 0 for d in days), default=0) or epoch_peak
                hist["RECENT"] = _hist(busy, recent_peak)
                hist["BUSY_START"] = {"10": 30.0}
            query["concurrency_histogram"] = hist
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
