"""Backfill running-idle evidence into fixtures that predate it (fixtures only).

Usage: python scripts/backfill_running_idle.py tests/golden/publisher/cases/*   (case folders, or case names)

Reads and rewrites each case's source/evidence.json (a JSON list of evidence items); cases without one are skipped.

Each current epoch whose gaps carry the legacy `saved_idle_minutes_if_{T}` estimates (query gaps capped at the current
auto-stop, the old auto-stop basis) and no running-idle keys gets them, as if the warehouse had run through every gap
until its auto-stop: removable running wall minutes per auto-stop value (below the current one) interpolated linearly
from the legacy values (and extended at the same slope below and above them, floored at 0), cluster minutes = wall
minutes x min clusters, no added or avoided restarts, no restarts for queries and no kept-alive time; queries = 30-day queries; the window covers
the event totals. Values with nothing removable get no row (a known zero). The legacy keys are then removed.

Whole-period running-idle keys (`gaps.running_idle_*` and the flat `running_idle_by_target` map, the collector's
shape before it measured per forecast window) are then spread over the forecast-basis windows (`to_windows`). Real
evidence is never backfilled: missing running-idle evidence fails closed.
"""
import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from publisher.analyzer.collector.demo import scale_running_idle_windows  # noqa: E402
from publisher.analyzer.forecast import Modeler  # noqa: E402
from publisher.analyzer.metrics import auto_stop_targets, running_idle_basis  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402
from tests.golden.publisher import evidence_source_path  # noqa: E402

LEGACY = "saved_idle_minutes_if_"


def _interpolate(points, x):
    """Linear interpolation of {auto-stop: minutes} at x, extended at the end slopes and floored at 0."""
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
    """Convert legacy idle estimates to running-idle evidence in place; returns how many epochs changed."""
    changed = 0
    targets = auto_stop_targets(Thresholds())
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


FLAT_KEYS = ("running_idle_window_start", "running_idle_window_end", "running_idle_queries", "running_segment_count",
             "observed_running_cluster_minutes", "kept_alive_wall_minutes", "kept_alive_gap_count", "restarts_for_queries", "running_idle_by_target")


def to_windows(evidence, recompute_forecast=False):
    """Spread whole-period running-idle keys (``gaps.running_idle_*`` and the flat ``running_idle_by_target`` map) over
    the forecast-basis windows (``gaps.running_idle_windows``, ``gaps.running_idle_model``) in proportion to each
    window's share of the period, as the hand-built demo does; returns how many epochs changed. Running wall minutes
    are the event uptime of the same period; a raise's added idle cluster-minutes are its wall minutes at the floor.
    With ``recompute_forecast`` the windows follow the forecast the golden harness recomputes from the daily rows."""
    changed = 0
    t = Thresholds()
    for x in evidence:
        for ep in x["epochs"]:
            gaps = ep.get("gaps") or {}
            if not ep.get("is_current") or "running_idle_by_target" not in gaps or "running_idle_windows" in gaps:
                continue
            meta = x.get("forecast_metadata") or {}
            if recompute_forecast:
                model = Modeler(t).forecast_from_daily_cost(x.get("daily_cost_365") or [], ep["epoch_start"], x["analysis_time"],
                                                            billing_watermark=meta.get("billing_watermark"))
                meta = {k: v for k, v in model.items() if k not in {"current_cost_365", "forecast_cost_365", "currency"}}
            events = ep.get("events") or {}
            floor = max(1, int(ep["config"]["min_clusters"]))
            flat = {"queries": gaps.get("running_idle_queries"), "running_segment_count": gaps.get("running_segment_count"),
                    "observed_running_cluster_minutes": gaps.get("observed_running_cluster_minutes"),
                    "observed_running_wall_minutes": events.get("uptime_minutes") or 0.0,
                    "kept_alive_wall_minutes": gaps.get("kept_alive_wall_minutes"), "kept_alive_gap_count": gaps.get("kept_alive_gap_count"),
                    "restarts_for_queries": gaps.get("restarts_for_queries"),
                    "running_idle_by_target": {k: {**row, "added_idle_cluster_minutes": round(float(row.get("added_idle_wall_minutes") or 0.0) * floor, 3)}
                                               for k, row in gaps["running_idle_by_target"].items()}}
            basis = running_idle_basis(ep["epoch_start"], events.get("event_coverage_start"), meta, t)
            period = (gaps.get("running_idle_window_start") or events.get("event_coverage_start") or ep["epoch_start"],
                      gaps.get("running_idle_window_end") or x["analysis_time"])
            windows = scale_running_idle_windows(flat, basis, period)
            for k in FLAT_KEYS:
                gaps.pop(k, None)
            gaps["running_idle_model"] = basis["model"]
            gaps["running_idle_windows"] = windows
            ep["gaps"] = gaps
            changed += 1
    return changed


def main(cases):
    """Rewrite each named case's evidence fixture when anything was backfilled; prints one line per file changed."""
    for case in cases:
        path = evidence_source_path(case)
        if not path.is_file():
            continue  # collector-level case (raw rows go through the collector)
        evidence = json.loads(path.read_text())
        spec = yaml.safe_load((path.parent.parent / "case.yaml").read_text()) or {}
        changed = backfill(evidence)
        changed += to_windows(evidence, bool((spec.get("expect") or {}).get("recompute_forecast")))
        if changed:
            path.write_text(json.dumps(evidence, indent=2) + "\n")
            print(f"backfilled {path}")


if __name__ == "__main__":
    main(sys.argv[1:])
