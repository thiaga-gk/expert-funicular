"""Warehouse-type migration (Pro/Classic <-> Serverless): always a separate scenario branch."""
from __future__ import annotations

from typing import Dict, List, TYPE_CHECKING

from publisher.analyzer.blockers import MIGRATION_BASELINE_RATIONALE, dbu_baseline_blocker
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision
from publisher.contracts.source import WarehouseConfig, WarehouseEvidence

if TYPE_CHECKING:
    from publisher.optimizer.optimizer import Optimizer


def decide(self: "Optimizer", e: WarehouseEvidence, analyses: Dict[str, AnalysisResult], cfg: WarehouseConfig, young: bool,
           billed_days: int, out: List[OptimizationDecision]) -> None:
    """Append the warehouse_type migration decision to ``out`` (``self`` is the Optimizer: thresholds and shared helpers).

    Migration is a standalone alternative, never combined with the current-type knobs: it is INSUFFICIENT_EVIDENCE
    without a DBU baseline and cost history, NO_CHANGE while the warehouse ran without queries (review first), and
    otherwise a TEST_REQUIRED canary (Serverless -> PRO, Pro/Classic -> SERVERLESS) that invalidates every other
    knob. Workspace capability not confirmed from system tables is carried as a blocker. Unsupported types never
    reach here: the analyzer fails closed.
    """
    # Warehouse-type migration is always a separate scenario branch.
    a=analyses["warehouse_type"]; f=a.facts
    blockers=["A_B_COST_PERFORMANCE_VALIDATION_REQUIRED"]
    if e.baseline_forecast_cost_365 is None or e.cost_history_days < self.t.min_economics_cost_history_days:
        out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","INSUFFICIENT_EVIDENCE",
            {"warehouse_type":cfg.warehouse_type},{"warehouse_type":cfg.warehouse_type},[dbu_baseline_blocker(e,self.t)],[],[],[],
            MIGRATION_BASELINE_RATIONALE[dbu_baseline_blocker(e,self.t)]))
    elif any(d.knob=="auto_stop_minutes" and "NO_QUERIES_WHILE_RUNNING" in d.blockers for d in out):
        # A warehouse that ran with no query may not be needed at all: the review comes before any migration.
        out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","NO_CHANGE",
            {"warehouse_type":cfg.warehouse_type},{"warehouse_type":cfg.warehouse_type},["NO_QUERIES_WHILE_RUNNING"],[],[],[],
            "The warehouse ran with no queries; no migration experiment is proposed until a review decides whether it is still needed."))
    elif cfg.warehouse_type == "SERVERLESS":
        if not f.get("pro_classic_observed"):
            blockers.append("PRO_CLASSIC_WORKSPACE_CAPABILITY_NOT_CONFIRMED_FROM_SYSTEM_TABLES")
        out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","COST_AND_COMPATIBILITY","TEST_REQUIRED",
            {"warehouse_type":cfg.warehouse_type},{"warehouse_type":"PRO"},blockers,[],
            ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
            ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
            "Pro is evaluated as an alternate migration experiment for Serverless warehouses. The estimate is pre-canary and compatibility remains a deterministic gate."))
    else:
        if not f.get("serverless_observed"):
            blockers.append("SERVERLESS_WORKSPACE_CAPABILITY_NOT_CONFIRMED_FROM_SYSTEM_TABLES")
        out.append(OptimizationDecision(e.warehouse_id,"warehouse_type","PERFORMANCE_AND_OPERATIONS","TEST_REQUIRED",
            {"warehouse_type":cfg.warehouse_type},{"warehouse_type":"SERVERLESS"},blockers,[],
            ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
            ["warehouse_size","max_clusters","min_clusters","auto_stop_minutes"],
            "Serverless is evaluated as a separate migration experiment with a pre-canary DBU/AWS counterfactual and deterministic compatibility gate."))
