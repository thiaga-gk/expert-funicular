"""Sequencing: plan-step order, and the rollback and canary sequencing notes of the recommended plan."""
from __future__ import annotations

from typing import List, Sequence, Tuple

from publisher.common.utils import safe_float
from publisher.contracts import reporter_contract as rc
from publisher.contracts.candidate import CandidateRecommendation


class PlanOrdering:
    """Plan-step ordering for the scenario planner (mixin)."""

    def _plan_order(self,eligible:Sequence[CandidateRecommendation])->List[CandidateRecommendation]:
        """Order plan steps by value, then place each step only after every step that invalidates it.

        Value = base savings x confidence / risk (a canary step carries more risk than a HITL-approved one). Priced
        positive steps come first, then unpriced steps (a canary prices them), then zero/negative ones (guardrails and
        performance fixes); ties follow the knob rank. Rounding to cents keeps the order platform-independent.
        """
        def key(c:CandidateRecommendation)->Tuple[int,float,int]:
            base=c.estimated_savings_365
            risk=self.t.plan_risk_canary_change if c.status=="TEST_REQUIRED" else self.t.plan_risk_hitl_change
            group=-1 if "CANARY_GUARDRAIL_BREACHED" in c.blockers else 1 if base is None else 0 if base>0 else 2
            value=0.0 if base is None else round(base*safe_float(c.confidence.get("score"),0.0)/risk,2)
            return (group,-value,PLAN_KNOB_RANK[c.knob])
        # A canary rollback restores the previous configuration: it is first and never waits on another step, whatever
        # the kept steps invalidate (a rollback restores its knob regardless).
        rollbacks=[c for c in sorted(eligible,key=key) if "CANARY_GUARDRAIL_BREACHED" in c.blockers]
        remaining=[c for c in sorted(eligible,key=key) if c not in rollbacks]; ordered:List[CandidateRecommendation]=[]
        while remaining:
            nxt=next((c for c in remaining if not any(c.knob in x.invalidates for x in remaining if x is not c)),None)
            if nxt is None:
                raise ValueError("Plan steps invalidate each other in a cycle: "+", ".join(c.knob for c in remaining))
            ordered.append(nxt); remaining.remove(nxt)
        # A step another plan step invalidates is always pending re-evaluation (its invalidator precedes it), so ready
        # steps come first: what can be applied now is listed before what waits, and the pending tail prices the
        # contingent upside on its own.
        invalidated={k for c in rollbacks+ordered for k in c.invalidates}
        return rollbacks+[c for c in ordered if c.knob not in invalidated]+[c for c in ordered if c.knob in invalidated]


PLAN_KNOB_RANK = {"warehouse_size": 0, "max_clusters": 1, "min_clusters": 2, "auto_stop_minutes": 3}  # plan-order tie-break


def _rollback_knobs(scenario: rc.Scenario, candidates: Sequence[rc.CandidateRecommendation]) -> List[str]:
    """Knobs of the scenario's canary rollback steps, in plan order."""
    rollback_ids = {c.candidate_id for c in candidates if "CANARY_GUARDRAIL_BREACHED" in c.blockers}
    return [x.knob.value for x in scenario.execution_plan
            if x.step_type == rc.PlanStepType.CANDIDATE and x.knob is not None and x.candidate_id in rollback_ids]


def _rollback_sequencing_note(scenario: rc.Scenario, candidates: Sequence[rc.CandidateRecommendation]) -> str:
    """A canary rollback goes before every other step: a change applied first starts a new configuration, and the next
    run no longer sees the change being rolled back."""
    knobs = _rollback_knobs(scenario, candidates)
    if not knobs:
        return ""
    return (f" Apply the canary rollback ({', '.join(knobs)}) before any other step: a change applied first starts a new "
            "configuration, and the next run no longer sees the change being rolled back.")


def _canary_sequencing_note(scenario: rc.Scenario, candidates: Sequence[rc.CandidateRecommendation] = ()) -> str:
    """When approval-only steps and canaries are both ready: approval-only steps go first, canaries start once those
    have run for the stability period, so a canary's cost reading is not mixed with another change's saving. A canary
    rollback has its own sentence (it goes before everything), so it is not listed again here."""
    rollbacks = _rollback_knobs(scenario, candidates)
    steps = [x for x in scenario.execution_plan if x.step_type == rc.PlanStepType.CANDIDATE and x.knob is not None]
    hitl = [x.knob.value for x in steps if x.execution_state == rc.ExecutionState.READY and x.knob.value not in rollbacks]
    canary = [x.knob.value for x in steps if x.execution_state == rc.ExecutionState.READY_FOR_CANARY]
    if not canary or not (hitl or rollbacks):
        return ""
    if not hitl:
        return (f" Start the {', '.join(canary)} canary once the next run shows the restored configuration ready "
                f"({rc.REEVALUATION_READY_BILLED_DAYS} settled billed days), so the canary's cost reading is not mixed with the rollback.")
    lead = f" Then apply the ready no-canary steps ({', '.join(hitl)});" if rollbacks else f" Apply the ready no-canary steps ({', '.join(hitl)}) first;"
    return (f"{lead} start the {', '.join(canary)} canary once the next run shows "
            f"their new configuration ready ({rc.REEVALUATION_READY_BILLED_DAYS} settled billed days), so the canary's cost reading is not "
            "mixed with their savings.")
