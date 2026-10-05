"""Reviewer candidate gate (runs after the Optimizer)."""
from __future__ import annotations

from typing import Sequence

from publisher.common.utils import safe_float, safe_int
from publisher.config.defaults import ACTIVE_KNOBS, SIZE_ORDER
from publisher.contracts.candidate import CandidateRecommendation
from publisher.contracts.source import WarehouseEvidence


class CandidateGate:
    """Candidate gate: identity, one candidate per active knob, cost, confidence, status/config consistency and knob bounds. Mixed into the Reviewer; fails fast with ValueError."""

    def review(self,e:WarehouseEvidence,candidates:Sequence[CandidateRecommendation])->None:
        seen=set(); cfg=e.current_epoch.config
        knobs=[c.knob for c in candidates]
        if set(knobs) != set(ACTIVE_KNOBS) or len(knobs) != len(ACTIVE_KNOBS):
            raise ValueError(f"Expected exactly one candidate per active knob for {e.workspace_id}/{e.warehouse_id}: {knobs}")
        for c in candidates:
            if c.workspace_id != e.workspace_id or c.warehouse_id != e.warehouse_id:
                raise ValueError(f"Candidate identity mismatch: {c.recommendation_id}")
            if c.recommendation_id in seen: raise ValueError(f"Duplicate recommendation id: {c.recommendation_id}")
            seen.add(c.recommendation_id)
            if any(x is not None and x<0 for x in (c.current_cost_365,c.baseline_forecast_cost_365)): raise ValueError(f"Negative cost: {c.recommendation_id}")
            score=safe_float(c.confidence.get("score"),-1)
            if not 0<=score<=1: raise ValueError(f"Confidence out of range: {c.recommendation_id}")
            if c.status in {"ACTIONABLE","TEST_REQUIRED","REVIEW"} and c.current_config==c.recommended_config:
                raise ValueError(f"Change status without config change: {c.recommendation_id}")
            if c.status in {"BLOCKED","INSUFFICIENT_EVIDENCE"} and not c.blockers: raise ValueError(f"{c.status} without blockers: {c.recommendation_id}")
            if c.status=="INSUFFICIENT_EVIDENCE" and c.current_config!=c.recommended_config: raise ValueError(f"INSUFFICIENT_EVIDENCE with config change: {c.recommendation_id}")
            if c.status=="NO_CHANGE" and c.estimated_savings_365 not in (None,0,0.0): raise ValueError(f"NO_CHANGE with savings: {c.recommendation_id}")
            if c.estimated_cost_range_365:
                r=c.estimated_cost_range_365
                if not (0<=r.low<=r.base<=r.high): raise ValueError(f"Invalid cost range: {c.recommendation_id}")
            if c.estimated_savings_range_365:
                r=c.estimated_savings_range_365
                if not (r.low<=r.base<=r.high): raise ValueError(f"Invalid savings range: {c.recommendation_id}")
            for f in c.findings:
                if f.epoch_id!=e.current_epoch_id and f.metric not in {"serverless_usage_pct_365"}:
                    raise ValueError(f"Decision finding not current epoch: {c.recommendation_id} {f.finding_id}")
            if c.knob=="warehouse_size":
                x=c.recommended_config.get("warehouse_size")
                if x not in SIZE_ORDER and x!=cfg.warehouse_size: raise ValueError(f"Invalid size: {x}")
            if c.knob=="min_clusters":
                x=safe_int(c.recommended_config.get("min_clusters"),cfg.min_clusters)
                if not 1 <= x <= self.t.min_clusters_supported_limit: raise ValueError("min_clusters outside supported range")
                # A rollback restores floor and ceiling together: the floor must fit under the ceiling it comes back with.
                ceiling=cfg.max_clusters
                if "CANARY_GUARDRAIL_BREACHED" in c.blockers:
                    ceiling=next((safe_int(o.recommended_config.get("max_clusters"),ceiling) for o in candidates
                                  if o.knob=="max_clusters" and "CANARY_GUARDRAIL_BREACHED" in o.blockers),ceiling)
                if x > ceiling: raise ValueError("min_clusters exceeds current max_clusters")
            if c.knob=="max_clusters":
                x=safe_int(c.recommended_config.get("max_clusters"),cfg.max_clusters)
                if not 1<=x<=self.t.max_clusters_supported_limit: raise ValueError("max_clusters outside supported range")
                if x < cfg.min_clusters: raise ValueError("max_clusters below current min_clusters")
            if c.knob=="auto_stop_minutes":
                x=safe_int(c.recommended_config.get("auto_stop_minutes"),cfg.auto_stop_minutes)
                if x < 0: raise ValueError("auto_stop_minutes < 0")
                if x != cfg.auto_stop_minutes and cfg.warehouse_type != "SERVERLESS" and x < self.t.auto_stop_floor_pro_classic:
                    raise ValueError("auto_stop_minutes below the Pro/Classic minimum")
            if str(c.metadata.get("currency"))!=e.currency: raise ValueError("Currency mismatch")
