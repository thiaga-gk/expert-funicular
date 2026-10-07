"""Reviewer facade: is the realization outcome defensible.

The Reviewer assembles the validation episode of the active application (readiness and performance verdicts) and the
recognition status of the Estimator's measurement, and hands them over with the upstream results. The single-file
Realizer has no review gate, so the Reviewer adds no check that could reject or alter an outcome. It writes nothing; the
engine records the episode.
"""
from __future__ import annotations

from typing import Any, Dict

from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.realization import RealizationEstimate, ReviewResult, ValidationEpisode
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import canary_window_open
from realizer.reviewer.economics import recognition_status
from realizer.reviewer.validation import plan_episode


def run(
    context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult,
    reconciliation: ReconciliationResult, estimate: RealizationEstimate,
) -> ReviewResult:
    """The validation episode and the measurement's recognition status when validation is due (neither otherwise).

    Nothing is reviewed for a reconciliation that failed (its ``halt``: the Packager raises it after the events decided
    before it) or when no validation is due; the upstream results pass through.

    ``run`` never raises. A failure while building the episode (an id or a payload that cannot be computed, a
    performance verdict that cannot be reached) is kept as ``halt`` with the payloads decided before it; the engine
    records those and raises it before anything the estimate holds, because the episode is recorded first.
    """
    review: Dict[str, Any] = {"analysis": analysis, "governance": governance, "reconciliation": reconciliation, "estimate": estimate}
    if reconciliation.halt is not None or not reconciliation.validation_due:
        return ReviewResult(**review)
    fields: Dict[str, Any] = {}
    try:
        # validation is due only for an observation with an active application
        plan_episode(fields, reconciliation.active.application_episode_id, analysis.observation, governance)  # type: ignore[union-attr,arg-type]
    except Exception as exc:  # noqa: BLE001 - kept, raised by the engine after the payloads decided before it
        return ReviewResult(**review, validation=ValidationEpisode(**fields) if fields else None, halt=exc)
    recognition = None
    if estimate.measurement is not None:
        # a measurement exists only after the guardrail verdict and the counterfactual were decided
        provisional = canary_window_open(reconciliation.active.payload, analysis.observation.raw)  # type: ignore[union-attr]
        recognition = recognition_status(estimate.validation_passed, estimate.counterfactual.claim_eligible, provisional)  # type: ignore[arg-type,union-attr]
    return ReviewResult(**review, validation=ValidationEpisode(**fields), recognition_status=recognition)
