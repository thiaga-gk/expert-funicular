"""Stateful bundle (scenario) estimator: prices an ordered set of steps against the state each previous step leaves."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from publisher.analyzer.blockers import DBU_BASELINE_WITHHELD, dbu_baseline_blocker
from publisher.common.utils import safe_float, safe_int
from publisher.governor.thresholds import Thresholds
from publisher.contracts.candidate import CandidateRecommendation, Range3
from publisher.contracts.scenario import PRICING_AUTHORITATIVE, PRICING_CONTINGENT, PlanStep, PortfolioEstimate
from publisher.contracts.source import WarehouseEvidence
from publisher.packager.estimator.state_transition import _ChainState


class StatefulBundleEstimator:
    """Compose ordered recommendations against evolving DBU, AWS, and resource state.

    Candidate estimates are standalone counterfactuals. Portfolio estimates are
    stateful and sequential. Min-cluster and auto-stop consume remaining
    cluster-minutes, so the same capacity minute cannot be claimed twice.
    """
    def __init__(self, thresholds: Thresholds):
        self.t = thresholds

    @staticmethod
    def _range_from_tracks(tracks: Sequence[float]) -> Range3:
        vals=sorted(max(0.0,float(x)) for x in tracks)
        return Range3(*(round(x,2) for x in vals))

    @staticmethod
    def _step_range(before: Optional[Sequence[float]], after: Optional[Sequence[float]]) -> Optional[Range3]:
        """This step's own savings on its chain, per track (cost before minus after, clamped like running costs):
        low/high = smallest/largest track change, base = the base track's change, so low <= base <= high always holds."""
        if before is None or after is None: return None
        d=[max(0.0,float(b))-max(0.0,float(a)) for b,a in zip(before,after)]
        return Range3(round(min(d),2),round(d[1],2),round(max(d),2))

    @staticmethod
    def _delta(baseline: Optional[float], cost: Optional[Range3]) -> Optional[Range3]:
        if baseline is None or cost is None: return None
        return Range3(round(baseline-cost.high,2),round(baseline-cost.base,2),round(baseline-cost.low,2))

    @staticmethod
    def _factors_from_range(baseline: Optional[float], costs: Optional[Range3]) -> Optional[List[float]]:
        if baseline is None or baseline <= 0 or costs is None: return None
        return [costs.low/baseline,costs.base/baseline,costs.high/baseline]

    def _portfolio(self,e:WarehouseEvidence,status:str,dbu_tracks:Sequence[float],aws_tracks:Optional[Sequence[float]],method:str)->PortfolioEstimate:
        dbu_cost=self._range_from_tracks(dbu_tracks)
        aws_cost=self._range_from_tracks(aws_tracks) if aws_tracks is not None else None
        total_cost=(Range3(round(dbu_cost.low+aws_cost.low,2),round(dbu_cost.base+aws_cost.base,2),round(dbu_cost.high+aws_cost.high,2)) if aws_cost else None)
        total_base=e.total_365d_baseline_cost
        dbu_save=self._delta(e.dbu_365d_baseline_cost,dbu_cost)
        aws_save=self._delta(e.aws_baseline_cost_365,aws_cost)
        total_save=self._delta(total_base,total_cost)
        legacy_base=total_base if total_base is not None else e.dbu_365d_baseline_cost
        legacy_cost=total_cost if total_cost is not None else dbu_cost
        legacy_save=total_save if total_save is not None else dbu_save
        return PortfolioEstimate(
            status,legacy_base,legacy_cost,legacy_save,method,True,
            dbu_365d_baseline_cost=e.dbu_365d_baseline_cost,
            aws_365d_baseline_cost=e.aws_baseline_cost_365,
            total_365d_baseline_cost=total_base,
            dbu_365d_estimated_cost_range=dbu_cost,
            aws_365d_estimated_cost_range=aws_cost,
            total_365d_estimated_cost_range=total_cost,
            dbu_365d_savings_range=dbu_save,
            aws_365d_savings_range=aws_save,
            total_365d_savings_range=total_save,
        )

    def _withheld(self,e:WarehouseEvidence,status:str,method:str)->PortfolioEstimate:
        """Estimate that cannot be priced (needs a canary or a cost model): every range is null, never a zero."""
        return PortfolioEstimate(
            status,e.total_365d_baseline_cost if e.total_365d_baseline_cost is not None else e.dbu_365d_baseline_cost,None,None,method,True,
            dbu_365d_baseline_cost=e.dbu_365d_baseline_cost,aws_365d_baseline_cost=e.aws_baseline_cost_365,total_365d_baseline_cost=e.total_365d_baseline_cost,
        )

    def _apply(self,st:"_ChainState",c:CandidateRecommendation,effect:Mapping[str,Any],original_resource:float,realizations:Sequence[float])->None:
        """Apply one candidate's bundle effect to a chain's evolving cost/resource state."""
        if st.halt is not None: return
        typ=effect.get("type")
        if typ=="migration_cost_override":
            if c.dbu_365d_estimated_cost_range is None:
                st.halt=("REQUIRES_COST_MODEL","Migration candidate lacks a deterministic DBU counterfactual; cost is withheld until required source pricing/resource evidence is available.")
                return
            st.dbu_tracks=[c.dbu_365d_estimated_cost_range.low,c.dbu_365d_estimated_cost_range.base,c.dbu_365d_estimated_cost_range.high]
            if c.aws_365d_estimated_cost_range is not None:
                st.aws_tracks=[c.aws_365d_estimated_cost_range.low,c.aws_365d_estimated_cost_range.base,c.aws_365d_estimated_cost_range.high]
            elif c.aws_365d_baseline_cost is None:
                st.aws_tracks=None
        elif typ=="multiplicative_cost_factor":
            df=self._factors_from_range(c.dbu_365d_baseline_cost,c.dbu_365d_estimated_cost_range)
            af=self._factors_from_range(c.aws_365d_baseline_cost,c.aws_365d_estimated_cost_range)
            if df:
                for i in range(3): st.dbu_tracks[i]*=max(0.0,df[i])
            if st.aws_tracks is not None and af:
                for i in range(3): st.aws_tracks[i]*=max(0.0,af[i])
            rr=effect.get("resource_runtime_factor_range")
            if rr and original_resource>0:
                rf=[safe_float(rr.get("low")),safe_float(rr.get("base")),safe_float(rr.get("high"))] if isinstance(rr,Mapping) else list(rr)
                for i in range(3): st.resource_tracks[i]*=max(0.0,rf[i])
        elif typ=="idle_cluster_minute_reduction":
            # Auto-stop: net cluster-minutes (idle removed minus restarts added, or restarts saved minus idle added)
            # measured at the floor the candidate saw; both terms scale with the track's current floor.
            floor0=max(1,safe_int(effect.get("floor_clusters"),1))
            unit=(safe_float(effect.get("gain_cluster_minutes"))-safe_float(effect.get("loss_cluster_minutes")))/floor0
            for i,realization in enumerate(realizations):
                before=st.resource_tracks[i]
                if before<=0 or unit<=0: continue
                # The saving at the floor the candidate saw, less its overlap with an earlier floor cut (the floor
                # clusters that cut already removed), never more than that cut removed.
                overlap=min(unit*realization*max(0.0,floor0-st.floor_tracks[i]),st.floor_cut_minutes[i])
                st.floor_cut_minutes[i]-=overlap
                removed=min(before,unit*floor0*realization-overlap)
                if removed<=0: continue
                st.auto_stop_wall_minutes[i]+=removed/max(1.0,st.floor_tracks[i])
                ratio=(before-removed)/before
                st.dbu_tracks[i]*=ratio
                if st.aws_tracks is not None: st.aws_tracks[i]*=ratio
                st.resource_tracks[i]=before-removed
        elif typ=="min_cluster_reduction":
            theoretical=max(0.0,safe_float(effect.get("cluster_minutes_to_remove")))
            target_min=max(1,safe_int(effect.get("target_min_clusters"),1))
            effect_realizations=[safe_float(x) for x in effect["realization"]] if effect.get("realization") else realizations
            for i,realization in enumerate(effect_realizations):
                before=st.resource_tracks[i]; floor=st.floor_tracks[i]
                # The track keeps the share of the floor reduction it did not realize.
                st.floor_tracks[i]=floor-max(0.0,floor-target_min)*realization
                if before<=0: continue
                claim=max(0.0,theoretical-st.auto_stop_wall_minutes[i]*max(0.0,floor-target_min))
                removed=min(before,claim*realization)
                st.floor_cut_minutes[i]+=removed
                after=max(0.0,before-removed)
                ratio=after/before
                st.dbu_tracks[i]*=ratio
                if st.aws_tracks is not None: st.aws_tracks[i]*=ratio
                st.resource_tracks[i]=after
        elif typ=="estimate_withheld":
            st.halt=(str(effect.get("status") or "UNAVAILABLE"),str(effect.get("reason") or "Estimate withheld."))
        elif typ=="requires_canary":
            st.halt=("REQUIRES_CANARY","At least one step changes the execution regime without a deterministic counterfactual; portfolio cost is withheld until canary measurement.")
        elif typ!="no_cost_change":
            raise ValueError(f"Unknown bundle effect type: {typ}")

    def estimate(self,e:WarehouseEvidence,steps:List[PlanStep],candidate_by_id:Dict[str,CandidateRecommendation])->PortfolioEstimate:
        """Price an ordered plan as two chains over the same evolving-state model.

        The *authoritative* chain prices only steps that are not PENDING_REEVALUATION: their evidence has not been
        invalidated by an upstream change. The *contingent* chain prices every step using pre-change evidence; it is
        a big-picture upside that must be regenerated after upstream remeasurement and never feeds package economics.
        """
        dbu_base=e.dbu_365d_baseline_cost
        aws_base=e.aws_baseline_cost_365
        if not steps:
            if dbu_base is None:
                return self._withheld(e,"NO_CHANGE","No scenario steps; the forward DBU cost is unavailable.")
            return self._portfolio(e,"NO_CHANGE",[dbu_base]*3,[aws_base]*3 if aws_base is not None else None,"No scenario steps.")

        original_resource=max(0.0,safe_float(e.current_epoch.events.get("cluster_minutes")))
        def fresh()->_ChainState:
            st=_ChainState([dbu_base]*3,[aws_base]*3 if aws_base is not None else None,[original_resource]*3,[float(max(1,e.current_epoch.config.min_clusters))]*3)
            if dbu_base is None:
                st.halt=DBU_BASELINE_WITHHELD[dbu_baseline_blocker(e,self.t)]
            return st
        auth,cont=fresh(),fresh()
        realizations=[self.t.realization_optimistic,self.t.realization_base,self.t.realization_conservative]

        for step in steps:
            c=candidate_by_id[step.recommendation_id]
            effect=c.metadata.get("bundle_effect") or {"type":"no_cost_change"}
            pending=step.execution_state=="PENDING_REEVALUATION"
            step.pricing_basis=PRICING_CONTINGENT if pending else PRICING_AUTHORITATIVE
            chain=cont if pending else auth
            # The step's own savings are differenced on its own chain, so capture that chain's tracks before applying.
            before_dbu,before_aws=list(chain.dbu_tracks),(list(chain.aws_tracks) if chain.aws_tracks is not None else None)
            if not pending: self._apply(auth,c,effect,original_resource,realizations)
            self._apply(cont,c,effect,original_resource,realizations)
            snap=self._portfolio(e,"RUNNING",chain.dbu_tracks,chain.aws_tracks,"Running stateful plan estimate.") if chain.halt is None else None
            step.running_dbu_cost_range_365=snap.dbu_365d_estimated_cost_range if snap else None
            step.running_aws_cost_range_365=snap.aws_365d_estimated_cost_range if snap else None
            step.running_dbu_savings_range_365=snap.dbu_365d_savings_range if snap else None
            step.running_aws_savings_range_365=snap.aws_365d_savings_range if snap else None
            step.running_estimated_cost_range_365=(snap.total_365d_estimated_cost_range or snap.dbu_365d_estimated_cost_range) if snap else None
            step.running_estimated_savings_range_365=(snap.total_365d_savings_range or snap.dbu_365d_savings_range) if snap else None
            step.step_dbu_savings_range_365=self._step_range(before_dbu,chain.dbu_tracks) if snap else None
            step.step_aws_savings_range_365=self._step_range(before_aws,chain.aws_tracks) if snap and snap.aws_365d_savings_range is not None else None

        priced=[x for x in steps if x.pricing_basis==PRICING_AUTHORITATIVE]
        has_pending=len(priced)<len(steps)
        has_migration=lambda group: any(candidate_by_id[x.recommendation_id].knob=="warehouse_type" for x in group)
        method=("Stateful DBU+AWS bundle. Each step changes the evolving cost/resource state. Min-cluster and auto-stop remove remaining cluster-minutes rather than adding standalone savings; migration steps replace the state with a DBU+AWS pre-canary counterfactual.")
        if has_pending:
            method+=" Steps invalidated by an upstream change (PENDING_REEVALUATION) are excluded from this authoritative estimate; regenerate after remeasurement."
        if auth.halt is not None:
            result=self._withheld(e,auth.halt[0],auth.halt[1])
        else:
            provisional=any(candidate_by_id[x.recommendation_id].status=="TEST_REQUIRED" for x in priced)
            status="PRE_CANARY_MODELED_RANGE" if has_migration(priced) else "PROVISIONAL_MODELED_RANGE" if provisional else "MODELED_RANGE"
            result=self._portfolio(e,status,auth.dbu_tracks,auth.aws_tracks,method)
        if has_pending and cont.halt is None:
            result.contingent=self._portfolio(
                e,"PRE_CANARY_MODELED_RANGE" if has_migration(steps) else "PROVISIONAL_MODELED_RANGE",cont.dbu_tracks,cont.aws_tracks,
                "Contingent upside: the same stateful chain priced through steps marked PENDING_REEVALUATION using pre-change evidence. "
                "Not authoritative: it never feeds package economics and must be regenerated after upstream remeasurement.")
        return result
