"""Projector: measurement evidence -> one normalized observation (format 2.1.0).

Pure and Realizer-owned. It plans the measurement again from the evidence (``realizer.analyzer.measurement``), so the
windows it reads are exactly the ones the bridge collected, and states what they show:

* the observed configuration (the epoch in effect at the as-of time), the application time (the start of the epoch
  that changed the step's knob, an exact time from ``system.compute.warehouses``) and the measurement window (the
  post-change whole weeks);
* the auto-stop replay counterfactual: the post-change days replayed with the package's source value. The idle a
  warehouse would have kept with the old value is the replay's added idle cluster-minutes, less the restarts the old
  value would have avoided at the package's restart assumption (the Publisher's cost of one restart per floor cluster);
  priced at the window's billed DBU cost per running cluster-minute and, for Pro and Classic, at the modeled AWS price
  of a cluster-hour. Realized = counterfactual - observed, holding every other knob at its observed value;
* readiness inputs: settled days since the change, the window's queries, the representative regime (whole weeks,
  query volume per day within ``VOLUME_RATIO_BAND`` of the pre-change window's) and the evidence quality;
* the financial-quality inputs of the window's billed rows.

Values the evidence cannot establish are ``null``; a ``null`` gate input never passes its gate.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Sequence

from realizer.analyzer.analysis import photon_bool_from_package
from realizer.analyzer.crosscheck import billed_cross_check, weekly_costs
from realizer.analyzer.measurement import (
    MeasurementPlan,
    daily_in,
    epoch_config,
    plan_measurement,
    replay_target,
    window_bounds,
    window_days,
)
from realizer.common.errors import InputError
from realizer.common.utils import money, ratio_string, utc_iso
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.governor.policy import package_currency

VOLUME_RATIO_BAND = (Decimal("0.75"), Decimal("1.25"))
REPLAY_METHOD = "AUTO_STOP_REPLAY"


def _d(value: Any) -> Optional[Decimal]:
    return None if value is None else Decimal(str(value))


def _check_windows(plan: MeasurementPlan, evidence: Mapping[str, Any]) -> None:
    """The evidence holds the windows the plan asks for (same epoch and days), or the projection would read other days."""
    for label, asked in plan.windows().items():
        held = (evidence.get("windows") or {}).get(label)
        if asked is None:
            continue
        if held is None or (held["epoch_start"], held["first_day"], held["last_day"]) != (asked["epoch_start"], asked["first_day"], asked["last_day"]):
            raise InputError(f"measurement evidence does not hold the planned {label} window")


def billed(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Billed totals of daily rows: DBU, priced cost (null when any DBU is unpriced), unpriced DBU and currencies."""
    dbu = sum((Decimal(str(r["dbu_quantity"])) for r in rows), Decimal("0"))
    unpriced = sum((Decimal(str(r["unpriced_dbu_quantity"])) for r in rows), Decimal("0"))
    currencies = sorted({str(r["currency_code"]) for r in rows if r.get("currency_code") is not None})
    cost = None if unpriced > 0 else sum((Decimal(str(r["list_cost"] or 0)) for r in rows), Decimal("0"))
    priced_dbu = dbu - unpriced
    by_currency: Dict[str, Decimal] = {}
    for r in rows:
        if r.get("currency_code") is not None:
            by_currency[str(r["currency_code"])] = by_currency.get(str(r["currency_code"]), Decimal("0")) + Decimal(str(r["dbu_quantity"]))
    return {"dbu": dbu, "cost": cost, "unpriced_dbu": unpriced, "currencies": currencies, "priced_dbu": priced_dbu,
            "majority_currency_dbu": max(by_currency.values(), default=Decimal("0"))}


def _replay_row(window: Mapping[str, Any], target: int) -> Dict[str, Decimal]:
    """The replay row of *target* in a window; a value without a row is a known zero (the collector omits zero rows)."""
    row = next((r for r in window.get("replay") or [] if int(r["target_min"]) == target), None)
    fields = ("added_idle_cluster_minutes", "avoided_starts", "removable_running_cluster_minutes", "additional_starts")  # zero when absent
    return {f: Decimal(str((row or {}).get(f, 0))) for f in fields}


def replay_net_cluster_minutes(row: Mapping[str, Decimal], replayed: int, applied: int, restart: Decimal) -> Decimal:
    """Cluster-minutes the replayed value would have cost more than the applied one on the replayed days (the saving of
    the change, negative for a cost increase). Replaying a higher value (or never stopping) keeps the warehouse up
    longer: its added idle less the restarts it avoids. Replaying a lower value stops it sooner: minus its removable
    idle, plus the restarts it adds. *row* is the replay row of the replayed value on days run at the applied value."""
    raises = int(replayed) == 0 or (int(applied) != 0 and int(replayed) > int(applied))
    if raises:
        return row["added_idle_cluster_minutes"] - row["avoided_starts"] * restart
    return -(row["removable_running_cluster_minutes"] - row["additional_starts"] * restart)


def restart_cost_cluster_minutes(config: Mapping[str, Any], assumptions: Mapping[str, Any]) -> Decimal:
    """The package's restart assumption: the cost of one restart in cluster-minutes, per floor cluster (the Publisher's
    Pro/Classic or Serverless value times max(1, min clusters))."""
    per_start = assumptions["restart_cost_cluster_minutes_serverless"] if config["warehouse_type"] == "SERVERLESS" else assumptions["restart_cost_cluster_minutes_pro_classic"]
    return Decimal(str(per_start)) * max(1, int(config["min_clusters"]))


def replay_economics(plan: MeasurementPlan, evidence: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The auto-stop replay of the post-change window: counterfactual, observed and realized cost, per component.
    ``None`` when the window has no running-time evidence or its billing is not priced in one currency."""
    post = (evidence.get("windows") or {}).get("post")
    if plan.post is None or plan.replayed_value is None or post is None or post.get("running") is None:
        return None
    config = epoch_config(plan.observed_epoch)
    running = Decimal(str(post["running"]["observed_running_cluster_minutes"]))
    target = replay_target(plan.replayed_value)
    row = _replay_row(post, target)
    restart = restart_cost_cluster_minutes(config, evidence["assumptions"])
    applied = int(config["auto_stop_minutes"])
    net = replay_net_cluster_minutes(row, plan.replayed_value, applied, restart)
    bill = billed(daily_in(evidence["daily_cost"], plan.post))
    if bill["cost"] is None or len(bill["currencies"]) > 1:
        return None
    dbu_rate = bill["cost"] / running if running > 0 else None
    if dbu_rate is None and bill["cost"] > 0:
        return None  # billed without running time: cost per cluster-minute unknown
    dbu_rate = dbu_rate or Decimal("0")
    aws = (evidence.get("aws") or {}).get("post") or {}
    aws_hourly = _d(aws.get("cluster_hourly_price"))
    aws_observed = None if aws_hourly is None else running / 60 * aws_hourly
    aws_realized = None if aws_hourly is None else net / 60 * aws_hourly
    dbu_realized = net * dbu_rate
    with_aws = aws_hourly is not None
    observed_cost = bill["cost"] + (aws_observed if with_aws else Decimal("0"))
    realized = dbu_realized + (aws_realized if with_aws else Decimal("0"))
    return {
        "currency": bill["currencies"][0] if bill["currencies"] else None,
        "observed_new_cost": observed_cost, "counterfactual_old_cost": observed_cost + realized,
        "dbu_realized": dbu_realized, "aws_realized": aws_realized, "cost_basis": "DBU_AND_MODELED_AWS" if with_aws else "DBU_ONLY",
        "detail": {
            "replayed_target_minutes": target, "added_idle_cluster_minutes": row["added_idle_cluster_minutes"],
            "avoided_starts": row["avoided_starts"], "restart_cost_cluster_minutes": restart, "net_cluster_minutes": net,
            "dbu_cost_per_cluster_minute": dbu_rate, "aws_cluster_hourly_price": aws_hourly, "aws_status": aws.get("status") or "UNAVAILABLE",
        },
    }


def window_summary(window: Optional[Mapping[str, Any]], daily: Sequence[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    """What one collected window shows: days, queries, running cluster-minutes, restarts and its billed cost."""
    if not window or window.get("running") is None:
        return None
    bill = billed(daily_in(daily, window))
    running = window["running"]
    return {
        "first_day": window["first_day"], "last_day": window["last_day"], "days": window_days(window),
        "queries": int(running["queries"]), "running_cluster_minutes": _fmt(running["observed_running_cluster_minutes"]),
        "restarts": int(running["restarts_for_queries"]), "billed_dbu": _fmt(bill["dbu"]), "billed_cost": money(bill["cost"]),
    }


def _fmt(value: Any, places: str = "0.001") -> Optional[str]:
    """A measured quantity as a canonical decimal string (``None`` stays ``None``)."""
    if value is None:
        return None
    return ratio_string(Decimal(str(value)).quantize(Decimal(places)))


def volume_ratio(pre: Optional[Mapping[str, Any]], post: Optional[Mapping[str, Any]]) -> Optional[Decimal]:
    """Post-change queries per day over the pre-change window's (None without both, or with no pre-change query)."""
    if not pre or not post or pre["queries"] <= 0:
        return None
    return (Decimal(post["queries"]) / post["days"]) / (Decimal(pre["queries"]) / pre["days"])


def evidence_quality(plan: MeasurementPlan, evidence: Mapping[str, Any]) -> str:
    """SUFFICIENT when the post-change window has running-time evidence over all its days, PARTIAL when the warehouse
    events cover only part of it, INSUFFICIENT without it (nothing to measure yet counts as INSUFFICIENT)."""
    post = (evidence.get("windows") or {}).get("post")
    if plan.post is None or post is None or post.get("running") is None:
        return "INSUFFICIENT"
    start, _ = window_bounds(plan.post)
    if post.get("window_start") is None or utc_iso(post["window_start"]) != start:
        return "PARTIAL"
    return "SUFFICIENT"


def financial_quality(plan: MeasurementPlan, evidence: Mapping[str, Any], replay: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Financial-quality inputs of the post-change window's billed rows (all ``null`` without a window)."""
    if plan.post is None:
        return {"attributable_spend_pct": None, "price_coverage_pct": None, "currency_consistency_pct": None,
                "correction_chain_complete": None, "material_cost_component_available": None}
    bill = billed(daily_in(evidence["daily_cost"], plan.post))
    priced = Decimal("100") if bill["dbu"] <= 0 else (bill["priced_dbu"] * 100 / bill["dbu"])
    consistent = Decimal("100") if bill["priced_dbu"] <= 0 else (bill["majority_currency_dbu"] * 100 / bill["priced_dbu"])
    return {
        # Usage rows are keyed by the warehouse id, so all of the window's billed SQL usage belongs to this warehouse.
        "attributable_spend_pct": "100",
        "price_coverage_pct": _fmt(priced, "0.01"),
        "currency_consistency_pct": _fmt(consistent, "0.01"),
        # Signed usage keeps retractions and restatements in the sums; the window is settled billing only.
        "correction_chain_complete": True,
        "material_cost_component_available": None if replay is None else replay["cost_basis"] == "DBU_AND_MODELED_AWS",
    }


def project_observation(package: Mapping[str, Any], evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """The observation (format 2.1.0) of *package*'s warehouse that *evidence* states."""
    warehouse = package["warehouse"]
    if (evidence["workspace_id"], evidence["warehouse_id"]) != (warehouse["workspace_id"], warehouse["warehouse_id"]):
        raise InputError(f"measurement evidence is for {evidence['workspace_id']}/{evidence['warehouse_id']}, "
                         f"the package for {warehouse['workspace_id']}/{warehouse['warehouse_id']}")
    plan = plan_measurement(package, evidence["epochs"], evidence["billing"]["settled_through"])
    _check_windows(plan, evidence)
    as_of = utc_iso(evidence["as_of_utc"])
    config = {**epoch_config(plan.observed_epoch), "enable_photon": photon_bool_from_package(package)}
    windows = evidence.get("windows") or {}
    post = window_summary(windows.get("post") if plan.post else None, evidence["daily_cost"])
    pre = window_summary(windows.get("pre") if plan.pre else None, evidence["daily_cost"])
    replay = replay_economics(plan, evidence)
    ratio = volume_ratio(pre, post)
    representative: Optional[bool] = None
    if post is not None and ratio is not None:
        representative = VOLUME_RATIO_BAND[0] <= ratio <= VOLUME_RATIO_BAND[1]
    window = window_bounds(plan.post) if plan.post else (None, None)
    economics = None
    if replay is not None:
        economics = {
            "currency": replay["currency"] or package_currency(package),
            "counterfactual_old_cost": money(replay["counterfactual_old_cost"]), "observed_new_cost": money(replay["observed_new_cost"]),
            "period_days": window_days(plan.post), "counterfactual_method": "CALIBRATED_PRIOR_STATE", "economic_basis": "ECONOMIC_COST",
            "expected_annual_savings_actual_scope": None, "dbu_realized": money(replay["dbu_realized"]),
            "aws_economic_realized": money(replay["aws_realized"]), "aws_cash_realized": None, "aws_commitment_freed": None,
            "aws_evidence_type": "MODELED" if replay["aws_realized"] is not None else None,
            "mature_ttm_realized_savings": None, "realized_savings_since_application": None,
        }
    cross_check = None
    if replay is not None:
        pre_rows = daily_in(evidence["daily_cost"], plan.pre)
        cross_check = billed_cross_check(
            replay_dbu_saving=replay["dbu_realized"],
            pre=None if pre is None else {"days": pre["days"], "billed_cost": billed(pre_rows)["cost"]},
            post={"days": post["days"], "billed_cost": billed(daily_in(evidence["daily_cost"], plan.post))["cost"]} if post else None,
            pre_weekly=None if plan.pre is None else weekly_costs(pre_rows, plan.pre["first_day"], window_days(plan.pre)),
            volume_ratio=ratio, other_knob_changes=plan.other_knob_changes)
    measurement = None
    if plan.step is not None:
        detail = None if replay is None else {k: (_fmt(v, "0.000001") if isinstance(v, Decimal) else v) for k, v in replay["detail"].items()}
        measurement = {
            "method": REPLAY_METHOD if replay is not None else None,
            "knob": plan.step["knob"], "source_value": plan.step["current_value"], "applied_value": plan.observed_epoch.get(plan.step["knob"]),
            "change_epoch_start": utc_iso(plan.change_epoch["epoch_start"]) if plan.change_epoch else None,
            "observed_epoch_start": utc_iso(plan.observed_epoch["epoch_start"]),
            "previous_epoch_start": utc_iso(plan.previous_epoch["epoch_start"]) if plan.previous_epoch else None,
            "not_measured_reason": plan.reason,
            "post_window": post, "pre_window": pre, "volume_ratio": _fmt(ratio, "0.0001"),
            "other_knob_changes": list(plan.other_knob_changes), "holds_other_knobs": replay is not None,
            "cost_basis": None if replay is None else replay["cost_basis"], "replay": detail,
            "cross_check": None if cross_check is None else {
                "outcome": cross_check["outcome"], "reason": cross_check["reason"],
                "replay_dbu_saving": money(cross_check["replay_dbu_saving"]), "billed_dbu_reduction": money(cross_check["billed_dbu_reduction"]),
                "difference_pct": _fmt(cross_check["difference_pct"], "0.01"), "noise": money(cross_check["noise"]),
                "pre_weekly_costs": [money(x) for x in cross_check["pre_weekly_costs"]],
            },
        }
    applied_at = utc_iso(plan.change_epoch["epoch_start"]) if plan.change_epoch is not None else None
    return {
        "observation_format_version": OBSERVATION_FORMAT_VERSION,
        "workspace_id": warehouse["workspace_id"], "warehouse_id": warehouse["warehouse_id"],
        "source": evidence["source"], "observed_at_utc": as_of, "recorded_at_utc": as_of,
        "applied_at_utc": applied_at, "effective_time_quality": "EXACT" if applied_at else "OBSERVED_AT_ONLY",
        "window_start_utc": window[0], "window_end_utc": window[1], "config": config,
        "elapsed_days": plan.settled_days_since_change, "query_count": None if post is None else post["queries"],
        "representative_regime": representative, "evidence_quality": evidence_quality(plan, evidence), "workload": {},
        "performance": {"normalized_p95_runtime_regression_pct": None, "reliability_pass": None, "correctness_pass": None, "guards": None},
        "financial_quality": financial_quality(plan, evidence, replay),
        "economics": economics, "overlap_count": 0, "origin_attribution": "UNKNOWN", "intent_state": "UNKNOWN",
        "intent_reference": None, "actor": None, "applied_scenario_id": None,
        "rollback_requested": False, "rollback_observed": False, "rollback_reason": None, "replacement_package_id": None,
        "correction": None, "measurement": measurement,
    }

