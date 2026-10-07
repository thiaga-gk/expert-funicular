"""Billed cross-check: the replay's saving checked against billed cost before and after the change.

The replay counterfactual repeats the Publisher's own arithmetic and assumptions (restart billing, cost per
cluster-minute) on post-change data, so an error in a shared assumption cancels out and never shows. The billed
comparison measures the same saving without them: the pre-change window's billed DBU cost per day, scaled to the
post-change window's query volume and length, minus the post-change window's billed cost. Pure.

Outcome (DBU only; AWS is not billed in Databricks data):

* ``AGREES``: same sign as the replay and within ``AGREEMENT_BAND`` of it;
* ``INCONCLUSIVE``: the comparison cannot resolve the saving, which does not block a claim. Reasons: no pre-change window
  (``NO_PRE_WINDOW``), fewer than two pre-change weeks to estimate the noise (``NOISE_UNKNOWN``), another decision knob
  changed too (``OTHER_KNOB_CHANGED``: billed cost cannot separate the knobs), query volume moved outside
  ``VOLUME_BAND`` (``VOLUME_SHIFT``), billing not priced in one currency (``BILLING_INCOMPLETE``), or the replay saving
  is below ``NOISE_MULTIPLE`` times the noise of the post-change window's billed total (``BELOW_NOISE``: the
  pre-change weekly billed cost's population standard deviation times the square root of the post-change weeks);
* ``DISAGREES``: otherwise. The measurement then stays out of CLAIMABLE until a person reviews it.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence

from realizer.config.settings import VOLUME_RATIO_BAND as VOLUME_BAND

AGREEMENT_BAND = Decimal("0.25")
NOISE_MULTIPLE = Decimal("2")
OUTCOMES = ("AGREES", "DISAGREES", "INCONCLUSIVE")
REASONS = ("NO_PRE_WINDOW", "NOISE_UNKNOWN", "OTHER_KNOB_CHANGED", "VOLUME_SHIFT", "BILLING_INCOMPLETE", "BELOW_NOISE")


def weekly_costs(daily: Sequence[Mapping[str, Any]], first_day: str, days: int) -> Optional[List[Decimal]]:
    """Billed cost of each 7-day block from *first_day* (a day without a row costs 0); None when a day is unpriced."""
    import datetime as dt
    start = dt.date.fromisoformat(first_day)
    weeks = [Decimal("0")] * (days // 7)
    for r in daily:
        offset = (dt.date.fromisoformat(r["usage_date"]) - start).days
        if 0 <= offset < len(weeks) * 7:
            if r.get("list_cost") is None or float(r.get("unpriced_dbu_quantity") or 0) > 0:
                return None
            weeks[offset // 7] += Decimal(str(r["list_cost"]))
    return weeks


def _pstdev(values: Sequence[Decimal]) -> Decimal:
    mean = sum(values, Decimal("0")) / len(values)
    return Decimal(str(math.sqrt(float(sum(((v - mean) ** 2 for v in values), Decimal("0")) / len(values)))))


def billed_cross_check(*, replay_dbu_saving: Optional[Decimal], pre: Optional[Mapping[str, Any]], post: Optional[Mapping[str, Any]],
                       pre_weekly: Optional[Sequence[Decimal]], volume_ratio: Optional[Decimal], other_knob_changes: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The cross-check of one measurement. *pre* and *post* hold ``days`` and ``billed_cost`` (a decimal, None when not
    priced); *pre_weekly* the pre-change window's weekly billed costs. None without a replay saving to check."""
    if replay_dbu_saving is None or post is None:
        return None
    out: Dict[str, Any] = {"outcome": "INCONCLUSIVE", "reason": None, "replay_dbu_saving": replay_dbu_saving,
                           "billed_dbu_reduction": None, "difference_pct": None, "noise": None, "pre_weekly_costs": list(pre_weekly or [])}

    def inconclusive(reason: str) -> Dict[str, Any]:
        return {**out, "reason": reason}

    if pre is None:
        return inconclusive("NO_PRE_WINDOW")
    if pre.get("billed_cost") is None or post.get("billed_cost") is None or pre_weekly is None:
        return inconclusive("BILLING_INCOMPLETE")
    reduction = Decimal(pre["billed_cost"]) / pre["days"] * (volume_ratio or Decimal("1")) * post["days"] - Decimal(post["billed_cost"])
    out["billed_dbu_reduction"] = reduction
    if replay_dbu_saving != 0:
        out["difference_pct"] = (reduction - replay_dbu_saving).copy_abs() * 100 / replay_dbu_saving.copy_abs()
    if other_knob_changes:
        return inconclusive("OTHER_KNOB_CHANGED")
    if volume_ratio is None or not VOLUME_BAND[0] <= volume_ratio <= VOLUME_BAND[1]:
        return inconclusive("VOLUME_SHIFT")
    if len(pre_weekly) < 2:
        return inconclusive("NOISE_UNKNOWN")
    noise = _pstdev(pre_weekly) * Decimal(str(math.sqrt(post["days"] / 7)))
    out["noise"] = noise
    if replay_dbu_saving.copy_abs() < NOISE_MULTIPLE * noise:
        return inconclusive("BELOW_NOISE")
    same_sign = (reduction > 0) == (replay_dbu_saving > 0) or reduction == replay_dbu_saving
    agrees = same_sign and (reduction - replay_dbu_saving).copy_abs() <= AGREEMENT_BAND * replay_dbu_saving.copy_abs()
    return {**out, "outcome": "AGREES" if agrees else "DISAGREES"}
