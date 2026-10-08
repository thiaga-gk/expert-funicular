"""Price sources: SQL SKU classification, the AWS EC2 price registry and the AWS cost models (forward baseline and trailing actual)."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.metrics import annualized_resource_rates
from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.common.time import parse_datetime
from publisher.common.utils import safe_float
from publisher.config.defaults import AWS_SQL_WAREHOUSE_TOPOLOGY, SQL_DBU_PER_CLUSTER_HOUR
from publisher.governor.thresholds import Thresholds
from publisher.contracts.source import EpochEvidence, WarehouseEvidence

AWS_WORKER_INSTANCE = "i3.2xlarge"  # every documented Pro/Classic size uses this worker type


def classify_sql_sku(sku_name: str) -> str:
    """Classify a billing SKU name as SERVERLESS, PRO, CLASSIC (SQL SKUs) or OTHER (not a SQL SKU), by name only."""
    n = str(sku_name or "").upper()
    if "SQL" not in n:
        return "OTHER"
    if "SERVERLESS" in n:
        return "SERVERLESS"
    if "SQL_PRO" in n or "PRO_SQL" in n or ("PRO" in n and "SQL" in n):
        return "PRO"
    return "CLASSIC"


def load_aws_price_registry(path: Optional[str]) -> Dict[str, Any]:
    """Load the versioned regional EC2 on-demand price registry (JSON object), or {} when no path is given.

    An empty registry is valid: every AWS baseline then comes back UNAVAILABLE (null), never a guessed price. A missing
    file or a non-object payload raises PublisherError.
    """
    if not path:
        LOGGER.warning("pricing.registry_unavailable reason=NO_REGISTRY_PATH")
        return {}
    p = Path(path)
    if not p.exists():
        raise PublisherError(f"AWS price registry not found: {path}")
    payload = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise PublisherError(f"AWS price registry must be a JSON object: {path}")
    LOGGER.info("pricing.registry_loaded regions=%d", len(payload.get("regions") or {}) if isinstance(payload.get("regions"), Mapping) else 0)
    return dict(payload)


def cluster_hourly(size: str, region_prices: Mapping[str, Any]) -> Optional[float]:
    """On-demand EC2 cost of one cluster-hour of ``size``: the driver's hourly price plus worker count × the worker's.

    None when the size has no documented topology or a node type has no regional price (unknown, never zero).
    """
    topology = AWS_SQL_WAREHOUSE_TOPOLOGY.get(size)
    if not topology or not isinstance(region_prices, Mapping):
        return None
    driver_price = region_prices.get(str(topology["driver"])); worker_price = region_prices.get(AWS_WORKER_INSTANCE)
    if driver_price is None or worker_price is None:
        return None
    return safe_float(driver_price) + int(topology["workers"]) * safe_float(worker_price)


def aws_cluster_hourly_price(size: str, region: str, registry: Mapping[str, Any]) -> Tuple[Optional[float], Dict[str, Any]]:
    """Hourly AWS cost of one cluster of ``size`` (driver + workers from the documented topology) in ``region``.

    Returns ``(price, info)``. ``price`` is None with an ``info`` blocker (size not in topology, region or instance
    price missing from the registry) when it cannot be modeled; unknown is never priced at zero.
    """
    topology = AWS_SQL_WAREHOUSE_TOPOLOGY.get(size)
    if not topology:
        return None, {"status": "UNAVAILABLE", "blocker": "WAREHOUSE_SIZE_NOT_IN_AWS_TOPOLOGY", "warehouse_size": size}
    regions = registry.get("regions") if isinstance(registry, Mapping) else None
    region_prices = regions.get(region) if isinstance(regions, Mapping) else None
    if not isinstance(region_prices, Mapping):
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_REGION_NOT_IN_REGISTRY", "region": region}
    driver = str(topology["driver"]); workers = int(topology["workers"]); worker_type = AWS_WORKER_INSTANCE
    driver_price = region_prices.get(driver); worker_price = region_prices.get(worker_type)
    hourly = cluster_hourly(size, region_prices)
    if hourly is None:
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_INSTANCE_PRICE_MISSING", "region": region, "driver": driver, "worker": worker_type}
    return round(hourly, 6), {
        "status": "MODELED", "pricing_mode": registry.get("pricing_mode", "UNKNOWN"), "currency": registry.get("currency", "USD"),
        "region": region, "warehouse_size": size, "driver_instance": driver, "driver_hourly_price": safe_float(driver_price),
        "worker_instance": worker_type, "worker_count": workers, "worker_hourly_price": safe_float(worker_price),
        "cluster_hourly_price": round(hourly, 6), "sources": registry.get("sources", []),
        "region_instance_prices": {str(k): safe_float(v) for k, v in region_prices.items()},
    }


def model_aws_baseline_cost(current: EpochEvidence, source_region: str, analysis_time: str, forecast_metadata: Mapping[str, Any], aws_registry: Mapping[str, Any], thresholds: Thresholds, *, observed_node_types: Optional[Sequence[str]] = None) -> Tuple[Optional[float], Dict[str, Any]]:
    """Annual customer AWS (EC2) baseline for the current epoch, kept separate from DBU cost; ``(cost, model)``.

    Annualizes the current epoch's cluster-minutes (same basis as the DBU forecast) times the topology's hourly price.
    SERVERLESS returns 0.0 with status NOT_CUSTOMER_BILLED (Databricks-managed infrastructure). Anything that cannot be
    established returns ``(None, model)`` with a blocker (price missing, billed node types outside the topology,
    insufficient cluster-minutes): unknown cost is null, never zero. ``analysis_time`` is accepted for signature
    symmetry with the forecast and is not used here.
    """
    if current.config.warehouse_type == "SERVERLESS":
        region_prices = dict((aws_registry.get("regions") or {}).get(source_region) or {})
        return 0.0, {
            "status": "NOT_CUSTOMER_BILLED",
            "reason": "Serverless SQL runs on Databricks-managed infrastructure; customer AWS EC2 is not modeled as a separate line item.",
            "region": source_region,
            "region_instance_prices": region_prices,
            "registry_region_available": bool(region_prices),
        }
    hourly, topo = aws_cluster_hourly_price(current.config.warehouse_size, source_region, aws_registry)
    if hourly is None:
        return None, topo
    # Billed node types (driver and workers are not distinguished) must belong to the modeled topology for this size.
    billed_nodes = sorted({str(x) for x in (observed_node_types or []) if x})
    if billed_nodes and not set(billed_nodes) <= {topo["driver_instance"], topo["worker_instance"]}:
        return None, {**topo, "status": "UNAVAILABLE", "blocker": "AWS_NODE_TYPE_MISMATCH", "observed_node_types": billed_nodes,
                      "expected_node_types": sorted({topo["driver_instance"], topo["worker_instance"]})}
    topo = {**topo, "node_type_check": "VERIFIED" if billed_nodes else "UNVERIFIED", "observed_node_types": billed_nodes}
    rates, info = annualized_resource_rates(current, forecast_metadata, thresholds, ("cluster_minutes",))
    if rates is None:
        return None, {**topo, "status": "UNAVAILABLE", **info}
    cluster_minutes = info["window_cluster_minutes"]
    if cluster_minutes <= 0:
        return None, {**topo, "status": "UNAVAILABLE", "blocker": "INSUFFICIENT_WAREHOUSE_EVENT_CLUSTER_MINUTES", "cluster_minutes": cluster_minutes, "resource_window_days": info["resource_window_days"]}
    annual_cluster_minutes = rates["cluster_minutes"]
    cost = annual_cluster_minutes / 60.0 * hourly
    cadence = info["model"] == "CADENCE"
    return round(cost, 2), {
        **topo, "status": "MODELED",
        "method": ("cluster-minutes over the DBU forecast's whole billing cycles" if cadence else "cluster-minute run rate on the DBU forecast's window and blend")
                  + " × documented SQL warehouse AWS topology × versioned regional EC2 price registry",
        "observed_cluster_minutes": round(cluster_minutes, 3), "resource_window_start": info["resource_window_start"], "resource_window_end": info["resource_window_end"],
        "resource_window_days": info["resource_window_days"], "trend_adjustment_factor": round(info["trend_adjustment_factor"], 6), "annualized_cluster_minutes": round(annual_cluster_minutes, 3),
        "annualized_cluster_hours": round(annual_cluster_minutes / 60.0, 3),
        **({"cadence_cycles": info["cadence_cycles"]} if cadence else {}),
    }


def model_aws_actual_cost(e: WarehouseEvidence) -> Tuple[Optional[float], Dict[str, Any]]:
    """Modeled customer AWS (EC2) cost of the trailing 365 days, over every epoch in the window; ``(cost, model)``.

    Billing records DBU, not hours, so each Pro/Classic billing row's cluster-hours are its DBU over the published DBU
    rate of the size its epoch ran (``SQL_DBU_PER_CLUSTER_HOUR``), priced at that size's cluster-hour (driver plus
    workers from the documented topology, regional on-demand prices from the evidence's AWS model). Serverless rows add
    0 (Databricks-managed infrastructure). The window is the trailing actual's: [analysis date - 365 days, last settled
    billing day]. Unpriced DBU counts: the cluster ran even when its DBU price is unknown. Returns ``(None, model)`` with
    a blocker when any contributing row cannot be priced (unknown epoch or size, no regional price, falsified topology,
    price currency not the billing currency, no settled window end): unknown is null, never zero.
    """
    model = e.aws_cost_model if isinstance(e.aws_cost_model, Mapping) else {}
    meta = e.forecast_metadata or {}
    end_text = meta.get("billing_complete_through") or meta.get("forecast_basis_end")
    analysis, end = parse_datetime(e.analysis_time), parse_datetime(end_text) if end_text else None
    if analysis is None or end is None:
        return None, {"status": "UNAVAILABLE", "blocker": "BILLING_WINDOW_UNKNOWN"}
    window_start = analysis.astimezone(dt.timezone.utc).date() - dt.timedelta(days=365)
    window_end = end.date()
    epochs = {parse_datetime(ep.epoch_start): ep.config for ep in e.epochs}
    prices = model.get("region_instance_prices")
    cost, cluster_hours, modeled_rows, serverless_rows = 0.0, 0.0, 0, 0
    for r in e.daily_cost_365:
        day = parse_datetime(r.get("usage_date"))
        if day is None or not window_start <= day.date() <= window_end:
            continue
        if r.get("dbu_quantity") is None:
            return None, {"status": "UNAVAILABLE", "blocker": "BILLED_DBU_QUANTITY_MISSING", "usage_date": str(r.get("usage_date"))[:10]}
        dbu = safe_float(r.get("dbu_quantity"))
        if dbu == 0:
            continue
        config = epochs.get(parse_datetime(r.get("epoch_start")))
        if config is None:
            return None, {"status": "UNAVAILABLE", "blocker": "BILLING_ROW_EPOCH_UNKNOWN", "usage_date": str(r.get("usage_date"))[:10]}
        if config.warehouse_type == "SERVERLESS":
            serverless_rows += 1
            continue
        rate = SQL_DBU_PER_CLUSTER_HOUR.get(config.warehouse_size)
        hourly = cluster_hourly(config.warehouse_size, prices) if isinstance(prices, Mapping) else None
        if rate is None or hourly is None:
            return None, {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_EVIDENCE_NOT_AVAILABLE" if isinstance(prices, Mapping) else model.get("blocker") or "AWS_PRICE_EVIDENCE_NOT_AVAILABLE",
                          "warehouse_size": config.warehouse_size, "region": e.source_region}
        hours = dbu / rate
        cluster_hours += hours; cost += hours * hourly; modeled_rows += 1
    if modeled_rows:
        if model.get("blocker") == "AWS_NODE_TYPE_MISMATCH":
            return None, {"status": "UNAVAILABLE", "blocker": "AWS_NODE_TYPE_MISMATCH"}
        price_currency = str(model.get("currency") or "USD")
        if price_currency != e.currency:
            return None, {"status": "UNAVAILABLE", "blocker": "AWS_PRICE_CURRENCY_MISMATCH", "price_currency": price_currency, "billing_currency": e.currency}
        if cost < 0:
            return None, {"status": "UNAVAILABLE", "blocker": "NEGATIVE_NET_BILLED_DBU"}
    return round(cost, 2), {
        "status": "MODELED" if modeled_rows else ("NOT_CUSTOMER_BILLED" if serverless_rows else "NO_BILLED_USAGE"),
        "window_start": window_start.isoformat(), "window_end": window_end.isoformat(),
        "modeled_rows": modeled_rows, "serverless_rows": serverless_rows, "cluster_hours": round(cluster_hours, 3),
    }
