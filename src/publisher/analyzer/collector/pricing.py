"""Price sources: SQL SKU classification, the AWS EC2 price registry and the AWS baseline cost model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from publisher.analyzer.metrics import annualized_resource_rates
from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.common.utils import safe_float
from publisher.config.defaults import AWS_SQL_WAREHOUSE_TOPOLOGY
from publisher.governor.thresholds import Thresholds
from publisher.contracts.source import EpochEvidence


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
    driver = str(topology["driver"]); workers = int(topology["workers"]); worker_type = "i3.2xlarge"
    driver_price = region_prices.get(driver); worker_price = region_prices.get(worker_type)
    if driver_price is None or worker_price is None:
        return None, {"status": "UNAVAILABLE", "blocker": "AWS_INSTANCE_PRICE_MISSING", "region": region, "driver": driver, "worker": worker_type}
    hourly = safe_float(driver_price) + workers * safe_float(worker_price)
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
