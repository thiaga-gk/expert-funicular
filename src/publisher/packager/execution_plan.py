"""Execution plan: the recommended plan text and its next action."""
from __future__ import annotations

from typing import Optional, Sequence

from publisher.contracts import reporter_contract as rc
from publisher.packager.sequencing import _canary_sequencing_note, _rollback_sequencing_note


def _recommended_plan(
    scenarios: Sequence[rc.Scenario],
    reevaluation: Optional[rc.ReevaluationWindow] = None,
    candidates: Sequence[rc.CandidateRecommendation] = (),
) -> rc.RecommendedPlan:
    """Public recommended-plan block for the RECOMMENDED scenario: selection reason, next action and summary text.

    With no recommended scenario the plan is NO_RECOMMENDED_SCENARIO. When no step is ready and a re-evaluation
    window exists, the next action is WAIT_FOR_REEVALUATION (immature epoch vs. billing history still accruing).
    Otherwise the next action follows the first step. Always requires human review (HITL); the text only describes
    the plan, it does not change any decision.
    """
    recommended = next((s for s in scenarios if s.status == rc.ScenarioStatus.RECOMMENDED), None)
    if recommended is None:
        return rc.RecommendedPlan(None, "NO_RECOMMENDED_SCENARIO", "REVIEW_EVIDENCE", True, "No scenario currently satisfies the deterministic selection policy.", reevaluation)
    first = recommended.execution_plan[0] if recommended.execution_plan else None
    ready = any(x.execution_state in (rc.ExecutionState.READY, rc.ExecutionState.READY_FOR_CANARY) for x in recommended.execution_plan)
    if reevaluation is not None and not ready:
        young = "CURRENT_CONFIG_EPOCH_TOO_NEW" in reevaluation.blocking_codes
        if young:
            reason = "CURRENT_EPOCH_IMMATURE"
            summary = (
                f"{recommended.scenario_name} has no step ready yet: the current configuration is not ready (fewer than "
                f"{rc.REEVALUATION_READY_BILLED_DAYS} settled billed days). "
                f"Earliest re-evaluation is {reevaluation.earliest_reevaluation_time} (gates: {', '.join(reevaluation.blocking_codes)})."
            )
        else:
            # Epoch is mature; only billing history is short. The earliest time can already be past: the missing days have
            # elapsed but are not yet ingested (billing typically lags ~12h), so the next run after ingestion clears it.
            reason = "BILLING_HISTORY_ACCRUING"
            summary = (
                f"{recommended.scenario_name} has no step ready yet: the current epoch is mature, but fewer than "
                f"{rc.REEVALUATION_COST_HISTORY_DAYS} days of billing history are available. Re-run once billing ingestion has caught up "
                f"(earliest {reevaluation.earliest_reevaluation_time}, gates: {', '.join(reevaluation.blocking_codes)})."
            )
        return rc.RecommendedPlan(
            scenario_id=recommended.scenario_id,
            selection_reason_code=reason,
            next_action="WAIT_FOR_REEVALUATION",
            requires_hitl=True,
            decision_summary=summary,
            reevaluation=reevaluation,
        )
    if first is None:
        next_action = "REVIEW_SCENARIO"
    elif first.step_type == rc.PlanStepType.COMPATIBILITY_VALIDATION:
        next_action = "VALIDATE_COMPATIBILITY"
    elif first.step_type == rc.PlanStepType.CANDIDATE and first.execution_state == rc.ExecutionState.READY_FOR_CANARY:
        next_action = f"RUN_{first.knob.value.upper()}_CANARY" if first.knob else "RUN_CANARY"
    else:
        next_action = "REVIEW_AND_EXECUTE_FIRST_READY_STEP"
    reason = "CURRENT_TYPE_OPTIMIZATION_HAS_VALIDATED_EVIDENCE" if recommended.scenario_type == rc.ScenarioType.IN_PLACE_OPTIMIZATION else "MIGRATION_SCENARIO_SELECTED_BY_POLICY"
    return rc.RecommendedPlan(
        scenario_id=recommended.scenario_id,
        selection_reason_code=reason,
        next_action=next_action,
        requires_hitl=True,
        decision_summary=f"{recommended.scenario_name} is the deterministic execution path; apply validation gates and regenerate after invalidating changes."
                         + _rollback_sequencing_note(recommended, candidates) + _canary_sequencing_note(recommended, candidates),
        reevaluation=reevaluation,
    )
