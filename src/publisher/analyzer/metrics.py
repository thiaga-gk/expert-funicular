"""Derived metrics: auto-stop measurement grid, running idle, concurrency histograms, resource windows and demand trends."""
from __future__ import annotations

import bisect
import datetime as dt
import math
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from publisher.common.time import parse_datetime
from publisher.common.utils import clamp, optional_float, safe_float, safe_int
from publisher.governor.thresholds import Thresholds
from publisher.contracts.source import EpochEvidence, WarehouseEvidence


def auto_stop_targets(t: Thresholds) -> List[int]:
    """Every auto-stop value any supported warehouse type may be evaluated at (the collector measures each)."""
    return list(range(min(t.auto_stop_floor_serverless, t.auto_stop_floor_pro_classic), t.auto_stop_max_minutes + 1, t.auto_stop_step_minutes))


def auto_stop_candidates(t: Thresholds, warehouse_type: str) -> List[int]:
    """Auto-stop values evaluated for one warehouse type: every step from the type's floor up to the maximum."""
    floor = t.auto_stop_floor_serverless if warehouse_type == "SERVERLESS" else t.auto_stop_floor_pro_classic
    return [x for x in auto_stop_targets(t) if x >= floor]


def running_idle_metrics(busy: Sequence[Tuple[float, float]], segments: Sequence[Tuple[float, float, int]], window: Tuple[float, float],
                         auto_stop: int, targets: Sequence[float], full_fraction: float = 0.99, margin_pct: float = 10.0,
                         kept_alive_tolerance: float = 2.0, stopped_gap_lag: float = 10.0) -> Dict[str, Any]:
    """Reference implementation of the running-idle collector (times in minutes, intervals half-open).

    ``busy`` are query intervals (start, end), ``segments`` RUNNING intervals (start, end, clusters > 0), ``window`` the
    evidence period (the same period as the event totals). An idle gap runs from the latest busy end before a query to
    that query's start; the gap after the last query runs to the window end (from the window start when there was no
    query). Under target T the warehouse would stop T minutes into a gap, but a lower auto-stop cannot stop it later
    than the current setting already does: running time past the current auto-stop was kept alive by something other
    than queries (open sessions, result fetching) and is reported; a gap kept alive longer than ``kept_alive_tolerance``
    minutes (stop events can trail the auto-stop) would not stop at a lower value either, so it contributes no
    removable time and no added restart. A value covers a gap (keeps the
    warehouse up until the next query) only when it is at least ``margin_pct`` longer than the gap, so a small schedule
    drift does not turn every gap into a restart. Per T (``running_idle_by_target``, only values with a non-zero field):

    * removable running wall/cluster minutes: RUNNING time in [gap start + T, min(gap end, gap start + current));
    * additional starts: gaps with a next query that the warehouse ran through (it did not restart for that query)
      and that T does not cover;
    * avoided starts / added idle wall minutes (raising auto-stop): gaps the warehouse stopped in, restarting for the
      next query; under T it stays up for min(gap, T) minutes, and a gap T covers saves its restart.
    """
    w0, w1 = window
    cover = 1.0 + margin_pct / 100.0
    gaps: List[Tuple[float, float, bool]] = []
    frontier: Optional[float] = None
    for start, end in sorted(busy):
        if frontier is not None and start > frontier:
            gaps.append((frontier, start, True))
        frontier = end if frontier is None else max(frontier, end)
    last = w0 if frontier is None else frontier
    if last < w1:
        gaps.append((last, w1, False))
    segs = [(max(a, w0), min(b, w1), c) for a, b, c in segments if c > 0 and min(b, w1) > max(a, w0)]  # clipped to the window
    # Cumulative running time F(x) = sum over segments of max(0, min(x, end) - start): with segment starts and ends
    # sorted, F(x) = sum over starts < x of (x - start) minus sum over ends < x of (x - end), so any overlap is
    # F(b) - F(a) in O(log n).
    def prefix(points: List[Tuple[float, float]]) -> Tuple[List[float], List[float], List[float], List[float], List[float]]:
        points = sorted(points)
        xs = [x for x, _ in points]; cw, cwx, cn, cnx = [0.0], [0.0], [0.0], [0.0]
        for x, w in points:
            cw.append(cw[-1] + w); cwx.append(cwx[-1] + w * x); cn.append(cn[-1] + 1); cnx.append(cnx[-1] + x)
        return xs, cw, cwx, cn, cnx
    starts, ends = prefix([(a, c) for a, _, c in segs]), prefix([(b, c) for _, b, c in segs])

    def cumulative(x: float, weighted: bool) -> float:
        total = 0.0
        for (xs, cw, cwx, cn, cnx), sign in ((starts, 1.0), (ends, -1.0)):
            k = bisect.bisect_left(xs, x)
            total += sign * ((cw[k] * x - cwx[k]) if weighted else (cn[k] * x - cnx[k]))
        return total

    def overlap(a: float, b: float, weighted: bool = False) -> float:
        return max(0.0, cumulative(b, weighted) - cumulative(a, weighted)) if b > a else 0.0

    out: Dict[str, Any] = {"running_segment_count": len(segs),
                           "observed_running_cluster_minutes": round(sum((b - a) * c for a, b, c in segs), 3)}
    kept_alive, kept_gaps, restarts = 0.0, 0, 0
    per_gap = []
    for g0, g1, has_next in gaps:
        if g1 <= w0:
            continue
        c0, c1 = max(g0, w0), min(g1, w1)
        stop_at = g0 + auto_stop if auto_stop > 0 else None
        running = overlap(c0, c1)
        alive = overlap(max(c0, stop_at), c1) if stop_at is not None else 0.0
        ran_through = running >= full_fraction * (c1 - c0)
        # Kept alive: ran through to the next query (or the window end) more than the tolerance past the auto-stop, or
        # stopped before it but only after running longer than a late stop event explains (a session held it open).
        kept = alive > kept_alive_tolerance and (ran_through or alive > stopped_gap_lag)
        kept_alive += alive if kept else 0.0; kept_gaps += 1 if kept else 0; restarts += 1 if has_next and not ran_through else 0
        per_gap.append((g0, g1, has_next, c0, c1, stop_at, running, ran_through, kept))
    out["kept_alive_wall_minutes"] = round(kept_alive, 3)
    out["kept_alive_gap_count"] = kept_gaps
    out["restarts_for_queries"] = restarts
    by_target: Dict[str, Dict[str, Any]] = {}
    # Only gap/value pairs that can contribute (as in the SQL): a gap the warehouse ran through matters to values that
    # do not cover it; a gap it stopped in matters to every value.
    through = sorted((x for x in per_gap if x[7]), key=lambda x: x[1] - x[0], reverse=True)
    stopped = [x for x in per_gap if not x[7]]
    for target in targets:
        wall = cluster = added_idle = 0.0
        added = avoided = 0
        k = 0
        while k < len(through) and target < cover * (through[k][1] - through[k][0]):
            k += 1
        for g0, g1, has_next, c0, c1, stop_at, running, ran_through, kept in through[:k] + stopped:
            r0 = max(g0 + target, c0)
            r1 = c0 if kept else c1 if stop_at is None else min(c1, stop_at)
            wall += overlap(r0, r1); cluster += overlap(r0, r1, True)
            covered = target >= cover * (g1 - g0)
            # A restart is added only where the warehouse ran through to the next query because of the current setting
            # (a gap within the current auto-stop); one kept alive past it would not stop at a lower value either.
            if has_next and ran_through and not covered and not kept and (stop_at is None or stop_at >= g1):
                added += 1
            if not ran_through:
                added_idle += max(0.0, min(g1 - g0, float(target)) - running)
                if has_next and covered:
                    avoided += 1
        row = {"removable_running_wall_minutes": round(wall, 3), "removable_running_cluster_minutes": round(cluster, 3),
               "additional_starts": added, "avoided_starts": avoided, "added_idle_wall_minutes": round(added_idle, 3)}
        if any(row.values()):
            by_target[str(target)] = row
    out["running_idle_by_target"] = by_target
    return out


def concurrency_histograms(intervals: Sequence[Tuple[float, float, float]], epoch_window: Tuple[float, float], recent_start: Optional[float],
                           lag_minutes: int) -> Dict[str, Dict[str, float]]:
    """Reference implementation of the concurrency collector (times in minutes from any origin aligned to UTC minutes).

    ``intervals`` are (start, end, queries) spans of running or queued queries (one query each in the collector). Each
    UTC minute's concurrency is the time-weighted number of queries in it (query-minutes / 1); its level is that number
    rounded up. Histograms (level -> weight, levels as strings): EPOCH = busy minutes (level >= 1) over the epoch
    window, RECENT = busy minutes from ``recent_start`` (current epoch only), BUSY_START = for each busy period that
    starts after at least ``lag_minutes`` idle minutes, its highest level in its first ``lag_minutes`` minutes (count of
    periods), within the recent window.
    """
    lo, hi = epoch_window
    per_minute: Dict[int, float] = {}
    for a, b, w in intervals:
        a, b = max(a, lo), min(b, hi)
        while a < b:
            m = math.floor(a + 1e-9)
            cut = min(b, m + 1.0)
            per_minute[m] = per_minute.get(m, 0.0) + (cut - a) * w
            a = cut
    out: Dict[str, Dict[str, float]] = {"EPOCH": {}}

    def add(hist: Dict[str, float], level: int, weight: float = 1.0) -> None:
        hist[str(level)] = hist.get(str(level), 0.0) + weight

    busy = sorted(m for m, c in per_minute.items() if c > 1e-9)
    for m in busy:
        add(out["EPOCH"], math.ceil(per_minute[m] - 1e-9))
    if recent_start is not None:
        out["RECENT"], out["BUSY_START"] = {}, {}
        recent = [m for m in busy if m >= recent_start]
        for m in recent:
            add(out["RECENT"], math.ceil(per_minute[m] - 1e-9))
        prev: Optional[int] = None
        for i, m in enumerate(recent):
            if prev is None or m - prev > lag_minutes:
                peak = max(math.ceil(per_minute[x] - 1e-9) for x in recent[i:] if x < m + lag_minutes)
                add(out["BUSY_START"], peak)
            prev = m
    return out


def histogram_percentile(hist: Optional[Mapping[str, Any]], pct: float) -> Optional[int]:
    """Smallest level whose cumulative weight reaches pct% of the total; None when the histogram is empty or missing."""
    if not hist:
        return None
    items = sorted((int(k), safe_float(v)) for k, v in hist.items())
    total = sum(w for _, w in items)
    if total <= 0:
        return None
    run = 0.0
    for level, w in items:
        run += w
        if run >= pct / 100.0 * total - 1e-9:
            return level
    return items[-1][0]


def histogram_top_level(hist: Optional[Mapping[str, Any]], minutes: float) -> Optional[int]:
    """Highest level reached in at least `minutes` of weight (the level of the minutes-th busiest minute); None when the
    histogram holds less weight than that (too little busy time to have a sustained peak)."""
    if not hist:
        return None
    items = sorted(((int(k), safe_float(v)) for k, v in hist.items()), reverse=True)
    run = 0.0
    for level, w in items:
        run += w
        if run >= minutes - 1e-9:
            return level
    return None


def recent_peak_at_rate(recent: Optional[Mapping[str, Any]], long_days: float, t: Thresholds, recent_days: float = 30.0) -> Optional[int]:
    """The recent (30-day) window's peak read at the same rate as a long window's: a long window's peak is the level
    reached in peak_demand_min_minutes busy minutes over long_days, so the recent window's is the level reached in
    peak_demand_min_minutes x recent_days / long_days of its busy minutes (at least 1); recent_days is the window's
    actual span (30, or the current epoch's age when younger). A recent window with busy minutes but
    less weight than that counts at its highest level; one with no busy minute counts as 0; None when not collected."""
    if recent is None:
        return None
    minutes = max(1.0, t.peak_demand_min_minutes * max(recent_days, 0.0) / max(long_days, 1.0))
    level = histogram_top_level(recent, minutes)
    if level is None:
        level = max((int(k) for k, v in recent.items() if safe_float(v) > 0), default=0)
    return level


def window_days(start: Optional[str], end: Optional[str], analysis_time: str, horizon_days: int = 365) -> float:
    """Calendar days of [start, end) (end None = analysis time) within the last horizon_days."""
    a, b = parse_datetime(start), parse_datetime(end) if end else parse_datetime(analysis_time)
    lo = parse_datetime(analysis_time) - dt.timedelta(days=horizon_days)
    if a is None or b is None:
        return 0.0
    return max(0.0, (min(b, parse_datetime(analysis_time)) - max(a, lo)).total_seconds() / 86400.0)


def cost_stability(cv: Optional[float]) -> Optional[float]:
    """Cost-stability confidence component in [0, 1]; None when the coefficient of variation is unknown."""
    return None if cv is None else clamp(1.0 - safe_float(cv) / 2.0, 0.0, 1.0)


def conditional_severity(q: Dict[str, Any], count_key: str, p95_key: str) -> Tuple[Optional[float], int]:
    """p95 severity among *affected* queries, plus how many queries were affected.

    Returns (None, 0) when the collector did not report the affected-query count (older evidence): the
    severity is unknown, never zero. A reported count of 0 means no query was affected: (0.0, 0).
    """
    if q.get(count_key) is None:
        return None, 0
    n = safe_int(q.get(count_key))
    if n <= 0:
        return 0.0, 0
    return (None if q.get(p95_key) is None else safe_float(q.get(p95_key))), n


RESOURCE_SERIES_FIELDS = ("uptime_minutes", "cluster_minutes", "minutes_at_min", "minutes_at_max", "recycle_minutes", "max_cluster_count", "starts")


def resource_window_from(epoch: EpochEvidence, basis_end: Optional[str], t: Thresholds, days: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The current epoch's daily resource series on the DBU forecast's calendar: the last forecast_long_days (or `days`)
    complete days ending at the billing basis end, clipped to the epoch start, missing days zero-filled. None when the
    series was not collected or no basis day exists."""
    end = parse_datetime(basis_end) if basis_end else None
    start_dt = parse_datetime(epoch.epoch_start)
    if epoch.resource_daily is None or end is None or start_dt is None:
        return None
    end_day = end.date()
    start_day = max(start_dt.astimezone(dt.timezone.utc).date(), end_day - dt.timedelta(days=(days or t.forecast_long_days) - 1))
    collected_from = parse_datetime(epoch.events.get("resource_daily_from"))
    if collected_from is not None:
        # Days before the collected range are unknown, never zero: the window shrinks rather than invents idle days.
        start_day = max(start_day, collected_from.date())
    if epoch.resource_daily and "resource_coverage_from" not in epoch.events:
        return None  # collected before the coverage marker existed: billed lead-in days cannot be told from idle ones
    coverage_from = parse_datetime(epoch.events.get("resource_coverage_from"))
    if coverage_from is not None:
        # Billed days before the first retained warehouse event have unknown running time (never idle): the collector
        # marks the first fully covered day. Unbilled lead-in days (not yet created, dormant) stay known zeros.
        start_day = max(start_day, coverage_from.date())
    if start_day > end_day:
        return None
    by_day = {str(r.get("usage_day"))[:10]: r for r in epoch.resource_daily}
    days = [start_day + dt.timedelta(days=i) for i in range((end_day - start_day).days + 1)]
    window: Dict[str, Any] = {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": len(days)}
    for name in RESOURCE_SERIES_FIELDS:
        window[name] = [safe_float((by_day.get(d.isoformat()) or {}).get(name)) for d in days]
    return window


def resource_window(e: "WarehouseEvidence", t: Thresholds) -> Optional[Dict[str, Any]]:
    meta = e.forecast_metadata or {}
    return resource_window_from(e.current_epoch, meta.get("forecast_basis_end") or meta.get("billing_complete_through"), t)


def annualized_resource_rates(epoch: EpochEvidence, meta: Mapping[str, Any], t: Thresholds, fields: Sequence[str]) -> Tuple[Optional[Dict[str, float]], Dict[str, Any]]:
    """Annual totals of daily resource series on the DBU forecast's model, so AWS and DBU describe the same year.

    Run-rate model: the forecast's window and recent-weighted blend, times its trend adjustment. Cadence model: the same
    whole run-to-run cycles over the latest runs warehouse events cover (at least forecast_min_cycles of them), with no
    trend. Returns (None, info with a blocker) when the window is missing or holds too few covered cycles.
    """
    end = meta.get("forecast_basis_end") or meta.get("billing_complete_through")
    if meta.get("forecast_model") == "CADENCE":
        window = resource_window_from(epoch, end, t, days=365)
        if window is None:
            return None, {"blocker": "RECENT_RESOURCE_WINDOW_UNAVAILABLE"}
        first = dt.date.fromisoformat(window["start"])
        starts = [x for x in (meta.get("cadence_run_starts") or []) if x >= window["start"]]
        if len(starts) < t.forecast_min_cycles + 1:
            return None, {"blocker": "CADENCE_CYCLES_NOT_COVERED_BY_EVENTS", "cadence_cycles": max(0, len(starts) - 1),
                          "resource_window_start": window["start"], "resource_window_end": window["end"]}
        i0, i1 = ((dt.date.fromisoformat(x) - first).days for x in (starts[0], starts[-1]))
        rates = {f: sum(window[f][i0:i1]) / (i1 - i0) * 365.0 for f in fields}
        return rates, {"model": "CADENCE", "cadence_cycles": len(starts) - 1, "trend_adjustment_factor": 1.0, "window_cluster_minutes": sum(window["cluster_minutes"][i0:i1]),
                       "window_uptime_minutes": sum(window["uptime_minutes"][i0:i1]), "max_observed_clusters": max(window["max_cluster_count"][i0:i1], default=0.0),
                       "resource_window_start": starts[0], "resource_window_end": (dt.date.fromisoformat(starts[-1]) - dt.timedelta(days=1)).isoformat(), "resource_window_days": i1 - i0}
    window = resource_window_from(epoch, end, t)
    if window is None:
        return None, {"blocker": "RECENT_RESOURCE_WINDOW_UNAVAILABLE"}
    trend = safe_float(meta.get("trend_adjustment_factor"), 1.0)
    rates = {f: blended_daily_rate(window[f], t) * 365.0 * trend for f in fields}
    return rates, {"model": "RUN_RATE", "trend_adjustment_factor": trend, "window_cluster_minutes": sum(window["cluster_minutes"]),
                   "window_uptime_minutes": sum(window["uptime_minutes"]), "max_observed_clusters": max(window["max_cluster_count"], default=0.0),
                   "resource_window_start": window["start"], "resource_window_end": window["end"], "resource_window_days": window["days"]}


def whole_weeks(values: Sequence[float]) -> List[float]:
    """The most recent whole weeks of a daily series (all of it when shorter than a week)."""
    values = list(values)
    return values[len(values) % 7:] if len(values) >= 7 else values


def blended_daily_rate(values: Sequence[float], t: Thresholds) -> float:
    """Same recent-weighted blend as the DBU forecast, so AWS and DBU describe the same year."""
    values = whole_weeks(values)
    if not values:
        return 0.0
    recent, long = values[-t.forecast_recent_days:], values[-t.forecast_long_days:]
    if len(values) >= t.forecast_long_days:
        return t.forecast_weight_28_long * statistics.mean(recent) + (1 - t.forecast_weight_28_long) * statistics.mean(long)
    if len(values) >= t.forecast_recent_days:
        return t.forecast_weight_28_mid * statistics.mean(recent) + (1 - t.forecast_weight_28_mid) * statistics.mean(values)
    return statistics.mean(values)


def recent_capacity_facts(window: Optional[Mapping[str, Any]], t: Thresholds) -> Dict[str, Optional[float]]:
    """Time at min/max and the observed peak over the last pressure_window_days of the resource window."""
    if window is None:
        return {"state": "MISSING", "at_min_pct": None, "at_max_pct": None, "max_observed": None, "window_days": None,
                "minutes_at_max": None, "cluster_minutes": None}
    n = t.pressure_window_days
    uptime = sum(window["uptime_minutes"][-n:])
    peak = max(window["max_cluster_count"][-n:], default=0.0)
    return {
        # IDLE: collected, but the warehouse did not run in the window, so time-at-level shares are undefined.
        "state": "OK" if uptime > 0 else "IDLE",
        "at_min_pct": None if uptime <= 0 else round(100.0 * sum(window["minutes_at_min"][-n:]) / uptime, 4),
        "at_max_pct": None if uptime <= 0 else round(100.0 * sum(window["minutes_at_max"][-n:]) / uptime, 4),
        "max_observed": int(peak),
        "window_days": min(n, window["days"]),
        "minutes_at_max": round(sum(window["minutes_at_max"][-n:]), 6),
        "cluster_minutes": round(sum(window["cluster_minutes"][-n:]), 6),
    }


def size_busy_share(busy_minutes: Any, uptime_minutes: Any, cluster_minutes: Any, min_clusters: Any) -> Optional[float]:
    """Share of running cluster-minutes spent with queries running; None when unknown.

    Idle running time is assumed to hold the minimum cluster count (Classic/Pro scale down only after sustained low
    load), so idle cluster-minutes = (running minutes - busy minutes) x min clusters, and busy cluster-minutes are
    at least busy wall-minutes. Busy time above running time (sources disagree) counts as all busy.
    """
    busy = optional_float(busy_minutes); uptime = optional_float(uptime_minutes); cluster = optional_float(cluster_minutes)
    if busy is None or uptime is None or cluster is None or uptime <= 0 or cluster <= 0:
        return None
    idle_cluster_minutes = max(0.0, uptime - busy) * max(1, safe_int(min_clusters, 1))
    # Busy cluster-minutes are never below busy wall-minutes (cluster counts under the floor would otherwise give 0).
    return round(clamp(max(1.0 - idle_cluster_minutes / cluster, busy / cluster), 0.0, 1.0), 6)


def query_volume_growth_ratio(weeks: Sequence[Any]) -> Optional[float]:
    """Smallest of the last 4 weekly query counts over the median of the 4 weeks before; None when unknown.

    Using every recent week (not a total) keeps a single month-, quarter- or year-end burst week from looking like
    growth, and the median keeps one quiet or burst week in the earlier period from moving the base.
    """
    vals = [optional_float(x) for x in list(weeks)[:8]]
    if len(vals) < 8 or any(v is None for v in vals):
        return None
    prior = statistics.median(vals[4:8])
    if prior <= 0:
        return None
    return round(min(vals[0:4]) / prior, 6)
