"""Estimator performance verdicts: steps that are not a saving.

A step whose role is ``PERFORMANCE`` (a capacity add priced as a cost increase) or ``CAPACITY`` (a cut the Publisher
prices at zero) is judged on queueing and p95 before and after the change, not against a savings range. Its cost change
is reported signed (``cost_change``: a capacity add's cluster-minutes above the old cap when the running minutes by
cluster count were read, else the billed change) and never counted as realized savings; attribution claims nothing for
it. A cap cut's verdict also states what the old cap could have added at most (``old_cap_upper_bound``).

- ``PERFORMANCE_VALIDATED``: the queued share fell to at most half its pre-change value or to the safe level, and p95
  on the shared sources did not rise past the canary rule (previous x (1 + the regression percentage)); the detail is
  ``PERFORMANCE_VALIDATED_COST_ABOVE_MODEL`` (review required) when the annualized cost increase exceeds
  ``COST_REVIEW_FACTOR`` x the modeled increase.
- ``CAPACITY_VALIDATED``: the queued share after the change is at or below the safe level, or not above its value before.
- Otherwise ``MONITORING`` with the reason (``PERFORMANCE_NOT_IMPROVED``, ``CAPACITY_QUEUEING_REGRESSED``) and
  re-optimization recommended; without guard evidence ``MONITORING`` (``PERFORMANCE_UNVERIFIED``, ``CAPACITY_UNVERIFIED``).

The safe level and the regression percentage are the package's cost-profile guard values (``guard_thresholds``).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Optional

from realizer.analyzer.analysis import candidate_by_id
from realizer.analyzer.cluster_counterfactual import capacity_cost, old_cap_upper_bound
from realizer.common.utils import decimal, money
from realizer.config.settings import guard_thresholds
from realizer.contracts import realization as rc

NON_SAVINGS_ROLES = frozenset({rc.StepRole.PERFORMANCE.value, rc.StepRole.CAPACITY.value})
# How far above the modeled cost increase a validated performance step may cost before it is flagged for review.
COST_REVIEW_FACTOR = Decimal("1.25")


def _guard(observation: Mapping[str, Any], period: str, metric: str) -> Optional[Decimal]:
    guards = ((observation.get("performance") or {}).get("guards") or {})
    row = guards.get(period) if isinstance(guards, Mapping) else None
    return decimal(row.get(metric)) if isinstance(row, Mapping) else None


def _modeled_increase(package: Mapping[str, Any], step_candidate_id: Any, component: str) -> Optional[Decimal]:
    """The modeled annual cost increase of the step: minus its candidate's savings base (``None`` when not priced)."""
    candidate = candidate_by_id(package, step_candidate_id) or {}
    block = ((candidate.get("independent_economics") or {}).get("savings_365d") or {}).get(component)
    base = decimal(block.get("base")) if isinstance(block, Mapping) else None
    return None if base is None else -base


def cost_change(package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any]) -> Dict[str, Any]:
    """The step's observed cost change, annualized and signed (positive = cost went up): for a capacity add measured
    with the running minutes by cluster count, the cluster-minutes above the old cap (``CLUSTER_LEVELS_ABOVE_OLD_CAP``,
    total with a modeled AWS price, else DBU); else the billed pre/post change at the pre-change query mix of a live
    measurement (total, else DBU); else the caller's counterfactual minus observed cost. The billed change of the same
    component is reported next to it (``billed_annualized_cost_increase``), and the modeled increase it is compared
    with."""
    increase: Optional[Decimal] = None
    basis: Optional[str] = None
    component = "total"
    measurement = observation.get("measurement") or {}
    billed = measurement.get("billed_change")
    econ = observation.get("economics") or {}
    measured = capacity_cost(measurement.get("levels"))
    if measured is not None:
        component, increase, basis = measured["component"], decimal(measured["annualized"]), str(measurement["levels"]["basis"])
    elif isinstance(billed, Mapping) and (billed.get("annualized_total") is not None or billed.get("annualized_dbu") is not None):
        component = "total" if billed.get("annualized_total") is not None else "dbu"
        increase, basis = decimal(billed.get(f"annualized_{component}")), str(billed.get("basis"))
    elif econ.get("counterfactual_old_cost") is not None and econ.get("observed_new_cost") is not None and int(econ.get("period_days") or 0) > 0:
        realized = decimal(econ["counterfactual_old_cost"]) - decimal(econ["observed_new_cost"])  # type: ignore[operator]
        increase, basis = -realized * Decimal("365") / Decimal(int(econ["period_days"])), "COUNTERFACTUAL"
    modeled = _modeled_increase(package, app.get("candidate_id"), component)
    review_above = modeled * COST_REVIEW_FACTOR if modeled is not None and modeled > 0 else None
    billed_increase = billed.get(f"annualized_{component}") if isinstance(billed, Mapping) else None
    return {"annualized_cost_increase": money(increase), "basis": basis, "component": component,
            "billed_annualized_cost_increase": money(billed_increase),
            "modeled_annual_cost_increase": money(modeled), "review_above": money(review_above)}


def verdict(package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any]) -> Dict[str, Any]:
    """The realization state, detail and flags of a performance or capacity step, with the evidence they rest on."""
    role = app.get("step_role")
    limits = guard_thresholds(((package.get("cost_profile") or {}).get("applied_thresholds")))
    safe = Decimal(str(limits["queue_query_pct_safe"]))
    p95_pct = Decimal(str(limits["canary_p95_regression_pct"]))
    pre_q, post_q = _guard(observation, "pre", "queued_query_pct"), _guard(observation, "post", "queued_query_pct")
    pre_p95, post_p95 = (_guard(observation, p, "p95_execution_ms_shared_sources") for p in ("pre", "post"))
    cost = cost_change(package, app, observation)
    evidence = {"queued_query_pct": {"pre": None if pre_q is None else rc.canonical_decimal(pre_q),
                                     "post": None if post_q is None else rc.canonical_decimal(post_q)},
                "p95_execution_ms_shared_sources": {"pre": None if pre_p95 is None else rc.canonical_decimal(pre_p95),
                                                    "post": None if post_p95 is None else rc.canonical_decimal(post_p95)},
                "queue_query_pct_safe": rc.canonical_decimal(safe), "canary_p95_regression_pct": rc.canonical_decimal(p95_pct)}
    review = reoptimize = False
    if role == rc.StepRole.PERFORMANCE.value:
        if pre_q is None or post_q is None:
            state, detail = rc.RealizationState.MONITORING.value, "PERFORMANCE_UNVERIFIED"
        else:
            improved = post_q <= pre_q / 2 or post_q <= safe
            p95_ok = pre_p95 is None or post_p95 is None or post_p95 <= pre_p95 * (1 + p95_pct / 100)
            if improved and p95_ok:
                state, detail = rc.RealizationState.PERFORMANCE_VALIDATED.value, "PERFORMANCE_VALIDATED"
                increase, limit = decimal(cost["annualized_cost_increase"]), decimal(cost["review_above"])
                if increase is not None and limit is not None and increase > limit:
                    detail, review = "PERFORMANCE_VALIDATED_COST_ABOVE_MODEL", True
            else:
                state, detail, reoptimize = rc.RealizationState.MONITORING.value, "PERFORMANCE_NOT_IMPROVED", True
    else:
        # What the old cap could have added at most (the minutes at the new cap x the clusters removed): evidence only.
        evidence["old_cap_upper_bound"] = old_cap_upper_bound((observation.get("measurement") or {}).get("levels"))
        if post_q is None:
            state, detail = rc.RealizationState.MONITORING.value, "CAPACITY_UNVERIFIED"
        elif post_q <= safe or (pre_q is not None and post_q <= pre_q):
            state, detail = rc.RealizationState.CAPACITY_VALIDATED.value, "CAPACITY_VALIDATED"
        else:
            state, detail, reoptimize = rc.RealizationState.MONITORING.value, "CAPACITY_QUEUEING_REGRESSED", True
    return {"realization_state": state, "classification_detail": detail, "review_required": review,
            "reoptimization_recommended": reoptimize, "performance": evidence, "cost_change": cost}


def performance_economics(package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any]) -> Dict[str, Any]:
    """The economics record of a performance or capacity step: the verdict, the signed cost change and the observed
    delta when the observation carries costs; no successful savings, run-rate, denominator or ratio."""
    econ = observation.get("economics") or {}
    cf, actual = decimal(econ.get("counterfactual_old_cost")), decimal(econ.get("observed_new_cost"))
    delta = None if cf is None or actual is None else cf - actual
    billed = (observation.get("measurement") or {}).get("billed_change") or {}
    return {
        "available": True,
        "currency": econ.get("currency") or billed.get("currency"),
        "economic_basis": str(econ.get("economic_basis") or "ECONOMIC_COST"),
        "observed_economic_delta": money(delta),
        "successful_realized_savings": None,
        "annualized_realized_savings": None,
        "expected_same_scope_annual_savings": None,
        "denominator_source": "NOT_APPLICABLE",
        "denominator_method": None,
        "expected_range": None,
        "expected_components": None,
        "classification_basis": "PERFORMANCE_GUARDS",
        "realization_ratio": None,
        **verdict(package, app, observation),
    }
