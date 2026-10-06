"""Reviewer scenario gate (runs on the planned scenarios of each warehouse)."""
from __future__ import annotations

from typing import Optional, Sequence

from publisher.common.utils import safe_float
from publisher.contracts.candidate import CandidateRecommendation
from publisher.contracts.scenario import PRICING_AUTHORITATIVE, PRICING_CONTINGENT, Scenario
from publisher.contracts.source import WarehouseEvidence


class ScenarioGate:
    """Scenario gate: plan steps, invalidations, bundle economics and recommended-scenario uniqueness. Mixed into the Reviewer; fails fast with ValueError."""

    def review_scenarios(
        self,
        e: WarehouseEvidence,
        candidates: Sequence[CandidateRecommendation],
        scenarios: Sequence[Scenario],
        recommended_scenario_id: Optional[str],
    ) -> None:
        """Raise ValueError on the first violated invariant of one warehouse's scenarios; mutates nothing.

        Checks: exactly one recommended scenario; steps match their candidate (config, confidence); invalidated
        downstream steps are pending re-evaluation and no step precedes its invalidator (a canary rollback is a first,
        ready step); economics ranges are ordered; the pricing basis matches the execution state (pending steps are
        contingent only); authoritative and contingent savings equal the last matching step's running savings; Plan A
        never contains the migration and Plan B stays a one-step branch.
        """
        by_id={c.recommendation_id:c for c in candidates}
        ids={s.scenario_id for s in scenarios}
        if recommended_scenario_id not in ids:
            raise ValueError(f"Recommended scenario missing for {e.workspace_id}/{e.warehouse_id}: {recommended_scenario_id}")
        if sum(1 for s in scenarios if s.recommended)!=1:
            raise ValueError(f"Expected exactly one recommended scenario for {e.workspace_id}/{e.warehouse_id}")
        for sc in scenarios:
            sc_score=safe_float(sc.confidence.get("score"),-1)
            if not (0<=sc_score<=1 or (not sc.steps and sc.confidence.get("score") is None)):
                raise ValueError(f"Scenario confidence out of range: {sc.scenario_id}")
            seen_steps=set(); invalidated=set()
            for st in sc.steps:
                if st.recommendation_id not in by_id:
                    raise ValueError(f"Scenario references unknown recommendation: {st.recommendation_id}")
                if st.recommendation_id in seen_steps:
                    raise ValueError(f"Scenario duplicates recommendation: {st.recommendation_id}")
                seen_steps.add(st.recommendation_id)
                c=by_id[st.recommendation_id]
                if st.knob!=c.knob or st.current_config!=c.current_config or st.recommended_config!=c.recommended_config:
                    raise ValueError(f"Scenario step diverges from candidate contract: {st.recommendation_id}")
                step_score=safe_float(st.confidence.get("score"),-1)
                if not 0<=step_score<=1:
                    raise ValueError(f"Plan-step confidence out of range: {st.recommendation_id}")
                if abs(step_score-safe_float(c.confidence.get("score"),-2))>1e-9:
                    raise ValueError(f"Plan-step confidence does not match candidate: {st.recommendation_id}")
                if st.knob in invalidated and st.execution_state!="PENDING_REEVALUATION":
                    raise ValueError(f"Invalidated downstream step must be pending reevaluation: {st.recommendation_id}")
                invalidated.update(st.invalidates)
            # A canary rollback is a first, ready step and restores its knob regardless of what later steps invalidate.
            rollbacks=[i for i,st in enumerate(sc.steps) if "CANARY_GUARDRAIL_BREACHED" in by_id[st.recommendation_id].blockers]
            if rollbacks!=list(range(len(rollbacks))) or any(sc.steps[i].execution_state=="PENDING_REEVALUATION" for i in rollbacks):
                raise ValueError(f"Canary rollback must be a first, ready step: {sc.scenario_id}")
            for i,st in enumerate(sc.steps):
                if i not in rollbacks and any(st.knob in later.invalidates for later in sc.steps[i+1:]):
                    raise ValueError(f"Plan step precedes a step that invalidates it: {st.recommendation_id}")
            pe=sc.portfolio_estimate
            for r in (pe.estimated_cost_range_365, pe.estimated_savings_range_365, pe.dbu_365d_estimated_cost_range, pe.aws_365d_estimated_cost_range, pe.total_365d_estimated_cost_range, pe.dbu_365d_savings_range, pe.aws_365d_savings_range, pe.total_365d_savings_range):
                if r is not None and not (r.low<=r.base<=r.high):
                    raise ValueError(f"Scenario range is not ordered: {sc.scenario_id}")
            # Authoritative economics may not include steps whose evidence was invalidated upstream.
            for st in sc.steps:
                pending=st.execution_state=="PENDING_REEVALUATION"
                if pending != (st.pricing_basis==PRICING_CONTINGENT):
                    raise ValueError(f"Plan-step pricing basis must match its execution state: {st.recommendation_id}")
            authoritative=[st for st in sc.steps if st.pricing_basis==PRICING_AUTHORITATIVE]
            contingent_steps=[st for st in sc.steps if st.pricing_basis==PRICING_CONTINGENT]
            if pe.contingent is not None:
                if not contingent_steps:
                    raise ValueError(f"Contingent economics without a pending step: {sc.scenario_id}")
                for r in (pe.contingent.estimated_cost_range_365, pe.contingent.estimated_savings_range_365):
                    if r is not None and not (r.low<=r.base<=r.high):
                        raise ValueError(f"Contingent range is not ordered: {sc.scenario_id}")
                last=contingent_steps[-1].running_estimated_savings_range_365
                got=pe.contingent.estimated_savings_range_365
                if last is not None and got is not None and any(abs(a-b)>0.01 for a,b in ((last.low,got.low),(last.base,got.base),(last.high,got.high))):
                    raise ValueError(f"Contingent savings must equal the last pending step's running savings: {sc.scenario_id}")
            # A step held in the plan (BLOCKED) is not applied, so it carries no running savings: compare with the last
            # step the chain applied.
            applied=[st for st in authoritative if st.execution_state!="BLOCKED"]
            if applied and pe.estimate_status not in {"REQUIRES_CANARY","REQUIRES_COST_MODEL"}:
                last=applied[-1].running_estimated_savings_range_365
                got=pe.estimated_savings_range_365
                if last is not None and got is not None and any(abs(a-b)>0.01 for a,b in ((last.low,got.low),(last.base,got.base),(last.high,got.high))):
                    raise ValueError(f"Authoritative scenario savings must equal the last authoritative step's running savings: {sc.scenario_id}")
            if sc.name.startswith("Plan A") and any(st.knob=="warehouse_type" for st in sc.steps):
                raise ValueError(f"Current-type Plan A cannot contain warehouse_type migration: {sc.scenario_id}")
            if sc.name.startswith("Plan B") and len(sc.steps)>1:
                raise ValueError(f"Migration Plan B must remain a separate one-step branch: {sc.scenario_id}")
