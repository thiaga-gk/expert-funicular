"""Backfill running-idle evidence into fixtures that predate it (fixtures only).

Usage: python tools/backfill_running_idle.py tests/golden/*.json

Each current epoch whose gaps carry the legacy `saved_idle_minutes_if_{T}` estimates (query gaps capped at the current
auto-stop, the old auto-stop basis) and no running-idle keys gets them, as if the warehouse had run through every gap
until its auto-stop: removable running wall minutes per auto-stop value (below the current one) interpolated linearly
from the legacy values (and extended at the same slope below and above them, floored at 0), cluster minutes = wall
minutes x min clusters, no added or avoided restarts, no restarts for queries and no kept-alive time; queries = 30-day queries; the window covers
the event totals. Values with nothing removable get no row (a known zero). The legacy keys are then removed. Real evidence is never backfilled: missing running-idle evidence fails closed.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import publisher as p  # noqa: E402

LEGACY = "saved_idle_minutes_if_"


def _interpolate(points, x):
    xs = sorted(points)
    if x <= xs[0]:
        a, b = xs[0], xs[1]
    elif x >= xs[-1]:
        a, b = xs[-2], xs[-1]
    else:
        a = max(v for v in xs if v <= x)
        b = min(v for v in xs if v >= x)
        if a == b:
            return points[a]
    return max(0.0, points[a] + (points[b] - points[a]) * (x - a) / (b - a))


def backfill(evidence):
    changed = 0
    targets = p.auto_stop_targets(p.Thresholds())
    for x in evidence:
        for ep in x["epochs"]:
            gaps = ep.get("gaps") or {}
            legacy = {int(k[len(LEGACY):]): float(v or 0.0) for k, v in gaps.items() if k.startswith(LEGACY)}
            if ep.get("is_current") and "running_idle_queries" in gaps and "restarts_for_queries" not in gaps:
                gaps["restarts_for_queries"] = 0  # fixtures backfilled before restarts were counted
                changed += 1
                continue
            if not ep.get("is_current") or not legacy or "running_idle_queries" in gaps:
                continue
            if len(legacy) == 1:
                legacy[next(iter(legacy)) + 1] = next(iter(legacy.values()))
            clusters = max(1, int(ep["config"]["min_clusters"]))
            events = ep.get("events") or {}
            current = int(ep["config"]["auto_stop_minutes"])
            by_target = {}
            for t in targets:
                wall = round(_interpolate(legacy, t), 3) if current == 0 or t < current else 0.0
                if wall > 0:
                    by_target[str(t)] = {"removable_running_wall_minutes": wall, "removable_running_cluster_minutes": round(wall * clusters, 3),
                                         "additional_starts": 0, "avoided_starts": 0, "added_idle_wall_minutes": 0.0}
            gaps["running_idle_by_target"] = by_target
            gaps.update({
                "running_idle_window_start": events.get("event_coverage_start") or ep["epoch_start"],
                "running_idle_window_end": x["analysis_time"],
                "running_idle_queries": int((ep.get("query") or {}).get("queries_30") or 0),
                "running_segment_count": 1, "observed_running_cluster_minutes": float(events.get("cluster_minutes") or 0.0),
                "kept_alive_wall_minutes": 0.0, "kept_alive_gap_count": 0, "restarts_for_queries": 0,
            })
            for k in [k for k in gaps if k.startswith(LEGACY)]:
                del gaps[k]
            ep["gaps"] = gaps
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
