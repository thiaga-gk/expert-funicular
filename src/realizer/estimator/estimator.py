"""Estimator facade: the realized value of the active application, measured against its counterfactual baseline.

The Estimator prices the application the Reconciler found and the evidence the Governor admitted: the hard-guardrail
verdict the economics depend on, the counterfactual baseline, the realized economics with their financial-quality
gates, the attribution and the measurement, and the billing correction the observation carries. It validates nothing
itself and writes nothing; the engine records its facts.
"""
from __future__ import annotations

from typing import Any, Dict

from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts import realization as rc
from realizer.contracts.realization import RealizationEstimate
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RunContext
from realizer.estimator.attribution import attribution_result
from realizer.estimator.economics import calculate_economics, correction_facts, counterfactual_record, measurement_facts


def run(
    context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult, reconciliation: ReconciliationResult,
) -> RealizationEstimate:
    """The realized value of the active application when validation is due (an empty estimate otherwise).

    Nothing is estimated for a reconciliation that failed (its ``halt``: the Packager raises it after the events decided
    before it) or when no validation is due.

    The facts are decided in the order they are recorded, reading each policy check where it has always been read:
    the performance guardrails, then the counterfactual eligibility, then the economics, then (only for available
    economics) the financial-quality gates, the attribution and the measurement, then the billing correction.

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
        independent = sorted({str(a.payload.get("knob")) for a in reconciliation.applications
                              if a.event_type == rc.EventType.INDEPENDENT_CHANGE_OBSERVED and a.payload.get("knob") != app_payload.get("knob")})
        attr = attribution_result(app_payload, observation, validation_passed, cf_claim_eligible, economic, policy, independent)
        measurement = measurement_facts(
            app_id, observation, counterfactual, economic, attr,
            effective=observed.effective_at_utc, observed_at=observed.observed_at_utc,
        )
        facts["measurement"] = measurement
        facts["correction"] = correction_facts(observation, measurement.measurement_id, analysis.history.prior_measurement)
    except Exception as exc:  # noqa: BLE001 - kept, raised by the engine after the facts decided before it
        return RealizationEstimate(**facts, halt=exc)
    return RealizationEstimate(**facts)
