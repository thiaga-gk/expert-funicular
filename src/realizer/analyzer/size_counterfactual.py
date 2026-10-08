"""Size counterfactual: the post-change days of a size change priced at the old size.

Pure and Realizer-owned; the projector calls it for a size step (``MeasurementPlan.reads_size_runtime``) and writes its
block to the observation's ``measurement.size``. It holds every other knob at its observed value.

* Runtime ratio ``rho`` = the post-change queries' execution time at the post-change medians over the same queries at
  the pre-change medians (the size runtime the bridge read: per statement source seen in both windows, the median
  execution time of each window summed at the post-change counts). Measured only when the matched sources hold at least
  the package's canary p95 sample (``canary_p95_min_queries``) of post-change queries and at least
  ``SIZE_RUNTIME_MIN_COVERAGE`` of the post-change window's executed queries, and both sums are positive; otherwise
  ``SIZE_RUNTIME_UNMEASURED``. It is published next to the Publisher's runtime multiplier range of the change's direction.
* The old size's price: the pre-change window's billed DBU cost per running cluster-minute. It needs a previous epoch at
  the step's source size and the observed warehouse type, pre-change running time, pre-change billing fully priced and
  both windows' billing in one and the same currency, and the measured rate ratio (the new size's billed DBU cost per
  running cluster-minute over the old size's) within ``SIZE_RATE_PUBLISHED_BAND`` of the Publisher's published
  DBU-per-cluster-hour ratio of the two sizes (``published_rate_ratio``; the Publisher's own check before it prices a
  Pro/Classic size change); otherwise, or without a published ratio for either size, ``SOURCE_RATE_UNAVAILABLE``. A
  window the warehouse ran in with no billed day has an unknown cost, not a zero one: ``BILLING_INCOMPLETE``.
* The busy share ``b`` of the post-change running cluster-minutes (the Publisher's ``size_busy_share``); unknown is
  ``BUSY_SHARE_UNKNOWN``.
* Two bounds of the old size's running cluster-minutes, from the post-change running cluster-minutes ``R1``: elastic,
  ``R1 x (1 - b + b / rho)`` (busy time scales with the query time); inelastic, ``R1`` (slower queries fill idle gaps, as
  on a warehouse queried often). The claimed bound is the one with the smaller saving: elastic when ``rho >= 1``,
  inelastic otherwise.
* Pricing: counterfactual DBU = running x the old DBU rate; AWS = running / 60 x the pre-change window's modeled
  cluster-hour price; observed = the post-change billed cost + ``R1`` / 60 x the post-change price. With both AWS prices
  known (Serverless's 0 counts) the cost basis is ``DBU_AND_MODELED_AWS``, else ``DBU_ONLY``.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Sequence

from realizer.analyzer.analysis import candidate_by_id, normalize_value
from realizer.analyzer.measurement import MeasurementPlan, billed, daily_in, window_running
from realizer.common.utils import money, ratio_string
from realizer.config.settings import (
    SIZE_ORDER,
    SIZE_RATE_PUBLISHED_BAND,
    SIZE_RUNTIME_MIN_COVERAGE,
    SIZE_RUNTIME_MULTIPLIERS,
    SQL_DBU_PER_CLUSTER_HOUR,
    size_busy_share,
)
from realizer.contracts.observation import BILLING_INCOMPLETE, SIZE_BOUNDS

METHOD = "SIZE_RUNTIME"
# The candidate's own busy-runtime multiplier range, when its package states one (the Publisher estimator's name).
CANDIDATE_RANGE_KEY = "busy_runtime_multiplier_range"


@dataclass(frozen=True)
class SizeCounterfactual:
    """``block``: the observation's ``measurement.size`` (``None`` without size runtime evidence); ``economics``: the
    counterfactual in the replay's shape (``None`` when not measured); ``reason``: why it was not measured."""
    block: Optional[Dict[str, Any]]
    economics: Optional[Dict[str, Any]]
    reason: Optional[str]


def _d(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _fmt(value: Optional[Decimal], places: str = "0.000001") -> Optional[str]:
    return None if value is None else ratio_string(value.quantize(Decimal(places)))


def counterfactual_running(running: Decimal, busy_share: Decimal, rho: Decimal) -> Dict[str, Decimal]:
    """The old size's running cluster-minutes on both bounds: elastic ``running x (1 - b + b / rho)``, inelastic ``running``."""
    return dict(zip(SIZE_BOUNDS, (running * (1 - busy_share + busy_share / rho), running)))


def bound_used(rho: Decimal) -> str:
    """The bound with the smaller saving: elastic for queries no faster after the change, inelastic for faster ones."""
    return "ELASTIC" if rho >= 1 else "INELASTIC"


def runtime_ratio(size_runtime: Mapping[str, Any], min_queries: Any) -> Optional[Decimal]:
    """``rho``, or ``None`` when the matched sources are too few or cover too little of the post-change queries."""
    matched, executed = int(size_runtime["matched_post_queries"]), int(size_runtime["post_executed_queries"])
    at_post, at_pre = _d(size_runtime.get("post_ms_at_post_median")), _d(size_runtime.get("post_ms_at_pre_median"))
    if (min_queries is None or matched < Decimal(str(min_queries)) or matched < SIZE_RUNTIME_MIN_COVERAGE * executed
            or at_post is None or at_pre is None or at_post <= 0 or at_pre <= 0):
        return None
    return at_post / at_pre


def published_rate_ratio(source: str, applied: str) -> Optional[Decimal]:
    """The published DBU-per-cluster-hour ratio of *applied* over *source* (Publisher spelling); ``None`` without a
    published rate for either."""
    old, new = SQL_DBU_PER_CLUSTER_HOUR.get(source), SQL_DBU_PER_CLUSTER_HOUR.get(applied)
    return Decimal(str(new)) / Decimal(str(old)) if old and new else None


def within_published_band(measured: Optional[Decimal], published: Optional[Decimal], band: Any = None) -> Optional[bool]:
    """Whether a measured rate ratio is within *band* (the Publisher's ``size_rate_published_band`` the evidence recorded;
    ``SIZE_RATE_PUBLISHED_BAND`` when not given) of the published one (``None`` when either is unknown)."""
    if measured is None or published is None or published <= 0:
        return None
    limit = SIZE_RATE_PUBLISHED_BAND if band is None else Decimal(str(band))
    return abs(measured / published - 1) <= limit


def published_runtime_multiplier(package: Mapping[str, Any], step: Mapping[str, Any], source: str, applied: str) -> Dict[str, Any]:
    """The runtime multiplier range the Publisher priced the change with: the candidate's own when its package states one,
    else its thresholds' range for the direction (one size down slows queries, one size up speeds them)."""
    direction = "DOWNSIZE" if SIZE_ORDER.get(applied, 0) < SIZE_ORDER.get(source, 0) else "UPSIZE"
    candidate = candidate_by_id(package, step.get("candidate_id")) or {}
    stated = candidate.get(CANDIDATE_RANGE_KEY) or (candidate.get("independent_economics") or {}).get(CANDIDATE_RANGE_KEY)
    if isinstance(stated, Mapping) and all(stated.get(k) is not None for k in ("low", "base", "high")):
        values = dict(zip(("optimistic", "base", "conservative"), (stated["low"], stated["base"], stated["high"])))
        origin = "CANDIDATE"
    else:
        values, origin = SIZE_RUNTIME_MULTIPLIERS[direction], "PUBLISHER_THRESHOLDS"
    return {"source": origin, "direction": direction, **{k: _fmt(_d(v)) for k, v in values.items()}}


def _old_rate(plan: MeasurementPlan, evidence: Mapping[str, Any], source: str, observed_type: Any,
              post_bill: Mapping[str, Any]) -> Optional[Decimal]:
    """The pre-change window's billed DBU cost per running cluster-minute, or ``None`` when it is not the old size's."""
    previous, pre = plan.previous_epoch, (evidence.get("windows") or {}).get("pre")
    if (previous is None or plan.pre is None or pre is None or pre.get("running") is None
            or normalize_value("warehouse_size", previous.get("warehouse_size")) != source
            or normalize_value("warehouse_type", previous.get("warehouse_type")) != observed_type):
        return None
    running = Decimal(str(pre["running"]["observed_running_cluster_minutes"]))
    bill = billed(daily_in(evidence["daily_cost"], plan.pre), running)
    if running <= 0 or bill["cost"] is None or post_bill["cost"] is None or len(set(bill["currencies"]) | set(post_bill["currencies"])) > 1:
        return None
    return bill["cost"] / running


def size_counterfactual(package: Mapping[str, Any], plan: MeasurementPlan, evidence: Mapping[str, Any]) -> Optional[SizeCounterfactual]:
    """The size counterfactual of *plan*'s step; ``None`` for another knob or before a post-change window."""
    post = (evidence.get("windows") or {}).get("post")
    if not plan.reads_size_runtime or plan.post is None or post is None or post.get("running") is None:
        return None
    runtime = evidence.get("size_runtime")
    if runtime is None:
        return SizeCounterfactual(block=None, economics=None, reason="SIZE_RUNTIME_UNMEASURED")
    step, observed = plan.step or {}, plan.observed_epoch
    source = normalize_value("warehouse_size", step.get("current_value"))
    applied = normalize_value("warehouse_size", observed.get("warehouse_size"))
    observed_type = normalize_value("warehouse_type", observed.get("warehouse_type"))
    r1 = Decimal(str(post["running"]["observed_running_cluster_minutes"]))
    post_bill = billed(daily_in(evidence["daily_cost"], plan.post), r1)
    rho = runtime_ratio(runtime, (evidence.get("assumptions") or {}).get("canary_p95_min_queries"))
    old_rate = _old_rate(plan, evidence, source, observed_type, post_bill)
    new_rate = post_bill["cost"] / r1 if post_bill["cost"] is not None and r1 > 0 else None
    share = size_busy_share(runtime["post_busy_wall_minutes"], post["running"]["observed_running_wall_minutes"], r1, observed.get("min_clusters"))
    busy = None if share is None else Decimal(str(share))
    published = published_runtime_multiplier(package, step, source, applied)
    measured_ratio = new_rate / old_rate if new_rate is not None and old_rate else None
    published_ratio = published_rate_ratio(source, applied)
    in_band = within_published_band(measured_ratio, published_ratio, (evidence.get("assumptions") or {}).get("size_rate_published_band"))
    aws = evidence.get("aws") or {}
    old_hourly, new_hourly = _d((aws.get("pre") or {}).get("cluster_hourly_price")), _d((aws.get("post") or {}).get("cluster_hourly_price"))
    edges = sorted(Decimal(str(published[k])) for k in ("optimistic", "conservative"))
    executed = int(runtime["post_executed_queries"])
    block: Dict[str, Any] = {
        "runtime_ratio": _fmt(rho), "matched_sources": int(runtime["matched_sources"]),
        "matched_post_queries": int(runtime["matched_post_queries"]), "post_executed_queries": executed,
        "matched_query_coverage": _fmt(Decimal(int(runtime["matched_post_queries"])) / executed) if executed > 0 else None,
        "published_runtime_multiplier": published,
        "runtime_ratio_in_published_range": None if rho is None else edges[0] <= rho <= edges[1],
        "old_dbu_cost_per_cluster_minute": _fmt(old_rate), "new_dbu_cost_per_cluster_minute": _fmt(new_rate),
        "measured_rate_ratio": _fmt(measured_ratio), "published_rate_ratio": _fmt(published_ratio),
        "rate_ratio_within_published_band": in_band,
        "busy_share": _fmt(busy), "pre_busy_wall_minutes": _fmt(_d(runtime["pre_busy_wall_minutes"]), "0.001"),
        "post_busy_wall_minutes": _fmt(_d(runtime["post_busy_wall_minutes"]), "0.001"),
        "observed_running_cluster_minutes": _fmt(r1, "0.001"),
        "old_aws_cluster_hourly_price": _fmt(old_hourly), "new_aws_cluster_hourly_price": _fmt(new_hourly),
        "elastic": None, "inelastic": None, "bound_used": None,
    }
    pre_window = (evidence.get("windows") or {}).get("pre")
    pre_running = window_running(pre_window)
    billing_missing = post_bill["missing"] or (plan.pre is not None and billed(daily_in(evidence["daily_cost"], plan.pre), pre_running)["missing"])
    reason = ("SIZE_RUNTIME_UNMEASURED" if rho is None else BILLING_INCOMPLETE if billing_missing
              else "SOURCE_RATE_UNAVAILABLE" if old_rate is None or not in_band else "BUSY_SHARE_UNKNOWN" if busy is None else None)
    if reason is not None:
        return SizeCounterfactual(block=block, economics=None, reason=reason)
    assert rho is not None and old_rate is not None and busy is not None and post_bill["cost"] is not None
    with_aws = old_hourly is not None and new_hourly is not None
    aws_observed = r1 / 60 * new_hourly if with_aws else None
    observed_cost = post_bill["cost"] + (aws_observed or Decimal("0"))
    priced: Dict[str, Dict[str, Any]] = {}
    for name, running in counterfactual_running(r1, busy, rho).items():
        dbu = running * old_rate
        aws_cost = running / 60 * old_hourly if with_aws else None
        total = dbu + (aws_cost or Decimal("0"))
        priced[name] = {"running": running, "dbu": dbu, "aws": aws_cost, "total": total}
        block[name.lower()] = {"running_cluster_minutes": _fmt(running), "dbu_cost": money(dbu), "aws_cost": money(aws_cost),
                               "counterfactual_cost": money(total),  # realized as the economics state it: of the cent-rounded costs
                               "realized": money(Decimal(money(total)) - Decimal(money(observed_cost)))}
    used = bound_used(rho)
    block["bound_used"] = used
    chosen = priced[used]
    economics = {
        "currency": post_bill["currencies"][0] if post_bill["currencies"] else None,
        "observed_new_cost": observed_cost, "counterfactual_old_cost": chosen["total"],
        "dbu_realized": chosen["dbu"] - post_bill["cost"],
        "aws_realized": None if not with_aws else chosen["aws"] - aws_observed,
        "cost_basis": "DBU_AND_MODELED_AWS" if with_aws else "DBU_ONLY",
    }
    return SizeCounterfactual(block=block, economics=economics, reason=None)
