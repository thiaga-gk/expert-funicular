"""Candidate estimator: each decision priced as an isolated, non-additive counterfactual with its confidence."""
from __future__ import annotations

import dataclasses
from typing import Any, Dict, List, Optional, Sequence

from publisher.analyzer.blockers import DBU_BASELINE_WITHHELD
from publisher.analyzer.epochs import epoch_age_days
from publisher.analyzer.metrics import cost_stability
from publisher.common.ids import recommendation_id
from publisher.common.utils import as_serializable, clamp, round_or_none, safe_float, safe_int
from publisher.governor.thresholds import Thresholds
from publisher.config.versions import ANALYZER_VERSION, OPTIMIZER_VERSION, POLICY_VERSION
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import CandidateRecommendation, OptimizationDecision, Range3
from publisher.contracts.source import EpochEvidence, WarehouseEvidence
from publisher.optimizer.estimator.cost import CostModel
from publisher.optimizer.estimator.savings import SavingsModel


class Estimator(CostModel, SavingsModel):
    """Price independent candidate counterfactuals and attach deterministic confidence."""
    def __init__(self, thresholds: Thresholds):
        self.t=thresholds

    # The sample each knob's decision is made on and its full-credit size; an unknown sample earns no credit.
    _DECISION_SAMPLE={"warehouse_size":("decision_queries","strong_query_sample_size"),"max_clusters":("busy_minutes_recent","confidence_strong_busy_minutes"),
                      "min_clusters":("busy_periods_recent","confidence_strong_busy_periods"),"auto_stop_minutes":("running_idle_queries","strong_query_sample_size"),
                      "warehouse_type":("queries_30","strong_query_sample_size")}

    def _confidence(self,e:WarehouseEvidence,a:AnalysisResult,signal:float=1.0,cap:float=1.0)->Dict[str,Any]:
        fact,strong=self._DECISION_SAMPLE[a.knob]
        coverage=clamp(e.cost_history_days/self.t.confidence_coverage_days,0,1); sample=clamp(safe_float(a.facts.get(fact))/getattr(self.t,strong),0,1)
        stability=cost_stability(e.cost_cv_84); epoch=clamp(epoch_age_days(e.current_epoch, e.analysis_time)/self.t.confidence_epoch_maturity_days,0,1)
        score=clamp(
            self.t.confidence_base_weight
            + self.t.confidence_coverage_weight*coverage
            + self.t.confidence_sample_weight*sample
            + self.t.confidence_stability_weight*(stability or 0.0)  # unknown stability earns no credit
            + self.t.confidence_epoch_weight*epoch
            + self.t.confidence_signal_weight*clamp(signal,0,1),
            0,cap)
        return {"score":round(score,3),"level":"HIGH" if score>=self.t.confidence_high_threshold else "MEDIUM" if score>=self.t.confidence_medium_threshold else "LOW",
                "components":{"coverage":round(coverage,3),"sample":round(sample,3),"stability":None if stability is None else round(stability,3),"epoch_maturity":round(epoch,3),"signal":round(signal,3)}}

    def candidates(self,e:WarehouseEvidence,analyses:Dict[str,AnalysisResult],decisions:Sequence[OptimizationDecision])->List[CandidateRecommendation]:
        out=[]; ep=e.current_epoch
        dbu_baseline=round_or_none(e.baseline_forecast_cost_365,2); aws_baseline=None if e.aws_baseline_cost_365 is None else round(e.aws_baseline_cost_365,2)
        total_baseline=None if aws_baseline is None or dbu_baseline is None else round(dbu_baseline+aws_baseline,2)
        for d in decisions:
            a=analyses[d.knob]
            out.append(self._candidate(e,ep,a,d,dbu_baseline,aws_baseline,total_baseline))
            gate=self._value_gate(d,out[-1])
            if gate:
                # A change made to save money ships only when its own modeled savings clear the floor; the modeled
                # economics stay on the held candidate so the reason is visible.
                held=dataclasses.replace(d,status=gate.get("status","NO_CHANGE"),recommended_config=dict(d.current_config),blockers=sorted(set(d.blockers)|{gate["reason_code"]}),
                    invalidates=[],reanalysis_required=[],rationale=gate["rationale"])
                out[-1]=self._candidate(e,ep,a,held,dbu_baseline,aws_baseline,total_baseline)
                out[-1].metadata["value_gate"]=gate
        return out

    def _value_gate(self,d:OptimizationDecision,c:"CandidateRecommendation")->Optional[Dict[str,Any]]:
        if d.knob=="warehouse_type" and d.status in {"ACTIONABLE","TEST_REQUIRED"} and d.current_config!=d.recommended_config:
            # A migration whose best case still costs more is not worth a canary (it may still be wanted for other
            # reasons, which is a human decision outside this cost model).
            # Total only: DBU alone is not comparable across types (Serverless DBU includes compute that Pro/Classic
            # pays as AWS), and unknown AWS cost is not a loss.
            r=c.total_365d_savings_range
            if r is None or r.high>0:
                return None
            return {"reason_code":"MIGRATION_NOT_COST_EFFECTIVE","modeled_savings_365":c.estimated_savings_365,"modeled_savings_range_365":as_serializable(r),
                    "floor_365":0.0,"held_change":as_serializable(d.recommended_config),"original_status":d.status,
                    "rationale":f"Held: even the best case of the {d.recommended_config.get('warehouse_type')} migration costs more than today."}
        if d.objective!="COST" or d.status not in {"ACTIONABLE","TEST_REQUIRED"} or d.current_config==d.recommended_config:
            return None
        save=c.estimated_savings_365
        if save is None and d.status=="ACTIONABLE" and c.metadata.get("estimate_blocker"):
            # A cost change applied without a canary has nothing else to validate it: it waits for its own price.
            blocker=c.metadata["estimate_blocker"]
            return {"reason_code":blocker,"status":"INSUFFICIENT_EVIDENCE","modeled_savings_365":None,"modeled_savings_range_365":None,
                    "floor_365":self.t.cost_change_min_annual_savings,"held_change":as_serializable(d.recommended_config),"original_status":d.status,
                    "rationale":f"Held: the {d.knob} change is applied without a canary, so it waits until its own savings can be priced. {DBU_BASELINE_WITHHELD[blocker][1]}"}
        if save is None:
            return None  # unpriced (withheld) changes are canaries; there is nothing to gate on
        floor=self.t.cost_change_min_annual_savings
        if save<=0:
            code,why="COST_CHANGE_NOT_COST_EFFECTIVE","its own modeled savings are not positive"
        elif save<floor:
            code,why="COST_SAVINGS_BELOW_MATERIALITY",f"its own modeled savings are below the {floor:,.0f} a year materiality floor"
        else:
            return None
        return {"reason_code":code,"modeled_savings_365":save,"modeled_savings_range_365":as_serializable(c.estimated_savings_range_365),
                "floor_365":floor,"held_change":as_serializable(d.recommended_config),"original_status":d.status,
                "rationale":f"Held: the {d.knob} change was proposed to save money but {why}."}

    def _candidate(self,e:WarehouseEvidence,ep:EpochEvidence,a:AnalysisResult,d:OptimizationDecision,dbu_baseline:Optional[float],aws_baseline:Optional[float],total_baseline:Optional[float])->"CandidateRecommendation":
        factors,meta=self._factors(e,d,a)
        dbu_costs=dbu_savings=None; aws_costs=aws_savings=None; total_costs=total_savings=None
        est=save=None
        if d.knob=="warehouse_type" and d.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"} and d.current_config!=d.recommended_config:
            migration=self._migration_costs(e,d,a.facts); meta={**meta,"migration_model":as_serializable(migration),"bundle_effect":{"type":"migration_cost_override"}}
            if migration.get("available"):
                dbu_costs=migration["dbu_cost_range"]; aws_costs=migration["aws_cost_range"]; total_costs=migration["total_cost_range"]
                dbu_savings=self._delta_range(dbu_baseline,dbu_costs)
                if aws_baseline is not None:
                    aws_savings=self._delta_range(aws_baseline,aws_costs)
                if total_baseline is not None:
                    total_savings=self._delta_range(total_baseline,total_costs); est=total_costs.base; save=total_savings.base
            else:
                meta["estimate_blocker"]=migration.get("blocker")
        elif factors is not None and dbu_baseline is not None:
            dbu_costs=Range3(round(dbu_baseline*factors.low,2),round(dbu_baseline*factors.base,2),round(dbu_baseline*factors.high,2))
            dbu_savings=self._delta_range(dbu_baseline,dbu_costs)
            aws_factors=self._aws_factor_range(e,d,factors,meta)
            meta={**meta,"aws_cost_factor_range":as_serializable(aws_factors) if aws_factors else None}
            if aws_baseline is not None and aws_factors is not None:
                aws_costs=Range3(round(aws_baseline*aws_factors.low,2),round(aws_baseline*aws_factors.base,2),round(aws_baseline*aws_factors.high,2))
                aws_savings=self._delta_range(aws_baseline,aws_costs)
                total_costs=Range3(round(dbu_costs.low+aws_costs.low,2),round(dbu_costs.base+aws_costs.base,2),round(dbu_costs.high+aws_costs.high,2))
                if total_baseline is not None: total_savings=self._delta_range(total_baseline,total_costs)
            elif e.current_epoch.config.warehouse_type=="SERVERLESS":
                aws_costs=Range3(0,0,0); aws_savings=Range3(0,0,0); total_costs=dbu_costs; total_savings=dbu_savings
            est=(total_costs.base if total_costs else dbu_costs.base); save=(total_savings.base if total_savings else dbu_savings.base)
        signal=self.t.confidence_signal_change if d.status in {"ACTIONABLE","TEST_REQUIRED"} else self.t.confidence_signal_no_change
        cap=(self.t.confidence_cap_test_required if d.status=="TEST_REQUIRED" else self.t.confidence_cap_actionable if d.status=="ACTIONABLE"
             else self.t.confidence_cap_insufficient_evidence if d.status=="INSUFFICIENT_EVIDENCE" else self.t.confidence_cap_default)
        if (d.knob=="max_clusters" and e.current_epoch.config.warehouse_type=="SERVERLESS"
                and safe_int(d.recommended_config.get("max_clusters"))>safe_int(d.current_config.get("max_clusters"))):
            cap=min(cap,self.t.confidence_cap_serverless_ceiling_increase)
        confidence=self._confidence(e,a,signal=signal,cap=cap)
        current=d.current_config; recommended=d.recommended_config
        rid=recommendation_id(e.workspace_id,e.warehouse_id,d.knob,ep.epoch_id,current,recommended)
        return CandidateRecommendation(
            recommendation_id=rid,warehouse_id=e.warehouse_id,workspace_id=e.workspace_id,warehouse_name=e.warehouse_name,knob=d.knob,objective=d.objective,status=d.status,
            current_config=current,recommended_config=recommended,current_cost_365=round_or_none(e.current_cost_365,2),
            baseline_forecast_cost_365=dbu_baseline,estimated_cost_365=est,
            estimated_cost_range_365=total_costs or dbu_costs,estimated_savings_365=save,estimated_savings_range_365=total_savings or dbu_savings,
            confidence=confidence,findings=a.findings,blockers=d.blockers,dependencies=d.dependencies,invalidates=d.invalidates,
            reanalysis_required=d.reanalysis_required,metadata={
                **meta,"cost_factor_range":as_serializable(factors) if factors else None,"estimate_is_independent_scenario":True,
                "candidate_savings_must_not_be_summed":True,"current_epoch_id":ep.epoch_id,"currency":e.currency,
                "rationale":d.rationale,"policy_version":POLICY_VERSION,"analyzer_version":ANALYZER_VERSION,"optimizer_version":OPTIMIZER_VERSION,
                "cost_scope":"DBU at published list price (observed system.billing usage x system.billing.list_prices, not contract-effective) plus modeled customer AWS EC2 at on-demand list price for Pro/Classic (Spot not modeled); Serverless cloud infrastructure is embedded in the Serverless DBU line.",
                "aws_cost_model":e.aws_cost_model,
            },
            dbu_365d_baseline_cost=dbu_baseline,aws_365d_baseline_cost=aws_baseline,total_365d_baseline_cost=total_baseline,
            dbu_365d_savings_estimate=(dbu_savings.base if dbu_savings else None),aws_365d_savings_estimate=(aws_savings.base if aws_savings else None),
            total_365d_savings_estimate=(total_savings.base if total_savings else None),dbu_365d_savings_range=dbu_savings,aws_365d_savings_range=aws_savings,
            total_365d_savings_range=total_savings,dbu_365d_estimated_cost_range=dbu_costs,aws_365d_estimated_cost_range=aws_costs,total_365d_estimated_cost_range=total_costs,
        )
