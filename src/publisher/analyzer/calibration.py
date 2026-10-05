"""Workspace calibration from collected evidence: observed warehouse types and billed Pro/Classic DBU rates per size."""
from __future__ import annotations

import datetime as dt
import statistics
from typing import Any, Dict, List, Mapping, Optional, Sequence

from publisher.analyzer.metrics import resource_window
from publisher.common.time import parse_datetime
from publisher.common.utils import safe_float
from publisher.config.thresholds import Thresholds
from publisher.contracts.source import EpochEvidence, WarehouseEvidence


def _billed_dbu_per_cluster_hour(x: WarehouseEvidence, ep: EpochEvidence, t: Thresholds) -> Optional[float]:
    """Billed DBU per running cluster-hour for one configuration epoch, with billing and running time on the same days.

    The current epoch uses its event-covered resource window; an earlier epoch (calibration evidence only) uses its
    whole-epoch event totals and the billing days from its event coverage start. Unknown when billed DBU per day or the
    event coverage start was not collected.
    """
    def rows_for(days: Optional[set], from_day: Optional[str]) -> List[Mapping[str, Any]]:
        return [r for r in x.daily_cost_365 if str(r.get("epoch_start")) == ep.epoch_start
                and (days is None or str(r.get("usage_date"))[:10] in days) and (from_day is None or str(r.get("usage_date"))[:10] >= from_day)]
    if ep.is_current:
        window = resource_window(x, t)
        if window is None:
            return None
        rows = rows_for({(dt.date.fromisoformat(window["start"]) + dt.timedelta(days=i)).isoformat() for i in range(window["days"])}, None)
        cluster_minutes = sum(window["cluster_minutes"])
    else:
        coverage = parse_datetime(ep.events.get("event_coverage_start"))
        if coverage is None:
            return None
        rows = rows_for(None, coverage.astimezone(dt.timezone.utc).date().isoformat())
        cluster_minutes = safe_float(ep.events.get("cluster_minutes"))
    if not rows or any(r.get("dbu_quantity") is None for r in rows):
        return None
    dbu = sum(safe_float(r.get("dbu_quantity")) for r in rows)
    return dbu / (cluster_minutes / 60.0) if dbu > 0 and cluster_minutes > 0 else None


def _prepare_workspace_calibration(evidence: Sequence[WarehouseEvidence], thresholds: Optional[Thresholds] = None) -> None:
    """Attach workspace-level capability/calibration observations derived only from collected system-table evidence."""
    t = thresholds or Thresholds()
    serverless_workspaces = {x.workspace_id for x in evidence if any(ep.config.warehouse_type == "SERVERLESS" for ep in x.epochs)}
    pro_classic_workspaces = {x.workspace_id for x in evidence if any(ep.config.warehouse_type in {"PRO", "CLASSIC"} for ep in x.epochs)}
    pro_rates: Dict[str, Dict[str, List[float]]] = {}
    for x in evidence:
        for ep in x.epochs:
            if ep.config.warehouse_type in {"PRO", "CLASSIC"}:
                rate = _billed_dbu_per_cluster_hour(x, ep, t)
                if rate is not None:
                    pro_rates.setdefault(x.workspace_id, {}).setdefault(ep.config.warehouse_size, []).append(rate)
    rate_registry = {
        ws: {size: round(statistics.median(values), 6) for size, values in sizes.items() if values}
        for ws, sizes in pro_rates.items()
    }
    for x in evidence:
        x.billing_features["workspace_serverless_observed_from_system_compute_warehouses"] = x.workspace_id in serverless_workspaces
        x.billing_features["workspace_pro_classic_observed_from_system_compute_warehouses"] = x.workspace_id in pro_classic_workspaces
        x.billing_features["workspace_pro_dbu_per_cluster_hour_by_size"] = rate_registry.get(x.workspace_id, {})
