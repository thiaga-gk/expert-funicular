"""Realized to date: the realized savings of one application episode over the time its measurements cover.

Daily runs measure overlapping windows (the latest whole weeks after the change, up to twelve), so no single
measurement says what was realized since the application. Each instant takes the value of the latest measurement whose
window covers it, pro rata over that window's length, and realized to date is the signed sum over every covered
instant; a re-measured window (a billing restatement) replaces the earlier value for its days. The coverage is stated:
the first and last covered day, the days covered and the whole days between them that no measurement covers.

The episode is mature when its measurements cover ``MATURE_DAYS`` days; the trailing-twelve-month figure is then the sum
over the latest ``MATURE_DAYS`` covered days. Pure: it reads the measurement windows and amounts it is given.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from realizer.common.utils import money, parse_utc

DAY = dt.timedelta(days=1)
MATURE_DAYS = 365


@dataclass(frozen=True)
class MeasuredWindow:
    """One measurement of the episode: its window (UTC, end exclusive) and its signed realized savings over it."""
    start: dt.datetime
    end: dt.datetime
    realized: Decimal


def measured_window(start_utc: Optional[str], end_utc: Optional[str], realized: Optional[str]) -> Optional[MeasuredWindow]:
    """The measured window of a measurement payload's window and realized amount; ``None`` without both ends, without an
    amount, or for a window that does not end after it starts."""
    if not start_utc or not end_utc or realized is None:
        return None
    start, end = parse_utc(start_utc), parse_utc(end_utc)
    return MeasuredWindow(start, end, Decimal(str(realized))) if end > start else None


def _segments(windows: Sequence[MeasuredWindow]) -> List[Tuple[dt.datetime, dt.datetime, MeasuredWindow]]:
    """The covered time cut at every window boundary, each piece with the latest window (last in *windows*) covering it."""
    cuts = sorted({t for w in windows for t in (w.start, w.end)})
    out: List[Tuple[dt.datetime, dt.datetime, MeasuredWindow]] = []
    for a, b in zip(cuts, cuts[1:]):
        owner = next((w for w in reversed(windows) if w.start <= a and b <= w.end), None)
        if owner is not None:
            out.append((a, b, owner))
    return out


def _value(a: dt.datetime, b: dt.datetime, w: MeasuredWindow) -> Decimal:
    return w.realized * Decimal(int((b - a).total_seconds())) / Decimal(int((w.end - w.start).total_seconds()))


def _days(seconds: float) -> Decimal:
    return (Decimal(int(seconds)) / Decimal(86400)).quantize(Decimal("0.01"))


def realized_to_date(windows: Sequence[MeasuredWindow]) -> Optional[Dict[str, object]]:
    """Realized to date over *windows* (in recording order, the latest last); ``None`` without any.

    ``realized_to_date`` (signed, money), ``first_day``/``last_day`` (the UTC days of the first and last covered instant),
    ``days_covered``, ``uncovered_days`` (whole UTC days between them no window covers), ``mature`` (covered days at least
    ``MATURE_DAYS``) and ``mature_ttm_realized_savings`` (the latest ``MATURE_DAYS`` covered days; ``None`` before)."""
    if not windows:
        return None
    segments = _segments(windows)
    total = sum((_value(a, b, w) for a, b, w in segments), Decimal("0"))
    covered = sum((b - a).total_seconds() for a, b, _ in segments)
    first, last = segments[0][0], segments[-1][1]
    uncovered = 0
    day = dt.datetime.combine(first.date(), dt.time(), dt.timezone.utc)
    while day < last:
        if day >= first and day + DAY <= last and not any(a < day + DAY and day < b for a, b, _ in segments):
            uncovered += 1
        day += DAY
    mature = covered >= MATURE_DAYS * 86400
    ttm: Optional[Decimal] = None
    if mature:
        # The latest MATURE_DAYS covered days, counted back from the last covered instant over covered time only.
        remaining = dt.timedelta(days=MATURE_DAYS)
        ttm = Decimal("0")
        for a, b, w in reversed(segments):
            take = min(b - a, remaining)
            ttm += _value(b - take, b, w)
            remaining -= take
            if remaining <= dt.timedelta(0):
                break
    return {
        "realized_to_date": money(total),
        "first_day": first.date().isoformat(),
        "last_day": (last - dt.timedelta(microseconds=1)).date().isoformat(),
        "days_covered": str(_days(covered)),
        "uncovered_days": uncovered,
        "mature": mature,
        "mature_ttm_realized_savings": money(ttm),
    }
