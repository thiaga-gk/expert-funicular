"""Auto-stop: the cost-minimizing value on the warehouse type's grid, within its guardrails."""
from __future__ import annotations

from typing import Dict, List, TYPE_CHECKING

from publisher.analyzer.blockers import failure_blockers
from publisher.common.utils import safe_float
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision
from publisher.contracts.source import WarehouseConfig, WarehouseEvidence

if TYPE_CHECKING:
    from publisher.optimizer.optimizer import Optimizer


def decide(self: "Optimizer", e: WarehouseEvidence, analyses: Dict[str, AnalysisResult], cfg: WarehouseConfig, young: bool,
           billed_days: int, out: List[OptimizationDecision]) -> None:
    """Append this knob's decision to ``out`` (``self`` is the Optimizer: thresholds and shared helpers)."""
    # Auto stop: jump to the cost-minimizing value on the type's grid.
    a=analyses["auto_stop_minutes"]; f=a.facts; cur_auto=cfg.auto_stop_minutes
    def auto_stop(status:str,target:int,blockers:List[str],rationale:str)->None:
        # Non-work activity is reported with every outcome; no auto-stop value removes it, so nothing is priced.
        if f.get("kept_alive_pct") is not None and f["kept_alive_pct"]>=self.t.kept_alive_flag_pct:
            blockers=blockers+["ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING"]
            rationale+=f" {f['kept_alive_pct']:g}% of running time came after the auto-stop should have stopped the warehouse, with no query running (open sessions, result fetching or keep-alive)."
        out.append(OptimizationDecision(e.warehouse_id,"auto_stop_minutes","COST",status,
            {"auto_stop_minutes":cur_auto},{"auto_stop_minutes":target},sorted(set(blockers)),[],[],[],rationale))
    grid=f["grid"]; options={o["target"]:o for o in f["options"]}
    if cfg.warehouse_type=="SERVERLESS" and 0<cur_auto<min(grid):
        auto_stop("NO_CHANGE",cur_auto,[],"Serverless auto-stop is below the lowest value the console offers (set through the API); it is left alone.")
    elif not f["running_available"]:
        billed=f.get("billed_recent")
        if (billed and f["queries_30"]==0 and not young
                and billed["settled_days"]>=self.t.min_economics_cost_history_days and billed["billed_days"]==billed["settled_days"]
                and e.baseline_forecast_cost_365 is not None and (cur_auto==0 or min(grid)<cur_auto)):
            # Billing shows it ran every settled day with no query, but without retained running intervals no
            # running hours or idle saving can be measured: a human decides first, and nothing is priced.
            spent="" if billed["billed_cost"] is None else f" ({billed['billed_cost']:,.0f} {e.currency})"
            auto_stop("REVIEW",min(grid),["NO_QUERIES_WHILE_RUNNING"],
                f"The warehouse was billed on {billed['billed_days']} of the last {billed['settled_days']} settled days{spent} with no query in the last 30 days; "
                f"its forward cost is {e.baseline_forecast_cost_365:,.0f} {e.currency} a year. Review whether it is still needed or what keeps it running "
                f"(open sessions, keep-alive, a connected tool). If it stays, {min(grid)} minutes is the lowest auto-stop; no saving is priced because "
                "no retained warehouse event gives its running time.")
        else:
            auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE"],
                "Idle time intersected with the warehouse's running time was not collected (older evidence or no retained warehouse event); no auto-stop change is priced.")
    elif safe_float(f.get("cluster_minutes"))<self.t.min_cluster_minutes_for_cost_model:
        auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["INSUFFICIENT_EVENT_RESOURCE_EVIDENCE"],"The warehouse recorded too little running time to price an auto-stop change.")
    elif any(o["direction"]=="LOWER" and o["gain_cluster_minutes"]>safe_float(f.get("cluster_minutes"))+0.01 for o in options.values()):
        # Running idle time is part of running time by construction; more of it than the events recorded means the
        # two collectors saw different periods or data, so nothing can be priced.
        auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["IDLE_EVIDENCE_EXCEEDS_RUNTIME"],
            "Removable idle time exceeds the running time warehouse events recorded; the evidence is inconsistent, so no auto-stop change is priced.")
    elif young:
        auto_stop("INSUFFICIENT_EVIDENCE",cur_auto,["CURRENT_CONFIG_EPOCH_TOO_NEW"],"The current configuration is not ready yet (too few settled billed days) to measure its idle gaps; auto-stop is re-evaluated once it is.")
    elif not f.get("running_idle_queries"):
        # No queries but running time: a human decides whether the warehouse is still needed or what keeps it up.
        floor=min(grid); o=options.get(floor)
        if o is not None and o["direction"]=="LOWER" and o["net_cluster_minutes"]>0:
            auto_stop("REVIEW",floor,["NO_QUERIES_WHILE_RUNNING"],
                "The warehouse ran with no queries in the window; review whether it is still needed. The lowest auto-stop value is priced on its measured idle time.")
        else:
            auto_stop("NO_CHANGE",cur_auto,["NO_QUERIES_WHILE_RUNNING"],
                "The warehouse ran with no queries in the window, and no lower auto-stop removes that running time; review whether it is still needed or what keeps it running.")
    else:
        saving=[o for o in options.values() if o["net_cluster_minutes"]>0]
        allowed=[o for o in saving if o["within_cold_start_budget"]]
        if not allowed:
            polling=(not saving and f.get("running_clock_pct") is not None and f["running_clock_pct"]>=self.t.always_on_running_pct
                     and f.get("busy_pct") is not None and f["busy_pct"]<=self.t.always_on_busy_pct_max)
            clients=", ".join(f.get("client_applications") or []) or "unknown clients"
            auto_stop("NO_CHANGE",cur_auto,(["COLD_START_BUDGET_EXCEEDED"] if saving else [])+(["SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING"] if polling else []),
                "Every value that saves money adds more cold starts than the budget allows; auto-stop stays." if saving else
                f"The warehouse runs {f['running_clock_pct']:g}% of the time while queries are busy only {f['busy_pct']:g}% of it: frequent tiny queries ({clients}) keep it up, so no auto-stop value can stop it. Review the polling, keep-alive or refresh schedule." if polling else
                "No auto-stop value saves money on the measured idle gaps; the current value is cost-optimal.")
        else:
            best=max(o["net_cluster_minutes"] for o in allowed)
            near=[o for o in allowed if o["net_cluster_minutes"]>=(1-self.t.auto_stop_near_best_pct/100.0)*best]
            # The smallest change among near-best values (an always-on warehouse changes least at the largest value).
            pick=min(near,key=lambda o:((abs(o["target"]-cur_auto) if cur_auto>0 else -o["target"]),o["target"]))
            blockers=failure_blockers(f["failure_pct"], self.t) if pick["direction"]=="LOWER" else []
            if blockers:
                auto_stop("BLOCKED",pick["target"],blockers,"A cheaper auto-stop exists but guardrails block the recommendation.")
            elif pick["direction"]=="RAISE":
                # The saving is entirely restarts avoided, priced on the assumption that starting minutes are billed.
                auto_stop("TEST_REQUIRED",pick["target"],["RESTART_BILLING_UNVERIFIED"],
                    "A higher auto-stop saves more restart cost than the idle time it adds; the restart cost is an assumption (starting minutes billed), so billed cost is measured after the change.")
            else:
                auto_stop("ACTIONABLE",pick["target"],[],
                    "The cost-minimizing auto-stop on measured running idle time, counting the restarts it adds, within the cold-start budget.")
