"""Estimator attribution: how much of the realized value the recommendation may claim, and the portfolio arithmetic
that keeps savings from being counted twice.

``attribution_result`` applies the hard disqualifiers (financial-quality gates, unmatched applied state, failed
validation, no defensible counterfactual, portfolio scope overlap, an origin that is not the recommendation) and the
confirmed-origin rule; ``authoritative_cumulative_realization`` measures a cumulative realization against the original
counterfactual and reports the sum of incremental realizations as a diagnostic only. Both are moved unchanged from the
single-file Realizer, with the attribution rules kept inline where they are applied. The cumulative helper is not on
the realize path.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Sequence

from realizer.common.utils import decimal, money
from realizer.contracts import realization as rc
from realizer.governor.policy import RealizerPolicy, canary_window_open


def attribution_result(app: Mapping[str, Any], observation: Mapping[str, Any], validation_passed: bool, cf_claim_eligible: bool, economic: Mapping[str, Any], policy: RealizerPolicy,
                       independent_knobs: Sequence[str] = ()) -> Dict[str, Any]:
    try:
        origin = rc.OriginAttribution(str(observation.get("origin_attribution") or "UNKNOWN").upper())
    except ValueError:
        origin = rc.OriginAttribution.UNKNOWN
    overlap = int(observation["overlap_count"])
    eligible_conf = app.get("implementation_conformance") in {rc.ImplementationConformance.EXACT.value, rc.ImplementationConformance.MODIFIED.value}
    hard: List[str] = []
    quality = economic["quality_gates"]
    if not quality.get("attributable_spend", {}).get("pass"):
        hard.append("MATERIAL_SPEND_UNRECONCILED")
    if not quality.get("price_coverage", {}).get("pass"):
        hard.append("PRICE_COVERAGE_INCOMPLETE")
    if not quality.get("currency_consistency", {}).get("pass"):
        hard.append("CURRENCY_MISMATCH")
    if not quality.get("correction_chain", {}).get("pass"):
        hard.append("CORRECTION_CHAIN_INCOMPLETE")
    if not quality.get("material_cost_components", {}).get("pass"):
        hard.append("MATERIAL_COST_COMPONENT_UNAVAILABLE")
    if not eligible_conf:
        hard.append("APPLIED_STATE_UNMATCHED")
    if not validation_passed:
        hard.append("HARD_VALIDATION_FAILURE")
    if not cf_claim_eligible:
        hard.append("NO_DEFENSIBLE_COUNTERFACTUAL")
    if overlap > 0:
        hard.append("PORTFOLIO_SCOPE_OVERLAP")
    if independent_knobs and not (observation.get("measurement") or {}).get("holds_other_knobs"):
        # Another knob changed too and the counterfactual cannot hold it at its new value: the saving may be its.
        hard.append("INDEPENDENT_CHANGE_NOT_HELD")
    if canary_window_open(app, observation):
        # A canary-type change is guarded by the Publisher for its first 30 days: its realization stays provisional.
        hard.append("CANARY_GUARD_WINDOW_OPEN")
    if (((observation.get("measurement") or {}).get("cross_check") or {}).get("outcome")) == "DISAGREES":
        # The billed cost before and after the change does not confirm the replay: a person reviews it first.
        hard.append("BILLED_CROSS_CHECK_DISAGREES")
    if origin in {rc.OriginAttribution.INDEPENDENT, rc.OriginAttribution.NOT_ATTRIBUTABLE}:
        hard.append("ORIGIN_NOT_ATTRIBUTABLE")
    if app.get("step_role") in (rc.StepRole.PERFORMANCE.value, rc.StepRole.CAPACITY.value):
        # A performance fix or a capacity guardrail is not a saving: its outcome is observable, never claimed.
        hard.append("NOT_A_SAVINGS_STEP")
    # The strict KPI needs a qualified origin: confirmed by an approval, or inferred from a change applied after the
    # package to the knob it recommended (a live measurement's origin).
    confirmed_ok = origin in {rc.OriginAttribution.CONFIRMED, rc.OriginAttribution.INFERRED} or not policy.require_confirmed_origin_for_hard_kpi
    claimable = not hard and confirmed_ok and economic.get("available") and quality.get("pass")
    if claimable:
        claim = rc.AttributionClaimStatus.CLAIMABLE
        state = rc.AttributionState.HIGH_CONFIDENCE
    elif economic.get("available") and validation_passed and cf_claim_eligible and origin not in {rc.OriginAttribution.INDEPENDENT, rc.OriginAttribution.NOT_ATTRIBUTABLE}:
        claim = rc.AttributionClaimStatus.OBSERVABLE_ONLY
        state = rc.AttributionState.PARTIAL_ATTRIBUTION if origin == rc.OriginAttribution.INFERRED else rc.AttributionState.CORRELATED_ONLY
    else:
        claim = rc.AttributionClaimStatus.NOT_ATTRIBUTABLE
        state = rc.AttributionState.NOT_ATTRIBUTABLE
    successful = decimal(economic.get("successful_realized_savings")) or Decimal("0")
    return {
        "origin_attribution": origin.value,
        "attribution_scope": str(app.get("application_scope") or observation.get("attribution_scope") or "CANDIDATE"),
        "attribution_state": state.value,
        "attribution_claim_status": claim.value,
        "hard_disqualifiers": hard,
        "overlap_count": overlap,
        "economic_quality_gates": quality,
        "validated_observed_realization": economic.get("successful_realized_savings"),
        "recommendation_attributed_realized_savings": money(successful if claimable else Decimal("0")),
    }


def authoritative_cumulative_realization(
    counterfactual_original_cost: Any, actual_final_cost: Any, incremental_realizations: Sequence[Any] = (),
) -> Dict[str, Any]:
    cf, actual = decimal(counterfactual_original_cost), decimal(actual_final_cost)
    if cf is None or actual is None:
        return {"authoritative": None, "diagnostic_incremental_sum": None, "interaction_delta": None}
    authoritative = cf - actual
    diagnostic = sum((decimal(v) or Decimal("0") for v in incremental_realizations), Decimal("0"))
    return {
        "authoritative": money(authoritative),
        "diagnostic_incremental_sum": money(diagnostic),
        "interaction_delta": money(authoritative - diagnostic),
    }
