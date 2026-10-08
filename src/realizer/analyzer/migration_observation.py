"""The observed delta of a warehouse-type migration (PRO or CLASSIC to SERVERLESS and back), observe-only.

A migration has no counterfactual model: there is no replay of what the old type would have billed on the new type's
days, and no reverse model that prices the query mix on the type the warehouse left. So the Realizer reports what the bill
did and claims nothing. The baseline is the pre-change window at the pre-change query mix, the same one the billed change
of a knob without a replay uses (``realizer.analyzer.projector.billed_change``): its billed cost per day times the volume
ratio times the post-change days, plus its modeled AWS cost the same way. Against it stands the post-change window's billed
cost and its modeled AWS cost; a Serverless window has a true zero AWS cost (the customer pays no cloud compute), an
unknown AWS price (``UNAVAILABLE``) leaves the AWS component out, and then the economics are DBU only.

The delta is the observation's economics (``counterfactual_old_cost`` the baseline, ``observed_new_cost`` the post-change
window, the components as signed reductions, positive = saving) with the diagnostic counterfactual method
``CORRELATED_ONLY``: the baseline is the pre-change window's own cost carried forward, so whatever else moved the bill is in
the delta, and it is never claim-eligible. The Estimator reports the delta against the scenario's informational range and
attributes nothing (hard disqualifier ``NO_REVERSE_MIGRATION_MODEL``).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from realizer.analyzer.measurement import MeasurementPlan, window_days
from realizer.common.utils import money
from realizer.contracts.observation import MIGRATION_METHOD, NO_REVERSE_MIGRATION_MODEL, is_migration_observed

__all__ = ["MIGRATION_METHOD", "NO_REVERSE_MIGRATION_MODEL", "is_migration_observed", "migration_delta", "window_aws_hourly"]

MIGRATION_KNOB = "warehouse_type"
# The baseline is the pre-change window carried forward, so the method is a diagnostic one (never claim-eligible).
MIGRATION_COUNTERFACTUAL_METHOD = "CORRELATED_ONLY"
NOT_CUSTOMER_BILLED = "NOT_CUSTOMER_BILLED"


def window_aws_hourly(price: Optional[Mapping[str, Any]]) -> Optional[Decimal]:
    """A window's AWS cluster-hour price in a billed change: its modeled customer price; a true zero for a window the
    customer pays no cloud compute for (Serverless, ``NOT_CUSTOMER_BILLED``); ``None`` when it is not known."""
    if not price:
        return None
    status, hourly = price.get("status"), price.get("cluster_hourly_price")
    if status == "MODELED" and hourly is not None:
        return Decimal(str(hourly))
    if status == NOT_CUSTOMER_BILLED:
        return Decimal("0")
    return None


def migration_delta(plan: MeasurementPlan, evidence: Mapping[str, Any], ratio: Optional[Decimal],
                    fallback_currency: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The observed delta of a warehouse-type step: the observation's ``economics`` block and the cost basis, or ``None``
    when the step is not a type change or the evidence cannot state it (without both windows' running time, a volume ratio,
    or billing priced in one currency)."""
    from realizer.analyzer.projector import window_bill  # the projector calls this module: imported late

    if plan.step is None or str(plan.step.get("knob")) != MIGRATION_KNOB or plan.post is None or plan.pre is None or ratio is None:
        return None
    windows = evidence.get("windows") or {}
    pre, post = windows.get("pre"), windows.get("post")
    if not pre or not post or pre.get("running") is None or post.get("running") is None:
        return None
    pre_bill, post_bill = window_bill(evidence["daily_cost"], plan.pre, pre), window_bill(evidence["daily_cost"], plan.post, post)
    currencies = sorted(set(pre_bill["currencies"]) | set(post_bill["currencies"]))
    if pre_bill["cost"] is None or post_bill["cost"] is None or len(currencies) > 1:
        return None
    days = window_days(plan.post)
    scale = ratio * days / window_days(plan.pre)
    aws = evidence.get("aws") or {}
    pre_hourly, post_hourly = window_aws_hourly(aws.get("pre")), window_aws_hourly(aws.get("post"))
    old_dbu, new_dbu = pre_bill["cost"] * scale, post_bill["cost"]
    old_aws = new_aws = None
    if pre_hourly is not None and post_hourly is not None:
        old_aws = Decimal(str(pre["running"]["observed_running_cluster_minutes"])) / 60 * pre_hourly * scale
        new_aws = Decimal(str(post["running"]["observed_running_cluster_minutes"])) / 60 * post_hourly
    with_aws = old_aws is not None
    old_cost = old_dbu + (old_aws if with_aws else Decimal("0"))
    new_cost = new_dbu + (new_aws if with_aws else Decimal("0"))
    economics = {
        "currency": currencies[0] if currencies else fallback_currency,
        "counterfactual_old_cost": money(old_cost), "observed_new_cost": money(new_cost), "period_days": days,
        "counterfactual_method": MIGRATION_COUNTERFACTUAL_METHOD, "economic_basis": "ECONOMIC_COST",
        "expected_annual_savings_actual_scope": None, "dbu_realized": money(old_dbu - new_dbu),
        "aws_economic_realized": money(old_aws - new_aws) if with_aws else None, "aws_cash_realized": None, "aws_commitment_freed": None,
        "aws_evidence_type": "MODELED" if with_aws else None,
    }
    return {"economics": economics, "cost_basis": "DBU_AND_MODELED_AWS" if with_aws else "DBU_ONLY"}
