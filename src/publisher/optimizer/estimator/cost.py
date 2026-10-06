"""Candidate cost estimation: Pro/Classic size-rate validation, AWS and target-SKU price lookups, and the CostModel mixin."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Tuple

from publisher.analyzer.blockers import DBU_BASELINE_WITHHELD, dbu_baseline_blocker
from publisher.analyzer.collector.pricing import classify_sql_sku
from publisher.analyzer.metrics import annualized_resource_rates
from publisher.common.utils import as_serializable, clamp, safe_float, safe_int
from publisher.config.defaults import AWS_SQL_WAREHOUSE_TOPOLOGY, QUERIES_PER_CLUSTER_PRO_CLASSIC, SIZE_ORDER, SQL_DBU_PER_CLUSTER_HOUR
from publisher.config.versions import ESTIMATOR_VERSION
from publisher.contracts.analysis import AnalysisResult
from publisher.contracts.candidate import OptimizationDecision, Range3
from publisher.contracts.source import WarehouseEvidence
from publisher.optimizer.techniques.scaling import MAX_INCREASE_COST_BOUNDS


def aws_cluster_hourly_from_evidence(e: WarehouseEvidence, size: str) -> Tuple[Optional[float], Dict[str, Any]]:
    """Modeled on-demand EC2 USD per cluster-hour (driver + workers) for a size, plus provenance.

    Returns (None, {status: UNAVAILABLE, blocker}) when the size topology or a regional instance price is missing:
    an unknown AWS cost is unavailable, never zero. Spot, Reserved Instances and Savings Plans are not modeled.
    """
    topology = AWS_SQL_WAREHOUSE_TOPOLOGY.get(size)
    prices = e.aws_cost_model.get("region_instance_prices") if isinstance(e.aws_cost_model, Mapping) else None
    if not topology or not isinstance(prices, Mapping):
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_EVIDENCE_NOT_AVAILABLE", "warehouse_size": size, "region": e.source_region}
    driver = str(topology["driver"]); worker_type = "i3.2xlarge"; workers = int(topology["workers"])
    if driver not in prices or worker_type not in prices:
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_INSTANCE_PRICE_MISSING", "driver": driver, "worker": worker_type, "region": e.source_region}
    hourly = safe_float(prices[driver]) + workers * safe_float(prices[worker_type])
    return round(hourly, 6), {"status": "MODELED", "region": e.source_region, "warehouse_size": size, "driver": driver, "workers": workers, "worker_type": worker_type, "cluster_hourly_price": round(hourly, 6)}


def resolve_target_sku_price(sql_sku_prices: Mapping[str, Any], target_type: str) -> Tuple[Optional[float], Dict[str, Any]]:
    """Unit price per DBU of the SQL SKUs classified as ``target_type``; None with a blocker when none is found or the SKUs disagree (ambiguous prices are never averaged)."""
    target = str(target_type).upper()
    candidates: List[Tuple[str, float]] = []
    for name, payload in sql_sku_prices.items():
        if classify_sql_sku(name) != target:
            continue
        price = payload.get("effective_list_price") if isinstance(payload, Mapping) else payload
        if price is not None:
            candidates.append((name, safe_float(price)))
    distinct = sorted({round(v, 10) for _, v in candidates})
    if not candidates:
        return None, {"status": "UNAVAILABLE", "blocker": "TARGET_SQL_SKU_PRICE_NOT_FOUND", "target_type": target}
    if len(distinct) != 1:
        return None, {"status": "UNAVAILABLE", "blocker": "AMBIGUOUS_TARGET_SQL_SKU_PRICE", "target_type": target, "candidates": candidates}
    return distinct[0], {"status": "RESOLVED", "target_type": target, "unit_price_per_dbu": distinct[0], "sku_names": [n for n, _ in candidates]}


def validated_size_rate_ratio(rates: Mapping[str, float], current: str, target: str, band: float) -> Tuple[Optional[float], str]:
    """Target/current DBU-per-cluster-hour ratio from a workspace's billed rates, or None with the reason it is unusable.

    Usable only when the observed rates rise with size and the pair ratio is within `band` of the published size
    ratio; otherwise a guessed ratio could price a downsize as an upsize.
    """
    observed = sorted((SIZE_ORDER.index(s), safe_float(r)) for s, r in rates.items() if s in SIZE_ORDER and safe_float(r) > 0)
    if any(later <= earlier for (_, earlier), (_, later) in zip(observed, observed[1:])):
        return None, "RATES_NOT_RISING_WITH_SIZE"
    if safe_float(rates.get(current)) <= 0 or safe_float(rates.get(target)) <= 0:
        return None, "SIZE_RATE_NOT_OBSERVED"
    published_cur, published_target = SQL_DBU_PER_CLUSTER_HOUR.get(current), SQL_DBU_PER_CLUSTER_HOUR.get(target)
    if not (published_cur and published_target):
        return None, "NO_PUBLISHED_SIZE_RATE"
    ratio = safe_float(rates[target]) / safe_float(rates[current])
    if abs(ratio / (published_target / published_cur) - 1.0) > band:
        return None, "OUTSIDE_PUBLISHED_RATIO_BAND"
    return ratio, "VALIDATED"


class CostModel:
    """Candidate cost counterfactuals: per-knob DBU factors, AWS factors, resource bounds and migration costs. Mixed into the Estimator; ``self.t`` is the policy."""

    def _factors(self,e:WarehouseEvidence,d:OptimizationDecision,a:AnalysisResult)->Tuple[Optional[Range3],Dict[str,Any]]:
        """Low/base/high multiplicative factors on the forward DBU baseline for one decision, plus metadata (method, ``bundle_effect`` for the stateful estimator).

        Factor ranges are (optimistic, base, conservative) multipliers of baseline cost, so a factor below 1 is a
        saving and above 1 a cost increase. Returns (None, meta) when the candidate cannot be priced (blocked,
        insufficient evidence, canary rollback, missing baseline, unpublished or unvalidated rate): the economics
        are then null, never zero, and ``estimate_blocker`` names why when a gate can wait on it.
        """
        knob=d.knob
        meta:Dict[str,Any]={"estimator_version":ESTIMATOR_VERSION}
        if d.status in {"BLOCKED","INSUFFICIENT_EVIDENCE"}:
            # No counterfactual is attempted for a candidate the constraints reject: unknown cost is null, never zero.
            return None,{"method":"estimate withheld: the candidate is blocked or lacks the evidence to be priced","bundle_effect":{"type":"no_cost_change"},**meta}
        if d.status=="NO_CHANGE":
            return Range3(1.0,1.0,1.0),{"method":"no modeled change","bundle_effect":{"type":"no_cost_change"},**meta}
        if "CANARY_GUARDRAIL_BREACHED" in d.blockers:
            reason="A rollback restores the previous configuration; its cost is the previous configuration's, which this estimate does not price."
            return None,{"method":"estimate withheld: canary rollback","bundle_effect":{"type":"estimate_withheld","status":"UNAVAILABLE","reason":reason},**meta}
        if knob=="max_clusters" and d.objective=="COST_GUARDRAIL":
            return Range3(1.0,1.0,1.0),{"method":"guardrail/stability change; no direct savings assigned","bundle_effect":{"type":"no_cost_change"},**meta}
        if e.baseline_forecast_cost_365 is None or e.cost_history_days < self.t.min_economics_cost_history_days:
            # Every estimate below scales the forward DBU cost, which is unknown (unpriced usage, too few billed days, a
            # non-positive run rate) or not trustworthy on a young epoch, even when replayed evidence carries a number.
            blocker=dbu_baseline_blocker(e,self.t); status,reason=DBU_BASELINE_WITHHELD[blocker]
            return None,{
                "method":f"estimate withheld: {reason}",
                "estimate_blocker":blocker,
                "bundle_effect":{"type":"estimate_withheld","status":status,"reason":reason},**meta}
        if knob=="warehouse_type":
            return None,{"method":"warehouse-type migration uses explicit DBU + AWS pre-canary cost model","bundle_effect":{"type":"migration_cost_override"},**meta}

        if knob=="auto_stop_minutes":
            target=safe_int(d.recommended_config.get("auto_stop_minutes"))
            opt=next((o for o in a.facts.get("options") or [] if o["target"]==target),None)
            running=safe_float(a.facts.get("running_cluster_minutes_per_day"))
            if opt is None or running<=0 or not a.facts.get("priced_basis"):
                # Priced only on the DBU forecast's own days; the whole-period fallback (no covered basis) decides but is not priced.
                reason="The auto-stop step cannot be priced without the warehouse's running time on the DBU forecast's days (no retained warehouse event covering them); economics are withheld."
                return None,{"method":"estimate withheld: no running-idle evidence on the forecast basis for the recommended auto-stop",
                             "estimate_blocker":"RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE",
                             "bundle_effect":{"type":"estimate_withheld","status":"UNAVAILABLE","reason":reason},**meta}
            # Reclaimable cluster-minutes per day as a share of running cluster-minutes per day on the forecast's days, so
            # share x the DBU forecast = reclaimable cluster-hours a year x the forecast's cost per cluster-hour. The
            # realization tracks scale only the idle a lower value removes; restarts (added or saved) and the idle a
            # higher value adds are mechanical and count in full on every track, so the conservative track stays
            # conservative about restarts (a raise's restart billing is the canary's to measure).
            gain=opt["gain_cluster_minutes_per_day"]/running; loss=opt["loss_cluster_minutes_per_day"]/running
            realized,fixed=(gain,-loss) if opt["direction"]=="LOWER" else (0.0,gain-loss)
            shares=[min(1.0,realized*r+fixed) for r in (self.t.realization_optimistic,self.t.realization_base,self.t.realization_conservative)]
            factors=Range3(*(max(0.0,1.0-s) for s in shares))
            trend=safe_float(a.facts.get("trend_adjustment_factor"),1.0)
            return factors,{
                "method":"reclaimable running cluster-hours (idle gaps intersected with observed RUNNING time) at the recommended auto-stop, net of priced restarts, on the DBU forecast's days, annualized like the forecast",
                "savings_share_range":as_serializable(Range3(*shares)),"gain_share":round(gain,9),"loss_share":round(loss,9),"auto_stop_option":dict(opt),
                "running_cluster_minutes_per_day":running,"annual_net_cluster_hours":round(opt["net_cluster_minutes_per_day"]*365.0*trend/60.0,1),
                "cost_per_cluster_hour":a.facts.get("cost_per_cluster_hour"),
                "restart_cost_assumption":"Pro/Classic starting minutes are assumed billed at the floor cluster count (start time about 4 minutes, documented; billing not documented); Serverless restarts cost about nothing.",
                "bundle_effect":{"type":"idle_cluster_minute_reduction","realized_share":round(realized,9),"fixed_share":round(fixed,9),
                                 "floor_clusters":safe_int(a.facts.get("floor_clusters"),1)},
                **meta,
            }

        if knob=="min_clusters":
            current=safe_int(d.current_config.get("min_clusters"),1)
            target=safe_int(d.recommended_config.get("min_clusters"),current)
            share=(a.facts.get("floor_spare_shares") or {}).get(str(target))
            basis=a.facts.get("floor_spare_basis") or {}
            if share is None:
                # Priced only on spare floor capacity measured on the DBU forecast's days; the canary measures the rest.
                return None,{
                    "method":"estimate withheld: no time at the floor by demand on the DBU forecast's days",
                    "estimate_blocker":"FLOOR_DEMAND_EVIDENCE_UNAVAILABLE","floor_demand_check":basis.get("reason"),
                    "bundle_effect":{"type":"requires_canary"},**meta,
                }
            # Spare floor capacity (cluster-minutes the floor held that the demand of the minute did not need) is the most
            # the lower floor removes; the realization tracks (shared with auto-stop) scale it for the autoscaler holding
            # clusters after demand drops.
            realization=[self.t.realization_optimistic,self.t.realization_base,self.t.realization_conservative]
            factors=Range3(*(max(0.0,1.0-share*r) for r in realization))
            spare_per_day=safe_float((a.facts.get("floor_spare_cluster_minutes_per_day") or {}).get(str(target)))
            return factors,{
                "method":"spare floor capacity on the DBU forecast's days (running cluster-minutes at the floor the demand did not need, above the new floor) as a share of running cluster-minutes, annualized like the forecast; realization tracks for scale-down holds; canary required",
                "spare_floor_share":share,"realization_range":realization,
                "spare_floor_cluster_minutes_per_day":spare_per_day,"running_cluster_minutes_per_day":basis.get("running_cluster_minutes_per_day"),
                "annual_spare_floor_cluster_hours":round(spare_per_day*365.0*safe_float(basis.get("trend_adjustment_factor"),1.0)/60.0,1),
                "spare_floor_basis":{k:basis.get(k) for k in ("model","resource_window_start","resource_window_end","resource_window_days","trend_adjustment_factor")},
                "bundle_effect":{"type":"min_cluster_reduction","removed_share":share,"target_min_clusters":target},
                **meta,
            }

        if knob=="warehouse_size":
            cur=str(d.current_config.get("warehouse_size")); target=str(d.recommended_config.get("warehouse_size"))
            warehouse_type=str(e.current_epoch.config.warehouse_type or "").upper()
            if warehouse_type=="SERVERLESS":
                cur_dbu=SQL_DBU_PER_CLUSTER_HOUR.get(cur); target_dbu=SQL_DBU_PER_CLUSTER_HOUR.get(target)
                if not (cur_dbu and target_dbu):
                    # No published Serverless DBU/hour for one side (5X-Large): a nominal ratio would be a guess, so the
                    # candidate keeps its decision but is not priced, and any plan containing it needs a canary to price.
                    unpriced=[x for x in (cur,target) if not SQL_DBU_PER_CLUSTER_HOUR.get(x)]
                    return None,{
                        "method":"estimate withheld: no published Serverless DBU/hour for "+"/".join(unpriced)+"; a nominal size ratio would be a guess",
                        "estimate_blocker":"SERVERLESS_DBU_RATE_NOT_PUBLISHED","unpublished_sizes":unpriced,
                        "bundle_effect":{"type":"requires_canary"},**meta,
                    }
                capacity=target_dbu/cur_dbu
                capacity_source="DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR_RATIO"
            else:
                # Pro/Classic DBU per size is not documented first-party: price only on the workspace's billed rates
                # when they are consistent with the published size table; otherwise withhold (never a guessed ratio).
                empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
                ratio,check=validated_size_rate_ratio(empirical if isinstance(empirical,Mapping) else {},cur,target,self.t.size_rate_published_band)
                if ratio is None:
                    return None,{
                        "method":"estimate withheld: the workspace's billed DBU per cluster-hour by size does not validate this size change",
                        "estimate_blocker":"PRO_CLASSIC_SIZE_RATE_UNVERIFIED","size_rate_check":check,
                        "bundle_effect":{"type":"requires_canary"},**meta,
                    }
                capacity=ratio
                capacity_source="WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR_RATIO"
            # Speed-up multipliers follow the decided direction, never the price ratio.
            if SIZE_ORDER.index(target)<SIZE_ORDER.index(cur):
                busy_runtime=Range3(self.t.downsize_runtime_multiplier_optimistic,self.t.downsize_runtime_multiplier_base,self.t.downsize_runtime_multiplier_conservative)
            else:
                busy_runtime=Range3(self.t.upsize_runtime_multiplier_optimistic,self.t.upsize_runtime_multiplier_base,self.t.upsize_runtime_multiplier_conservative)
            # The multiplier changes query execution time only; idle running time is unchanged. Unknown busy share:
            # all running time is treated as busy.
            busy_share=a.facts.get("busy_share")
            share=1.0 if busy_share is None else float(busy_share)
            runtime=Range3(*(round(share*x+(1.0-share),6) for x in (busy_runtime.low,busy_runtime.base,busy_runtime.high)))
            factors=Range3(capacity*runtime.low,capacity*runtime.base,capacity*runtime.high)
            vals=sorted([
                clamp(factors.low,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
                clamp(factors.base,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
                clamp(factors.high,self.t.cost_factor_floor,self.t.cost_factor_ceiling),
            ])
            factors=Range3(vals[0],vals[1],vals[2])
            return factors,{
                "method":"capacity ratio × runtime elasticity on busy running time",
                "capacity_ratio":capacity,
                "capacity_ratio_source":capacity_source,
                "busy_share":busy_share,
                "busy_runtime_multiplier_range":as_serializable(busy_runtime),
                "runtime_multiplier_range":as_serializable(runtime),
                "bundle_effect":{"type":"multiplicative_cost_factor","resource_runtime_factor_range":as_serializable(runtime),"busy_share":share},
                **meta,
            }

        if knob=="max_clusters" and d.recommended_config.get("max_clusters",0)>d.current_config.get("max_clusters",0):
            at=safe_float(a.facts.get("minutes_at_max")); cluster=safe_float(a.facts.get("cluster_minutes"))
            current=safe_int(d.current_config.get("max_clusters"),1); target=safe_int(d.recommended_config.get("max_clusters"),current)
            delta=max(0,target-current)
            # Worst case: the added clusters run whenever the warehouse sat at its ceiling. No cap: a single-cluster
            # warehouse at its ceiling all the time doubles its cost with a second cluster.
            raw=max(0.0,(at*delta)/cluster) if cluster>0 else 0
            hist=a.facts.get("recent_concurrency_histogram")
            if e.current_epoch.config.warehouse_type!="SERVERLESS" and hist and cluster>0:
                # Base: each busy minute runs only the clusters its running + queued queries need under the new ceiling
                # (one per documented queries-per-cluster), never more than the worst case.
                qpc=QUERIES_PER_CLUSTER_PRO_CLASSIC
                added=sum(safe_float(w)*(min(math.ceil(int(level)/qpc),target)-min(math.ceil(int(level)/qpc),current)) for level,w in hist.items())
                base=min(raw,max(0.0,added/cluster)); method="cost uplift from the clusters peak demand needs under the new ceiling (base) up to the added clusters running whenever the warehouse sat at its ceiling (worst case)"
            else:
                base=raw*self.t.max_cluster_base_uplift_weight; method="upper-bound cost uplift from additional clusters during observed saturation"
            factors=Range3(1.0,1+base,1+raw)
            return factors,{
                "method":method+". "+MAX_INCREASE_COST_BOUNDS,
                "raw_uplift_fraction":round(raw,4),"base_uplift_fraction":round(base,4),
                "bundle_effect":{"type":"multiplicative_cost_factor","resource_runtime_factor_range":as_serializable(factors)},
                **meta,
            }

        return Range3(1,1,1),{"method":"no direct modeled cost delta","bundle_effect":{"type":"no_cost_change"},**meta}

    def _aws_factor_range(self,e:WarehouseEvidence,d:OptimizationDecision,dbu_factors:Optional[Range3],meta:Mapping[str,Any])->Optional[Range3]:
        """Factors for the AWS baseline: None when AWS cost is unknown; 1.0 for Serverless (no customer EC2); the DBU factors for non-size knobs; for a size change the EC2 cluster-hour ratio times the runtime multiplier."""
        if e.aws_baseline_cost_365 is None:
            return None
        if e.current_epoch.config.warehouse_type == "SERVERLESS":
            return Range3(1.0,1.0,1.0)
        if dbu_factors is None:
            return None
        if d.knob != "warehouse_size":
            return dbu_factors
        current=str(d.current_config.get("warehouse_size")); target=str(d.recommended_config.get("warehouse_size"))
        cur_hourly,_=aws_cluster_hourly_from_evidence(e,current); target_hourly,_=aws_cluster_hourly_from_evidence(e,target)
        rr=meta.get("runtime_multiplier_range")
        if cur_hourly is None or target_hourly is None or not isinstance(rr,Mapping) or cur_hourly<=0:
            return None
        ratio=target_hourly/cur_hourly
        vals=sorted([ratio*safe_float(rr.get("low"),1.0),ratio*safe_float(rr.get("base"),1.0),ratio*safe_float(rr.get("high"),1.0)])
        return Range3(vals[0],vals[1],vals[2])

    def _annualized_resource_bounds(self,e:WarehouseEvidence)->Tuple[Optional[Range3],Dict[str,Any]]:
        """Annualized current-epoch cluster-minutes as (uptime, run-rate, uptime x peak clusters) bounds; None with a status when the resource window is missing or empty. Current epoch only: running time never carries across epochs."""
        # Same window and model as the DBU forecast (run-rate blend, or whole billing cycles): AWS/resource bounds and
        # DBU describe the same year.
        rates,info=annualized_resource_rates(e.current_epoch,e.forecast_metadata or {},self.t,("uptime_minutes","cluster_minutes"))
        if rates is None:
            return None,{"status":"UNAVAILABLE",**info}
        cluster_minutes=info["window_cluster_minutes"]; uptime=info["window_uptime_minutes"]; max_obs=max(1,int(info["max_observed_clusters"] or 1))
        if cluster_minutes<=0 or uptime<=0:
            return None,{"status":"UNAVAILABLE","blocker":"INSUFFICIENT_CURRENT_EPOCH_RESOURCE_EVIDENCE","resource_window_days":info["resource_window_days"],"cluster_minutes":cluster_minutes,"uptime_minutes":uptime}
        trend=info["trend_adjustment_factor"]
        low=rates["uptime_minutes"]
        base=rates["cluster_minutes"]
        high=max(base,low*max_obs)
        vals=sorted([low,base,high])
        return Range3(vals[0],vals[1],vals[2]),{"status":"MODELED","method":"recent resource-time structural bounds on the DBU forecast's window and model, annualized forward","resource_window_start":info["resource_window_start"],"resource_window_end":info["resource_window_end"],"resource_window_days":info["resource_window_days"],"trend_adjustment_factor":trend,"window_cluster_minutes":cluster_minutes,"window_uptime_minutes":uptime,"max_observed_clusters":max_obs,"annualized_cluster_minutes_range":as_serializable(Range3(vals[0],vals[1],vals[2]))}

    def _current_model_reconciliation(self,e:WarehouseEvidence,resources:Range3)->Dict[str,Any]:
        """Modeled current DBU cost (running cluster-hours x current DBU/hour x current price) over the billed forecast."""
        cfg=e.current_epoch.config
        if cfg.warehouse_type=="SERVERLESS":
            rate=SQL_DBU_PER_CLUSTER_HOUR.get(cfg.warehouse_size)
        else:
            empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
            rate=(safe_float(empirical.get(cfg.warehouse_size),0.0) if isinstance(empirical,Mapping) else 0.0) or None
        price,_=resolve_target_sku_price(e.sql_sku_prices,cfg.warehouse_type)
        billed=e.baseline_forecast_cost_365
        if not rate or price is None or not billed:
            return {"blocker":"MIGRATION_MODEL_NOT_RECONCILED","reason":"CURRENT_RATE_OR_PRICE_UNAVAILABLE","ratio":None}
        modeled=resources.base/60.0*rate*price
        ratio=round(modeled/billed,6)
        out={"ratio":ratio,"modeled_current_dbu_cost_365":round(modeled,2),"billed_dbu_forecast_365":round(billed,2),"current_dbu_per_cluster_hour":rate,"current_unit_price":price,
             "band":[self.t.migration_reconciliation_min,self.t.migration_reconciliation_max]}
        if not self.t.migration_reconciliation_min<=ratio<=self.t.migration_reconciliation_max:
            out["blocker"]="MIGRATION_MODEL_NOT_RECONCILED"
        return out

    def _migration_costs(self,e:WarehouseEvidence,d:OptimizationDecision,facts:Optional[Mapping[str,Any]]=None)->Dict[str,Any]:
        """Pre-canary DBU, AWS and total annual cost ranges of the target warehouse type at the current size.

        Returns ``{"available": False, "blocker": code, ...}`` whenever a required input is missing (resource
        window, running fraction, target DBU rate, SKU price, reconciliation, EC2 price); no input is guessed. The
        target is priced relative to billing (scaled by billed / modeled) so systematic model error cancels.
        Serverless AWS cost is a true zero (infrastructure is inside the DBU line); Pro/Classic AWS is modeled.
        """
        target=str(d.recommended_config.get("warehouse_type") or "").upper()
        size=e.current_epoch.config.warehouse_size
        current_resources,resource_meta=self._annualized_resource_bounds(e)
        if current_resources is None:
            return {"available":False,"blocker":resource_meta.get("blocker"),"resource_model":resource_meta}
        fraction=(facts or {}).get("migration_running_fraction")
        if fraction is None:
            return {"available":False,"blocker":"RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE","resource_model":resource_meta}
        # The target runs at its own best auto-stop: base = today's annualized cluster-minutes x its running fraction;
        # optimistic = busy cluster-time only (it never runs idle); conservative = today's structural upper bound
        # (every running minute at the peak cluster count), never below the base.
        busy_share=(facts or {}).get("busy_share")
        base_t=current_resources.base*fraction
        low_t=min(base_t,current_resources.base*float(busy_share)) if busy_share is not None else base_t
        resources=Range3(round(low_t,3),round(base_t,3),round(max(base_t,current_resources.high),3))
        resource_meta={**resource_meta,"target_running_fraction":fraction,"target_auto_stop_option":as_serializable((facts or {}).get("migration_target_option")),
                       "busy_share":busy_share,"target_annualized_cluster_minutes_range":as_serializable(resources),
                       "target_method":"target at its own cost-minimizing auto-stop (base), busy cluster-time only (optimistic), today's running time at the peak cluster count (conservative)"}
        if target=="SERVERLESS":
            dbu_per_hour=SQL_DBU_PER_CLUSTER_HOUR.get(size)
            rate_source="DATABRICKS_PUBLISHED_SQL_SERVERLESS_DBU_PER_HOUR"
        else:
            empirical=e.billing_features.get("workspace_pro_dbu_per_cluster_hour_by_size") or {}
            dbu_per_hour=safe_float(empirical.get(size),0.0) if isinstance(empirical,Mapping) else 0.0
            dbu_per_hour=dbu_per_hour or None
            rate_source="WORKSPACE_SYSTEM_TABLE_EMPIRICAL_PRO_CLASSIC_DBU_PER_CLUSTER_HOUR"
        if dbu_per_hour is None:
            return {"available":False,"blocker":"TARGET_DBU_PER_CLUSTER_HOUR_NOT_AVAILABLE","warehouse_size":size,"target_type":target,"resource_model":resource_meta,"rate_source":rate_source}
        unit_price,price_meta=resolve_target_sku_price(e.sql_sku_prices,target)
        if unit_price is None:
            return {"available":False,"blocker":price_meta.get("blocker"),"sku_price_model":price_meta,"resource_model":resource_meta}
        # Reconcile: the same formula on the current warehouse must reproduce its billed DBU forecast; the target is then
        # priced relative to billing (scaled by billed / modeled), so systematic model error cancels out.
        reconciliation=self._current_model_reconciliation(e,current_resources)
        if reconciliation.get("blocker"):
            return {"available":False,"blocker":reconciliation["blocker"],"reconciliation_ratio":reconciliation.get("ratio"),"reconciliation":reconciliation,"resource_model":resource_meta,"sku_price_model":price_meta}
        scale=1.0/reconciliation["ratio"]
        dbu_cost=Range3(*(round(x/60.0*dbu_per_hour*unit_price*scale,2) for x in (resources.low,resources.base,resources.high)))
        if target=="SERVERLESS":
            aws_cost=Range3(0.0,0.0,0.0)
            aws_meta={"status":"NOT_CUSTOMER_BILLED","reason":"Serverless SQL infrastructure is Databricks-managed; no separate customer EC2 line is modeled."}
        elif target in {"PRO","CLASSIC"}:
            hourly,aws_meta=aws_cluster_hourly_from_evidence(e,size)
            if hourly is None:
                return {"available":False,"blocker":aws_meta.get("blocker"),"sku_price_model":price_meta,"resource_model":resource_meta,"aws_model":aws_meta}
            aws_cost=Range3(*(round(x/60.0*hourly,2) for x in (resources.low,resources.base,resources.high)))
        else:
            return {"available":False,"blocker":"UNSUPPORTED_TARGET_WAREHOUSE_TYPE","target_type":target}
        total_cost=Range3(round(dbu_cost.low+aws_cost.low,2),round(dbu_cost.base+aws_cost.base,2),round(dbu_cost.high+aws_cost.high,2))
        return {"available":True,"target_type":target,"warehouse_size":size,"reconciliation_ratio":reconciliation["ratio"],"reconciliation":reconciliation,"dbu_cost_range":dbu_cost,"aws_cost_range":aws_cost,"total_cost_range":total_cost,"dbu_per_cluster_hour":dbu_per_hour,"dbu_rate_source":rate_source,"unit_price_per_dbu":unit_price,"resource_model":resource_meta,"sku_price_model":price_meta,"aws_model":aws_meta,"estimate_status":"PRE_CANARY_MODELED_RANGE"}
