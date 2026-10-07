"""Estimator economics: the counterfactual baseline, the realized economics of an application and its measurement.

``make_cf_id``, ``expected_annual_for_exact_step`` (the publisher's expected savings for an exactly applied step) and
``calculate_economics`` (realized savings against the counterfactual, annualization when the evidence is
representative, the expected-savings denominator and the realization classification) are moved unchanged from the
single-file Realizer. ``counterfactual_record``, ``measurement_facts`` and ``correction_facts`` are its validation and
measurement statements that build the counterfactual, measurement and billing-correction payloads, in its order; the
engine records them.

The realization is measured once per active application episode: a candidate-scope application is the closest thing to
a step realization, a scenario-scope application to a plan realization, and the snapshot's latest measurement to a
package realization. Separate step, plan and package realization levels do not exist in the single-file Realizer and
are not added here.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Mapping, Optional, Tuple

from realizer.analyzer.analysis import candidate_by_id, candidate_steps, config_hash, package_config, scenario_by_id
from realizer.common.utils import decimal, money, ratio_string
from realizer.config.settings import MODELER_GATEWAY_VERSION
from realizer.contracts import realization as rc
from realizer.contracts.realization import CorrectionFacts, CounterfactualRecord, MeasurementFacts, make_measurement_id
from realizer.governor.policy import RealizerPolicy, authority_eligible, readiness


RUN_RATE_STATES = frozenset({rc.RealizationState.REALIZED.value, rc.RealizationState.UNDER_REALIZED.value, rc.RealizationState.MONITORING.value})


def make_cf_id(app_id: str, observation: Mapping[str, Any], package: Mapping[str, Any], policy: RealizerPolicy) -> str:
    return rc.content_id("CF", {
        "app": app_id,
        "old_config_hash": config_hash(package_config(package)),
        "post_change_workload": observation.get("workload", {}),
        "window": [observation.get("window_start_utc"), observation.get("window_end_utc") or observation.get("observed_at_utc")],
        "modeler_version": MODELER_GATEWAY_VERSION,
        "policy_snapshot_id": policy.snapshot_id,
    })


def expected_annual_for_exact_step(package: Mapping[str, Any], step: Mapping[str, Any]) -> Tuple[Optional[Decimal], str]:
    c = candidate_by_id(package, step.get("candidate_id"))
    if not c:
        return None, "UNAVAILABLE"
    econ = c.get("independent_economics") or {}
    savings = econ.get("savings_365d") or {}
    total = savings.get("total")
    dbu = savings.get("dbu")
    value = (total or dbu or {}).get("base") if isinstance(total or dbu, Mapping) else None
    return decimal(value), "PUBLISHER_CANDIDATE" if value is not None else "UNAVAILABLE"


def calculate_economics(
    package: Mapping[str, Any], app: Mapping[str, Any], observation: Mapping[str, Any],
    policy: RealizerPolicy, validation_passed: bool, counterfactual_claim_eligible: bool = True,
) -> Dict[str, Any]:
    econ = observation.get("economics") or {}
    cf = decimal(econ.get("counterfactual_old_cost"))
    actual = decimal(econ.get("observed_new_cost"))
    if cf is None or actual is None:
        return {
            "available": False,
            "currency": econ.get("currency"),
            "observed_economic_delta": None,
            "successful_realized_savings": None,
            "annualized_realized_savings": None,
            "expected_same_scope_annual_savings": None,
            "denominator_source": "UNAVAILABLE",
            "realization_ratio": None,
            "classification_detail": "ECONOMICS_UNAVAILABLE",
        }
    realized = cf - actual
    period_days = Decimal(int(econ["period_days"]))
    representative = readiness(observation, policy)["ready"]
    annualized = realized * Decimal("365") / period_days if representative and period_days > 0 else None

    conformance = app.get("implementation_conformance")
    expected: Optional[Decimal] = None
    denom_source = "UNAVAILABLE"
    supplied = decimal(econ.get("expected_annual_savings_actual_scope"))
    if app.get("application_scope") == "SCENARIO":
        if supplied is not None:
            expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
        elif conformance == rc.ImplementationConformance.EXACT.value:
            scn = scenario_by_id(package, app.get("scenario_id"))
            total = (((scn or {}).get("portfolio_economics") or {}).get("savings_365d") or {}).get("total") or {}
            expected = decimal(total.get("base"))
            if expected is not None:
                denom_source = "PUBLISHER_SCENARIO"
    elif conformance == rc.ImplementationConformance.MODIFIED.value:
        if supplied is not None:
            expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
    elif conformance == rc.ImplementationConformance.EXACT.value:
        if supplied is not None:
            expected, denom_source = supplied, "REESTIMATED_APPLIED_SCOPE"
        else:
            step = next((st for st in candidate_steps(package, app.get("scenario_id")) if int(st.get("step", 0)) == int(app.get("step", -1))), None)
            if step is not None and authority_eligible(step):
                expected, denom_source = expected_annual_for_exact_step(package, step)

    ratio: Optional[Decimal] = None
    if annualized is not None and expected is not None and expected > 0:
        ratio = annualized / expected

    if not validation_passed:
        state, detail = rc.RealizationState.VALIDATION_FAILED.value, "HARD_GUARDRAIL_FAILED"
        successful = Decimal("0")
    elif not counterfactual_claim_eligible:
        # Retain source actual/counterfactual diagnostics but do not convert an
        # indefensible/correlated-only counterfactual into successful realized
        # value. This is the fail-closed REAL-INV-009 path.
        state, detail = rc.RealizationState.BLOCKED.value, "COUNTERFACTUAL_NOT_CLAIM_ELIGIBLE"
        successful = Decimal("0")
    elif ratio is None:
        state, detail = rc.RealizationState.MONITORING.value, "VALIDATED_VALUE_UNCLASSIFIED"
        successful = realized
    elif ratio >= policy.realized_ratio_min:
        state, detail = rc.RealizationState.REALIZED.value, "REALIZED"
        successful = realized
    else:
        state = rc.RealizationState.UNDER_REALIZED.value
        detail = "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED" if ratio < policy.under_realized_ratio_min else "UNDER_REALIZED"
        successful = realized

    if state not in RUN_RATE_STATES:
        # A failed or blocked measurement keeps its observed delta but publishes no run-rate and no ratio.
        annualized, ratio = None, None
    return {
        "available": True,
        "currency": str(econ["currency"]),
        "economic_basis": str(econ.get("economic_basis") or "ECONOMIC_COST"),
        "counterfactual_old_cost": money(cf),
        "observed_new_cost": money(actual),
        "realized_savings_period": money(realized),
        "realized_savings_since_application": money(econ.get("realized_savings_since_application") if econ.get("realized_savings_since_application") is not None else realized),
        "observed_economic_delta": money(realized),
        "successful_realized_savings": money(successful),
        "annualized_realized_savings": money(annualized),
        "mature_ttm_realized_savings": money(econ.get("mature_ttm_realized_savings")),
        "expected_same_scope_annual_savings": money(expected),
        "denominator_source": denom_source,
        "realization_ratio": ratio_string(ratio),
        "realization_state": state,
        "classification_detail": detail,
        "reoptimization_recommended": detail == "UNDER_REALIZED_REOPTIMIZATION_RECOMMENDED",
        "dbu_realized": money(econ.get("dbu_realized")),
        "aws_economic_realized": money(econ.get("aws_economic_realized")),
        "aws_cash_realized": money(econ.get("aws_cash_realized")),
        "aws_commitment_freed": money(econ.get("aws_commitment_freed")),
        "aws_evidence_type": econ.get("aws_evidence_type"),
        "period_days": rc.canonical_decimal(period_days),
    }


def counterfactual_record(
    application_episode_id: str, observation: Mapping[str, Any], package: Mapping[str, Any], policy: RealizerPolicy,
    claim_eligible: bool, method: str,
) -> CounterfactualRecord:
    """The counterfactual baseline of the measurement: its CF id, and the COUNTERFACTUAL_RECORDED payload when the
    observation carries the counterfactual (old-configuration) cost."""
    econ_obs = observation.get("economics") or {}
    if econ_obs.get("counterfactual_old_cost") is not None:
        cf_id = make_cf_id(application_episode_id, observation, package, policy)
        cf_payload = {
            "counterfactual_id": cf_id,
            "method": method,
            "claim_eligible": claim_eligible,
            "workload_basis": "ACTUAL_POST_CHANGE_WORKLOAD",
            "counterfactual_old_cost": money(econ_obs.get("counterfactual_old_cost")),
            "modeler_version": MODELER_GATEWAY_VERSION,
        }
        return CounterfactualRecord(counterfactual_id=cf_id, method=method, claim_eligible=claim_eligible, payload=cf_payload)
    cf_id = make_cf_id(application_episode_id, observation, package, policy)
    return CounterfactualRecord(counterfactual_id=cf_id, method=method, claim_eligible=claim_eligible, payload=None)


def measurement_facts(
    application_episode_id: str, observation: Mapping[str, Any], counterfactual: CounterfactualRecord,
    economic: Mapping[str, Any], attribution: Mapping[str, Any], *, effective: str, observed_at: str,
) -> MeasurementFacts:
    """The measurement of available economics: the MEAS id and the measurement payload fields (window, counterfactual,
    economics, attribution, realization state, mature trailing-twelve-month savings, correction status)."""
    basis = str(economic.get("economic_basis") or "ECONOMIC_COST")
    meas_id = make_measurement_id(application_episode_id, observation, counterfactual.counterfactual_id, basis)
    return MeasurementFacts(
        measurement_id=meas_id,
        window_start_utc=observation.get("window_start_utc") or effective,
        window_end_utc=observation.get("window_end_utc") or observed_at,
        counterfactual_id=counterfactual.counterfactual_id,
        counterfactual_method=counterfactual.method,
        economics=economic,
        attribution=attribution,
        realization_state=economic.get("realization_state"),
        mature_ttm_realized_savings=economic.get("mature_ttm_realized_savings"),
        correction_status=(observation.get("correction") or {}).get("type", "ORIGINAL"),
    )


def correction_facts(
    observation: Mapping[str, Any], measurement_id: str, prior_measurement: Optional[rc.RealizationLedgerEntry],
) -> Optional[CorrectionFacts]:
    """The billing correction the observation carries (``None`` without one): the BILLING_CORRECTION_RECORDED payload
    and the entry it corrects, the observation's reference or else the previous run's latest measurement."""
    corr = observation.get("correction") or {}
    if not corr:
        return None
    corrects_entry_id = corr.get("corrects_entry_id") or (prior_measurement.ledger_entry_id if prior_measurement else None)
    correction_payload = {
        "correction_key": corr.get("key") or measurement_id,
        "type": str(corr.get("type") or "RESTATEMENT").upper(),
        "version": corr.get("version"),
        "reason": corr.get("reason"),
        "measurement_id": measurement_id,
        "corrects_entry_id": corrects_entry_id,
    }
    return CorrectionFacts(payload=correction_payload, corrects_entry_id=corrects_entry_id)
