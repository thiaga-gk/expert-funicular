"""No-change baseline forecast from daily billing (the Modeler)."""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Dict, List, Optional, Sequence, Tuple

from publisher.analyzer.collector.billing import _is_unpriced_only_row, settle_reference_time
from publisher.analyzer.metrics import blended_daily_rate, whole_weeks
from publisher.common.time import parse_datetime
from publisher.common.utils import clamp, optional_float, round_or_none, safe_float
from publisher.governor.thresholds import Thresholds
from publisher.config.versions import MODEL_VERSION


def billing_cadence(series: Sequence[float], basis_start: dt.date, t: Thresholds) -> Dict[str, Any]:
    """Runs of billed days in a zero-filled daily cost series and their run-to-run cycles.

    Billed days at most forecast_run_bridge_days apart form one run, so a weekend does not split a month-end close.
    periodic: short runs (median calendar length at most forecast_cadence_gap_days) with long rests between them (median
    idle gap from one run's last day to the next run's first day above it) on a current cadence: the last run started
    no more than one reference cycle (the upper quartile of cycles, or the only cycle) plus forecast_cadence_gap_days
    before the basis end. weekly: the same shape with a rest of exactly the limit, where the cadence model is used only
    when the run rate has no number. A run starting within forecast_run_bridge_days of the basis start may continue
    before it, so it is not a cycle anchor. The cycle cost is the cost from the first anchor run's first day up to the
    last run's first day: whole cycles only.
    """
    gap_limit = t.forecast_cadence_gap_days
    billed = [i for i, x in enumerate(series) if x > 1e-12]
    runs: List[List[int]] = []
    for i in billed:
        if runs and i - runs[-1][-1] <= t.forecast_run_bridge_days:
            runs[-1].append(i)
        else:
            runs.append([i])
    starts = [r[0] for r in runs]
    cycles = [b - a for a, b in zip(starts, starts[1:])]
    median = statistics.median(cycles) if len(starts) >= 3 else None
    short_runs = bool(runs) and statistics.median(r[-1] - r[0] + 1 for r in runs) <= gap_limit
    # Idle time between runs (next run's first day minus the previous run's last day): a periodic workload rests longer
    # than a week between bursts; frequent ad hoc use bridged into short runs does not.
    idle = [b[0] - a[-1] for a, b in zip(runs, runs[1:])]
    rest = statistics.median(idle) if idle else None
    # Recency reference: the upper quartile of cycles, so a stable two-runs-a-month cadence (7 and 24 days) stays
    # current between its runs while a stopped job still lapses about one cycle after its last run.
    reference = statistics.quantiles(cycles, n=4, method="inclusive")[-1] if len(cycles) >= 2 else (cycles[0] if cycles else None)
    current = reference is not None and (len(series) - 1 - starts[-1]) <= reference + gap_limit
    shape = short_runs and current and rest is not None
    # Each cycle is at least its own rest, so the median cycle and the longest gap are at least the median rest: the
    # rest alone decides.
    periodic = shape and rest > gap_limit
    weekly = shape and rest == gap_limit
    # A run within forecast_run_bridge_days of the basis start may continue before it (cut by the epoch or window
    # start), so it is not a cycle anchor.
    anchors = starts[1:] if starts and starts[0] < t.forecast_run_bridge_days else starts
    day = lambda i: (basis_start + dt.timedelta(days=i)).isoformat()
    return {
        "periodic": periodic, "weekly": weekly, "runs": len(runs), "cycles": max(0, len(anchors) - 1),
        "median_cycle_days": median,
        "cycle_cost": sum(series[anchors[0]:anchors[-1]]) if len(anchors) >= 2 else 0.0,
        "cycle_days": (anchors[-1] - anchors[0]) if len(anchors) >= 2 else 0,
        "window_start": day(anchors[0]) if anchors else None, "window_end": day(anchors[-1]) if anchors else None,
        "run_starts": [day(i) for i in anchors],
    }


class Modeler:
    """Forward-looking baseline model using zero-filled calendar-day cost history."""
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    def forecast_from_daily_cost(self, rows: Sequence[Dict[str, Any]], current_epoch_start: str, analysis_time: str, billing_watermark: Optional[str] = None, ingestion_cutoff: Optional[str] = None) -> Dict[str, Any]:
        """No-change DBU baseline for the current epoch from daily list-cost rows (all epochs in `rows`; only the current one forecasts).

        The basis is the settled, watermark-covered UTC days from the epoch start (at most 365 days back); today and
        unsettled days are excluded. The forecast is a recent-weighted whole-week run rate with a bounded trend
        adjustment, or the cadence model for periodic workloads. Unknown cost is None, never zero: unpriced usage,
        too few billed days and a non-positive run rate each set `dbu_forecast_status` and leave `forecast_cost_365`
        None. Raises ValueError on an invalid analysis time or epoch start, or on mixed currencies. Pure and
        deterministic: no wall clock is read.
        """
        analysis_dt = parse_datetime(analysis_time)
        current_start = parse_datetime(current_epoch_start)
        if not analysis_dt:
            raise ValueError(f"Invalid analysis_time: {analysis_time}")
        if not current_start:
            raise ValueError(f"Invalid current epoch start: {current_epoch_start}")

        # Unpriced usage rows (no list_prices match) carry no currency; they make cost unknown, not mixed.
        currencies = sorted({str(r.get("currency_code") or "USD") for r in rows if not _is_unpriced_only_row(r)})
        if len(currencies) > 1:
            raise ValueError(f"Mixed currencies cannot be aggregated: {currencies}")
        currency = currencies[0] if currencies else "USD"

        by_date: Dict[dt.date, float] = {}
        current_by_date: Dict[dt.date, float] = {}
        unpriced_by_date: Dict[dt.date, float] = {}
        # Billing corrections per day: (net DBU, retracted DBU, restated DBU); None when the row predates their collection.
        correction_by_date: Dict[dt.date, Optional[Tuple[float, float, float]]] = {}
        for r in rows:
            date_val = parse_datetime(r.get("usage_date"))
            if not date_val:
                continue
            d = date_val.date()
            cost = safe_float(r.get("list_cost"))
            by_date[d] = by_date.get(d, 0.0) + cost
            if d >= current_start.date():
                current_by_date[d] = current_by_date.get(d, 0.0) + cost
            unpriced = safe_float(r.get("unpriced_dbu_quantity"))
            if unpriced:
                unpriced_by_date[d] = unpriced_by_date.get(d, 0.0) + unpriced
            retracted, restated = optional_float(r.get("retracted_dbu_quantity")), optional_float(r.get("restated_dbu_quantity"))
            prior = correction_by_date.get(d, (0.0, 0.0, 0.0))
            if retracted is None or restated is None or prior is None:
                correction_by_date[d] = None
            else:
                correction_by_date[d] = (prior[0] + safe_float(r.get("dbu_quantity")), prior[1] + retracted, prior[2] + restated)

        analysis_date = analysis_dt.astimezone(dt.timezone.utc).date()
        # Half-open window [analysis_date-365, last complete day]. Today is incomplete; yesterday is complete only once
        # the billing watermark has passed its end (records are typically available within 12h, not guaranteed).
        window_start = analysis_date - dt.timedelta(days=365)
        # A day is complete once it has settled (billing_settle_hours after it ends) and, when known, the workspace's
        # billing watermark has passed it. Records arrive "typically within 12 hours"; the buffer covers that latency.
        settled_through = (settle_reference_time(analysis_time, ingestion_cutoff) - dt.timedelta(hours=self.t.billing_settle_hours)).date() - dt.timedelta(days=1)
        basis_end = min(analysis_date - dt.timedelta(days=1), settled_through)
        watermark_dt = parse_datetime(billing_watermark) if billing_watermark else None
        if watermark_dt is not None:
            complete_through = watermark_dt.astimezone(dt.timezone.utc).date() - dt.timedelta(days=1)
            basis_end = min(basis_end, complete_through)
            completeness = "COVERED_BY_WATERMARK"
        else:
            completeness = "UNVERIFIED_ASSUMED_COMPLETE_THROUGH_PRIOR_DAY"
        excluded_incomplete_days = (analysis_date - dt.timedelta(days=1) - basis_end).days
        basis_start = max(current_start.astimezone(dt.timezone.utc).date(), window_start)
        unpriced_365 = round(sum(v for d, v in unpriced_by_date.items() if window_start <= d <= basis_end), 6)
        unpriced_in_basis = round(sum(v for d, v in unpriced_by_date.items() if basis_start <= d <= basis_end), 6)
        # Correction share over the same settled window as the trailing actual: retracted DBU / DBU originally billed
        # (net + retracted - restated, i.e. the ORIGINAL records). Retractions of earlier restatements also count, so it
        # can exceed 1 when a day is corrected repeatedly. A quality signal only; unknown when any day predates its
        # collection.
        window_corrections = [v for d, v in correction_by_date.items() if window_start <= d <= basis_end]
        retracted_365: Optional[float] = None
        restated_365: Optional[float] = None
        correction_share: Optional[float] = None
        if all(v is not None for v in window_corrections):
            retracted_365 = round(sum((v[1] for v in window_corrections), 0.0), 6)
            restated_365 = round(sum((v[2] for v in window_corrections), 0.0), 6)
            original = sum(v[0] for v in window_corrections) + retracted_365 - restated_365
            correction_share = round(retracted_365 / original, 6) if original > 0 else None
        # Unpriced DBU has unknown cost: the trailing actual (and, when inside the forecast basis, the forecast) is
        # unknown rather than understated.
        actual: Optional[float] = None if unpriced_365 > 0 else round(sum(v for d, v in by_date.items() if window_start <= d <= basis_end), 2)
        completeness_meta = {
            "billing_watermark": watermark_dt.astimezone(dt.timezone.utc).isoformat() if watermark_dt else None,
            "billing_completeness": completeness,
            "billing_complete_through": basis_end.isoformat(),
            "excluded_incomplete_days": excluded_incomplete_days,
            "billing_settle_hours": self.t.billing_settle_hours,
            "unpriced_dbu_quantity_365": unpriced_365,
            "unpriced_dbu_quantity_in_basis": unpriced_in_basis,
            "dbu_pricing_status": "UNPRICED_USAGE" if unpriced_365 > 0 else "COMPLETE",
            "billing_retracted_dbu_quantity_365": retracted_365,
            "billing_restated_dbu_quantity_365": restated_365,
            "billing_correction_dbu_share_365": correction_share,
        }

        series: List[float] = []
        if basis_start <= basis_end:
            d = basis_start
            while d <= basis_end:
                series.append(current_by_date.get(d, 0.0))
                d += dt.timedelta(days=1)

        if not series:
            return {
                "current_cost_365": actual,
                "forecast_cost_365": None,
                "dbu_forecast_status": "UNPRICED_USAGE" if unpriced_in_basis > 0 else "INSUFFICIENT_ACTIVE_COST_DAYS",
                "currency": currency,
                "cost_history_days": 0,
                "active_cost_days": 0,
                "cost_cv_84": None,
                **completeness_meta,
                "forecast_method": MODEL_VERSION,
                "forecast_model": "RUN_RATE",
                "forecast_basis_start": None,
                "forecast_basis_end": None,
            }

        def mean(x: Sequence[float]) -> float:
            """Arithmetic mean; 0.0 for an empty series."""
            return statistics.mean(x) if x else 0.0

        recent = self.t.forecast_recent_days
        long = self.t.forecast_long_days
        rate = whole_weeks(series)  # rates use whole weeks only, so weekday/weekend mix carries no bias
        last28 = rate[-recent:]
        last84 = rate[-long:]
        prev28 = rate[-2*recent:-recent] if len(rate) >= 2*recent else []
        mean28, mean84 = mean(last28), mean(last84)
        daily = blended_daily_rate(rate, self.t)

        ratio = mean28 / mean(prev28) if prev28 and mean(prev28) > 0 else 1.0
        ratio = clamp(ratio, self.t.trend_ratio_floor, self.t.trend_ratio_ceiling)
        trend_adjustment_factor = 1.0 + self.t.trend_adjustment_weight * (ratio - 1.0)
        daily *= trend_adjustment_factor
        cv = statistics.pstdev(last84) / mean84 if len(last84) >= self.t.forecast_cv_min_days and mean84 > 0 else None
        active_cost_days = sum(1 for x in series if x > 1e-12)  # billed days; a correction-only (negative) day is not one
        rate_billed_days = sum(1 for x in rate[-long:] if x > 1e-12)  # the floor applies to the days the rate actually uses
        # The forward DBU cost is published only when it is known: every day priced, enough billed days to estimate a
        # run rate, and a positive run rate. Otherwise it is unknown (null), never $0 or a guess.
        if unpriced_in_basis > 0:
            forecast_status = "UNPRICED_USAGE"
        elif rate_billed_days < self.t.min_economics_cost_history_days:
            forecast_status = "INSUFFICIENT_ACTIVE_COST_DAYS"
        elif daily <= 0:
            forecast_status = "NON_POSITIVE_RUN_RATE"
        else:
            forecast_status = "COMPLETE"
        forecast_365 = round(daily * 365, 2) if forecast_status == "COMPLETE" else None
        cadence = billing_cadence(series, basis_start, self.t)
        model = "RUN_RATE"
        if cadence["periodic"] or (cadence["weekly"] and forecast_365 is None):
            # Periodic workload (short runs resting longer than a week; weekly only when the run rate has no
            # number): average whole run-to-run cycles, with no trend; fewer than forecast_min_cycles cycles is unknown.
            model = "CADENCE"
            ratio = None
            trend_adjustment_factor = 1.0
            cv = None
            if unpriced_in_basis > 0:
                forecast_status = "UNPRICED_USAGE"
            elif cadence["cycles"] < self.t.forecast_min_cycles:
                forecast_status = "INSUFFICIENT_BILLING_CYCLES"
            elif cadence["cycle_cost"] <= 0:
                forecast_status = "NON_POSITIVE_RUN_RATE"
            else:
                forecast_status = "COMPLETE"
            forecast_365 = round(cadence["cycle_cost"] / cadence["cycle_days"] * 365, 2) if forecast_status == "COMPLETE" else None
        if unpriced_in_basis > 0:
            # Unpriced days look like $0 days in the cost series: trend, run rates and stability are unknown, and the
            # AWS model gets no trend adjustment instead of a spurious downward one.
            ratio = None
            trend_adjustment_factor = 1.0
            cv = None
        return {
            "current_cost_365": actual,
            "forecast_cost_365": forecast_365,
            "forecast_model": model,
            "billing_runs": cadence["runs"],
            "billing_cycles": cadence["cycles"],
            "median_cycle_days": cadence["median_cycle_days"],
            "cadence_window_start": cadence["window_start"] if model == "CADENCE" else None,
            "cadence_window_end": cadence["window_end"] if model == "CADENCE" else None,
            "cadence_run_starts": cadence["run_starts"] if model == "CADENCE" else None,
            "dbu_forecast_status": forecast_status,
            "currency": currency,
            "cost_history_days": len(series),
            "active_cost_days": active_cost_days,
            "cost_cv_84": None if cv is None else round(cv, 4),
            "forecast_method": MODEL_VERSION,
            "run_rate_28": None if unpriced_in_basis > 0 else round(mean28 * 365, 2),
            "run_rate_84": None if unpriced_in_basis > 0 else round(mean84 * 365, 2),
            "trend_ratio": round_or_none(ratio, 6),
            "trend_adjustment_factor": round(trend_adjustment_factor, 6),
            "forecast_basis_start": basis_start.isoformat(),
            "forecast_basis_end": basis_end.isoformat(),
            **completeness_meta,
            "zero_cost_days_in_basis": sum(1 for x in series if abs(x) <= 1e-12),
            "rate_basis_days": min(len(rate), long),
            "rate_billed_days": rate_billed_days,
        }
