"""Estimator facade: the realized value of the active application, measured against its counterfactual baseline.

The Estimator prices the application the Reconciler found and the evidence the Governor admitted: the hard-guardrail
verdict the economics depend on, the counterfactual baseline, the realized economics with their financial-quality
gates, the attribution and the measurement, and its billing correction (one the observation carries, or a restatement
of a window the ledger already measured). It validates nothing itself and writes nothing; the engine records its facts.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Dict

from realizer.config.settings import UNPRICED_DECISION_KNOBS
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts import realization as rc
from realizer.contracts.realization import RealizationEstimate
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RunContext
from realizer.estimator.attribution import attribution_result
from realizer.estimator.economics import (
    calculate_economics, correction_facts, counterfactual_record, measurement_facts, repeats_earlier_measurement,
)


# The economics of a safety action: nothing is measured against a counterfactual and nothing is claimed.
SAFETY_ACTION_ECONOMICS = {
    "available": False, "currency": None, "observed_economic_delta": None, "successful_realized_savings": None,
    "annualized_realized_savings": None, "expected_same_scope_annual_savings": None, "denominator_source": "NOT_APPLICABLE",
    "realization_ratio": None, "classification_detail": "SAFETY_ACTION",
}


def run(
    context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult, reconciliation: ReconciliationResult,
) -> RealizationEstimate:
    """The realized value of the active application when validation is due (an empty estimate otherwise).

    Nothing is estimated for a reconciliation that failed (its ``halt``: the Packager raises it after the events decided
    before it) or when no validation is due.

    The facts are decided in the order they are recorded, reading each policy check where it has always been read:
    the performance guardrails, then the counterfactual eligibility, then the economics, then (only for available
    economics) the financial-quality gates, the attribution and the measurement, then the billing correction. A
    measurement that repeats an earlier measurement of its window (the same facts) is a duplicate: neither it nor a
    correction is handed over.

    ``run`` never raises. A failure is kept as ``halt`` together with the facts decided before it, so the engine records
    exactly those and then raises it, as before.
    """
    if reconciliation.halt is not None or not reconciliation.validation_due:
        return RealizationEstimate()
    observed, active = analysis.observation, reconciliation.active
    assert observed is not None and active is not None  # validation is due only for an active application
    package, policy, observation = context.request.package, context.policy, observed.raw
    app_payload, app_id = active.payload, active.application_episode_id
    facts: Dict[str, Any] = {}
    try:
        validation_passed = governance.performance.get()["hard_guardrails_pass"]
        facts["validation_passed"] = validation_passed
        if app_payload.get("step_role") == rc.StepRole.SAFETY_ROLLBACK.value:
            # A safety rollback restores a value the Publisher's guard rejected: validated, never measured or claimed.
            return RealizationEstimate(**facts, economics=SAFETY_ACTION_ECONOMICS)
        cf_claim_eligible, cf_method = governance.counterfactual.get()
        counterfactual = counterfactual_record(app_id, observation, package, policy, cf_claim_eligible, cf_method)
        facts["counterfactual"] = counterfactual
        economic = calculate_economics(
            package, app_payload, observation, policy, validation_passed,
            counterfactual_claim_eligible=cf_claim_eligible,
        )
        if not economic.get("available"):
            facts["economics"] = economic
            return RealizationEstimate(**facts)
        # The gates join the economics (``quality_gates``, last key) before attribution reads them, as they always did.
        economic = {**economic, "quality_gates": governance.economic_quality.get()}
        facts["economics"] = economic
        # A change with no priced effect (the channel) cannot be in the saving, so it never counts against the claim.
        independent = sorted({str(a.payload.get("knob")) for a in reconciliation.applications
                              if a.event_type == rc.EventType.INDEPENDENT_CHANGE_OBSERVED and a.payload.get("knob") != app_payload.get("knob")
                              and a.payload.get("knob") not in UNPRICED_DECISION_KNOBS})
        attr = attribution_result(app_payload, observation, validation_passed, cf_claim_eligible, economic, policy, independent)
        measurement = measurement_facts(
            app_id, observation, counterfactual, economic, attr,
            effective=observed.effective_at_utc, observed_at=observed.observed_at_utc,
        )
        history = analysis.history.entries
        if repeats_earlier_measurement(measurement, app_id, history):
            return RealizationEstimate(**facts)  # the window was measured before with the same facts: nothing new
        facts["measurement"] = measurement
        correction = correction_facts(observation, measurement, app_id, history)
        if correction is not None and not observation.get("correction"):
            # a window restated without a correction object: the measurement is the restatement the correction records
            facts["measurement"] = dataclasses.replace(measurement, correction_status=correction.payload["type"])
        facts["correction"] = correction
    except Exception as exc:  # noqa: BLE001 - kept, raised by the engine after the facts decided before it
        return RealizationEstimate(**facts, halt=exc)
    return RealizationEstimate(**facts)
