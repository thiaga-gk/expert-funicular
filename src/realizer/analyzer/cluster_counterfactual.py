"""Cluster-count counterfactual: what the running minutes by cluster count show for a min- or max-cluster change.

Pure and Realizer-owned. The post-change window's levels (``windows.post.levels``: running minutes per cluster count,
the running-idle collector's RUNNING segments) are read for the measured step's knob, its source value (the package's)
and its applied value (the observed epoch's):

* a floor cut (min clusters lowered): the counterfactual runs every level at least at the old floor, so its
  cluster-minutes are the sum of running minutes x max(cluster count, old floor) and the observed ones the sum of
  running minutes x cluster count. The removed cluster-minutes are priced like the auto-stop replay: at the post window's
  billed DBU cost per running cluster-minute and, with a modeled AWS price, at the post epoch's cluster-hour price. This
  is the floor cut's counterfactual (method ``FLOOR_LEVELS``), every other knob held at its observed value. Without
  levels there is none (``LEVELS_UNAVAILABLE``); a floor raise is never measured (``FLOOR_RAISED``): minutes at the
  higher new floor cannot say how many clusters the lower old floor would have run;
* a capacity add (max clusters raised): the measured cost is the cluster-minutes above the old cap, running minutes x
  (cluster count - old cap) for the levels above it, a level above the new cap (a recycling transient) counted at most up
  to the new cap. It is a cost, not a saving: the performance verdict judges it;
* a cap cut (max clusters lowered): the minutes at the new cap (a level above it counted as at it) and the upper bound
  of what the old cap could have added, those minutes x (old cap - new cap) cluster-minutes. Evidence only, no saving.

Every priced quantity is annualized x 365 / the post window's days. Values the evidence cannot establish are ``None``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from realizer.analyzer.measurement import LEVEL_KNOBS, MeasurementPlan, window_days
from realizer.common.utils import money, ratio_string
from realizer.contracts.observation import BILLING_INCOMPLETE, LEVELS_BASES

FLOOR_METHOD = "FLOOR_LEVELS"
# What the priced cluster-minutes are: removed by a floor cut, above the old cap of a capacity add, the upper bound of
# what the old cap of a cap cut could have added.
FLOOR_CUT_REMOVED, ABOVE_OLD_CAP, OLD_CAP_UPPER_BOUND = LEVELS_BASES
LEVELS_UNAVAILABLE = "LEVELS_UNAVAILABLE"
FLOOR_RAISED = "FLOOR_RAISED"
ANNUAL_DAYS = Decimal("365")

Levels = List[Tuple[int, Decimal]]


@dataclass(frozen=True)
class ClusterLevels:
    """What the levels show: ``block`` (the observation's ``measurement.levels``, formatted), ``counterfactual`` (a floor
    cut's, in the auto-stop replay's shape: currency, observed and counterfactual cost, DBU and AWS realized, cost basis)
    and ``reason`` (why a floor change has no counterfactual)."""
    block: Optional[Dict[str, Any]]
    counterfactual: Optional[Dict[str, Any]]
    reason: Optional[str]


def window_levels(window: Optional[Mapping[str, Any]]) -> Optional[Levels]:
    """A window's ``(cluster count, running minutes)`` by cluster count; ``None`` when not read or empty."""
    rows = (window or {}).get("levels")
    if not rows:
        return None
    return sorted((int(r["cluster_count"]), Decimal(str(r["running_minutes"]))) for r in rows)


def observed_cluster_minutes(levels: Levels) -> Decimal:
    return sum((minutes * count for count, minutes in levels), Decimal("0"))


def floor_cluster_minutes(levels: Levels, floor: int) -> Decimal:
    """The cluster-minutes of *levels* with every level run at least at *floor* clusters."""
    return sum((minutes * max(count, floor) for count, minutes in levels), Decimal("0"))


def above_cap_cluster_minutes(levels: Levels, old_cap: int, new_cap: int) -> Decimal:
    """The cluster-minutes above *old_cap*, a level above *new_cap* counted at most up to it."""
    return sum((minutes * (min(count, new_cap) - old_cap) for count, minutes in levels if count > old_cap), Decimal("0"))


def minutes_at_cap(levels: Levels, cap: int) -> Decimal:
    """The running minutes at *cap* clusters (a level above it counted as at it)."""
    return sum((minutes for count, minutes in levels if count >= cap), Decimal("0"))


def _qty(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else ratio_string(value.quantize(Decimal("0.001")))


def _rates(evidence: Mapping[str, Any], bill: Mapping[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
    """``(DBU cost per running cluster-minute, AWS cluster-hour price, AWS status)`` of the post window. The DBU rate is
    ``None`` when the billing is not priced in one currency, or billed without running time."""
    post = (evidence.get("windows") or {}).get("post") or {}
    aws = (evidence.get("aws") or {}).get("post") or {}
    hourly = aws.get("cluster_hourly_price")
    aws_hourly = None if hourly is None else Decimal(str(hourly))
    running = None if post.get("running") is None else Decimal(str(post["running"]["observed_running_cluster_minutes"]))
    rate: Optional[Decimal] = None
    if bill.get("cost") is not None and len(bill.get("currencies") or []) <= 1 and running is not None:
        if running > 0:
            rate = bill["cost"] / running
        elif bill["cost"] == 0:
            rate = Decimal("0")
    return rate, aws_hourly, str(aws.get("status") or "UNAVAILABLE")


def cluster_levels(plan: MeasurementPlan, evidence: Mapping[str, Any], bill: Mapping[str, Any]) -> ClusterLevels:
    """What the post window's levels show for a min- or max-cluster step (module docstring). *bill* is the post window's
    billed totals (``measurement.billed``: ``cost``, ``currencies``). Nothing for another knob or before a post window."""
    if plan.step is None or plan.step.get("knob") not in LEVEL_KNOBS or plan.post is None:
        return ClusterLevels(None, None, None)
    knob = str(plan.step["knob"])
    floor = knob == "min_clusters"
    windows = evidence.get("windows") or {}
    post = window_levels(windows.get("post"))
    if post is None:
        return ClusterLevels(None, None, LEVELS_UNAVAILABLE if floor else None)
    source, applied = int(plan.step["current_value"]), int(plan.observed_epoch[knob])
    observed = observed_cluster_minutes(post)
    basis: Optional[str] = None
    priced: Optional[Decimal] = None
    counterfactual_minutes = at_cap = None
    if floor and applied < source:
        basis, counterfactual_minutes = FLOOR_CUT_REMOVED, floor_cluster_minutes(post, source)
        priced = counterfactual_minutes - observed
    elif not floor and applied > source:
        basis, priced = ABOVE_OLD_CAP, above_cap_cluster_minutes(post, source, applied)
    elif not floor and applied < source:
        at_cap = minutes_at_cap(post, applied)
        basis, priced = OLD_CAP_UPPER_BOUND, at_cap * (source - applied)
    rate, aws_hourly, aws_status = _rates(evidence, bill)
    days = window_days(plan.post)
    dbu = None if priced is None or rate is None else priced * rate
    aws = None if dbu is None or aws_hourly is None else priced / 60 * aws_hourly  # type: ignore[operator]
    total = None if aws is None else dbu + aws  # type: ignore[operator]
    annual = lambda v: None if v is None else v * ANNUAL_DAYS / days
    currency = (bill.get("currencies") or [None])[0] if rate is not None else None
    block = {
        "basis": basis,
        "pre_levels": _format_levels(window_levels(windows.get("pre"))), "post_levels": _format_levels(post),
        "observed_cluster_minutes": _qty(observed), "counterfactual_cluster_minutes": _qty(counterfactual_minutes),
        "minutes_at_new_cap": _qty(at_cap), "cluster_minutes": _qty(priced), "days": days, "currency": currency,
        "dbu_cost_per_cluster_minute": None if rate is None else ratio_string(rate.quantize(Decimal("0.000001"))),
        "aws_cluster_hourly_price": None if aws_hourly is None else ratio_string(aws_hourly.quantize(Decimal("0.000001"))),
        "aws_status": aws_status,
        "dbu_cost": money(dbu), "aws_cost": money(aws), "total_cost": money(total),
        "annualized_dbu_cost": money(annual(dbu)), "annualized_total_cost": money(annual(total)),
    }
    if floor and applied > source:
        return ClusterLevels(block, None, FLOOR_RAISED)
    counterfactual = None
    if basis == FLOOR_CUT_REMOVED and dbu is not None:
        running = Decimal(str(windows["post"]["running"]["observed_running_cluster_minutes"]))
        with_aws = aws_hourly is not None
        observed_cost = bill["cost"] + (running / 60 * aws_hourly if with_aws else Decimal("0"))  # type: ignore[operator]
        realized = dbu + (aws if with_aws else Decimal("0"))  # type: ignore[operator]
        counterfactual = {"currency": currency, "observed_new_cost": observed_cost, "counterfactual_old_cost": observed_cost + realized,
                          "dbu_realized": dbu, "aws_realized": aws, "cost_basis": "DBU_AND_MODELED_AWS" if with_aws else "DBU_ONLY"}
    # A floor cut whose post window has running time and no billed day cannot be priced: its cost is unknown, not zero.
    return ClusterLevels(block, counterfactual, BILLING_INCOMPLETE if basis == FLOOR_CUT_REMOVED and bill.get("missing") else None)


def _format_levels(levels: Optional[Sequence[Tuple[int, Decimal]]]) -> Optional[List[Dict[str, Any]]]:
    return None if levels is None else [{"cluster_count": count, "running_minutes": _qty(minutes)} for count, minutes in levels]


def capacity_cost(levels: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """A capacity add's measured cost for the performance verdict: ``(annualized, component)`` of the cluster-minutes
    above the old cap, the total with a modeled AWS price else DBU; ``None`` without a priced levels block of that basis."""
    if not isinstance(levels, Mapping) or levels.get("basis") != ABOVE_OLD_CAP:
        return None
    if levels.get("annualized_total_cost") is not None:
        return {"annualized": levels["annualized_total_cost"], "component": "total"}
    if levels.get("annualized_dbu_cost") is not None:
        return {"annualized": levels["annualized_dbu_cost"], "component": "dbu"}
    return None


def old_cap_upper_bound(levels: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """A cap cut's evidence for the capacity verdict: the minutes at the new cap, the cluster-minutes the old cap could
    have added at most and their annualized cost (total with a modeled AWS price, else DBU); ``None`` without levels."""
    if not isinstance(levels, Mapping) or levels.get("basis") != OLD_CAP_UPPER_BOUND:
        return None
    component = "total" if levels.get("annualized_total_cost") is not None else "dbu"
    return {"minutes_at_new_cap": levels.get("minutes_at_new_cap"), "cluster_minutes": levels.get("cluster_minutes"),
            "annualized_cost": levels.get(f"annualized_{component}_cost"), "component": component}
