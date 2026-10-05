"""Scenario construction: Plan A (current type) and Plan B (migration) from the reviewed candidates, and the recommended scenario."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from publisher.common.ids import scenario_id
from publisher.common.utils import safe_float
from publisher.config.thresholds import Thresholds
from publisher.contracts.candidate import CandidateRecommendation
from publisher.contracts.scenario import PlanStep, Scenario
from publisher.contracts.source import WarehouseEvidence
from publisher.governor.compatibility import migration_compatibility
from publisher.packager.estimator.scenario import StatefulBundleEstimator
from publisher.packager.sequencing import PLAN_KNOB_RANK, PlanOrdering


class ScenarioPlanner(PlanOrdering):
    """Build ordered, dependency-aware scenarios from reviewed candidates."""
    def __init__(self,thresholds:Thresholds): self.t=thresholds; self.bundle=StatefulBundleEstimator(thresholds)

    def _scenario_confidence(self, steps: Sequence[PlanStep]) -> Dict[str, Any]:
        scores=[safe_float(s.confidence.get("score"),0.0) for s in steps if s.confidence]
        if not scores:
            return {"score":None,"level":"UNKNOWN","method":"no steps: nothing to be confident about"}
        score=min(scores)
        return {"score":round(score,3),"level":"HIGH" if score>=self.t.confidence_high_threshold else "MEDIUM" if score>=self.t.confidence_medium_threshold else "LOW","method":"bottleneck minimum of step confidences"}

    def _gate(self,c:CandidateRecommendation)->str:
        if c.knob=="warehouse_size" and c.status=="TEST_REQUIRED": return "A_B_CANARY_LATENCY_COST_ERROR"
        if c.knob=="min_clusters" and c.status=="TEST_REQUIRED": return "A_B_CANARY_QUEUE_LATENCY_COST"
        if c.knob=="warehouse_type": return "A_B_CANARY_COST_PERFORMANCE_COMPATIBILITY"
        if c.knob=="auto_stop_minutes" and c.status=="TEST_REQUIRED": return "A_B_CANARY_BILLED_COST"
        return "HITL_APPROVAL_AND_POST_CHANGE_VALIDATION"

    def _compatibility(self,e:WarehouseEvidence,target_type:str)->Dict[str,Any]:
        return migration_compatibility(e,target_type)

    def scenarios(self,e:WarehouseEvidence,candidates:Sequence[CandidateRecommendation])->Tuple[List[Scenario],Optional[str]]:
        by_knob={c.knob:c for c in candidates}; by_id={c.recommendation_id:c for c in candidates}
        steps=[]; pending_invalidated=set()
        for c in self._plan_order([by_knob[k] for k in PLAN_KNOB_RANK if by_knob[k].status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"}]):
            knob=c.knob
            pending = knob in pending_invalidated
            state = "PENDING_REEVALUATION" if pending else "READY_FOR_CANARY" if c.status=="TEST_REQUIRED" else "READY_FOR_HITL"
            gate = "RE_ANALYZE_AFTER_UPSTREAM_CANARY" if pending else self._gate(c)
            instruction = "Re-evaluate this knob using fresh post-change evidence before applying." if pending else ("Apply as canary, validate, then re-measure before downstream changes." if c.status=="TEST_REQUIRED" else "HITL approve and validate post-change evidence.")
            steps.append(PlanStep(len(steps)+1,c.recommendation_id,c.knob,c.current_config,c.recommended_config,c.status,gate,self.t.remeasure_days_after_invalidating_change if c.invalidates else 0,c.invalidates,c.dependencies,state,instruction,confidence=dict(c.confidence)))
            pending_invalidated.update(c.invalidates)
        plan_a_id=scenario_id(e.workspace_id,e.warehouse_id,"Plan A",[s.recommendation_id for s in steps])
        plan_a=Scenario(plan_a_id,e.warehouse_id,e.workspace_id,"Plan A — Optimize current warehouse type","Apply ordered tuning without changing warehouse type. Re-analyze downstream knobs after invalidating changes.",steps,self.bundle.estimate(e,steps,by_id),[],True,{"target_warehouse_type":e.current_epoch.config.warehouse_type,"assessment":"CURRENT_TYPE"},self._scenario_confidence(steps))

        type_c=by_knob["warehouse_type"]; bsteps=[]
        if type_c.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"}:
            bsteps=[PlanStep(1,type_c.recommendation_id,type_c.knob,type_c.current_config,type_c.recommended_config,type_c.status,self._gate(type_c),self.t.remeasure_days_after_invalidating_change,type_c.invalidates,type_c.dependencies,"READY_FOR_CANARY","Run a compatibility-gated A/B canary; successful migration invalidates current-type tuning and requires re-analysis.",confidence=dict(type_c.confidence))]
        # The type the migration was evaluated against: the recommended type, or the one a value gate held. A migration
        # that was never evaluated (too little evidence) has no target, so no checks run against the current type.
        held=(type_c.metadata.get("value_gate") or {}).get("held_change") or {}
        evaluated=type_c.recommended_config.get("warehouse_type") if bsteps else held.get("warehouse_type")
        target=str(evaluated).upper() if evaluated else None
        plan_b_id=scenario_id(e.workspace_id,e.warehouse_id,"Plan B",[s.recommendation_id for s in bsteps])
        pe=self.bundle.estimate(e,bsteps,by_id) if bsteps else self.bundle.estimate(e,[],by_id)
        if target is None:
            plan_b=Scenario(plan_b_id,e.warehouse_id,e.workspace_id,"Plan B — Migration not evaluated",
                "No warehouse-type migration was evaluated: "+(", ".join(type_c.blockers) or str(type_c.metadata.get("rationale") or "no target").rstrip("."))+". Re-run once the evidence gates pass.",
                [],pe,list(type_c.blockers),False,{"target_warehouse_type":None,"assessment":"NOT_EVALUATED","evidence_mode":"A_HITL_FALLBACK","checks":[]},self._scenario_confidence([]))
        else:
            plan_b=Scenario(plan_b_id,e.warehouse_id,e.workspace_id,f"Plan B — {target.title()} migration experiment",f"Canary {target} as a separate branch. The pre-canary cost range uses current-epoch resource bounds and target DBU/AWS pricing; successful migration invalidates current-type tuning and triggers re-analysis.",bsteps,pe,type_c.blockers if bsteps else list(type_c.blockers),False,self._compatibility(e,target),self._scenario_confidence(bsteps))
        return [plan_a,plan_b],plan_a.scenario_id
