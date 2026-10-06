"""Product defaults: warehouse sizes, published DBU and AWS topology tables, the active recommendation knobs, the supported (fail-closed) warehouse types and the Databricks product constants."""
from __future__ import annotations


GIB = 1024 ** 3


SIZE_ORDER = [
    "2X_SMALL", "X_SMALL", "SMALL", "MEDIUM", "LARGE", "X_LARGE",
    "2X_LARGE", "3X_LARGE", "4X_LARGE", "5X_LARGE",
]


# Published SQL Serverless DBU/hour schedule by nominal warehouse size. The
# production cost model uses account pricing from system.billing.list_prices;
# this table is only the documented DBU consumption rate per cluster-hour.
SQL_DBU_PER_CLUSTER_HOUR = {
    "2X_SMALL": 4.0, "X_SMALL": 6.0, "SMALL": 12.0, "MEDIUM": 24.0,
    "LARGE": 40.0, "X_LARGE": 80.0, "2X_LARGE": 144.0,
    "3X_LARGE": 272.0, "4X_LARGE": 528.0,
}


# Databricks-documented AWS topology for Pro/Classic SQL warehouses. All workers
# use i3.2xlarge; the driver type and worker count vary by warehouse size.
AWS_SQL_WAREHOUSE_TOPOLOGY = {
    "2X_SMALL": {"driver": "i3.2xlarge", "workers": 1},
    "X_SMALL": {"driver": "i3.2xlarge", "workers": 2},
    "SMALL": {"driver": "i3.4xlarge", "workers": 4},
    "MEDIUM": {"driver": "i3.8xlarge", "workers": 8},
    "LARGE": {"driver": "i3.8xlarge", "workers": 16},
    "X_LARGE": {"driver": "i3.16xlarge", "workers": 32},
    "2X_LARGE": {"driver": "i3.16xlarge", "workers": 64},
    "3X_LARGE": {"driver": "i3.16xlarge", "workers": 128},
    "4X_LARGE": {"driver": "i3.16xlarge", "workers": 256},
    "5X_LARGE": {"driver": "i3.16xlarge", "workers": 512},
}


ACTIVE_KNOBS = (
    "warehouse_size",
    "max_clusters",
    "min_clusters",
    "auto_stop_minutes",
    "warehouse_type",
)


SUPPORTED_WAREHOUSE_TYPES = frozenset({"CLASSIC", "PRO", "SERVERLESS"})


# Databricks product facts: documented platform behavior and limits, not policy (docs/reference/product-constants.md).
# Minutes before a warehouse adds clusters for queued demand: the busy-period start window, and the idle minutes that
# separate busy periods.
SCALE_UP_LAG_MINUTES_PRO_CLASSIC = 5
SCALE_UP_LAG_MINUTES_SERVERLESS = 1
# Lowest auto-stop (minutes) the API accepts for Pro/Classic; Serverless values below 5 set through the API are left alone.
AUTO_STOP_FLOOR_PRO_CLASSIC = 10
AUTO_STOP_FLOOR_SERVERLESS = 5
AUTO_STOP_MAX_MINUTES = 1440
MAX_CLUSTERS_SUPPORTED_LIMIT = 40
MIN_CLUSTERS_SUPPORTED_LIMIT = 30
# Pro/Classic add one cluster per this many concurrent queries (documented scaling rule).
QUERIES_PER_CLUSTER_PRO_CLASSIC = 10

PRODUCT_CONSTANTS = {
    "SCALE_UP_LAG_MINUTES_PRO_CLASSIC": SCALE_UP_LAG_MINUTES_PRO_CLASSIC,
    "SCALE_UP_LAG_MINUTES_SERVERLESS": SCALE_UP_LAG_MINUTES_SERVERLESS,
    "AUTO_STOP_FLOOR_PRO_CLASSIC": AUTO_STOP_FLOOR_PRO_CLASSIC,
    "AUTO_STOP_FLOOR_SERVERLESS": AUTO_STOP_FLOOR_SERVERLESS,
    "AUTO_STOP_MAX_MINUTES": AUTO_STOP_MAX_MINUTES,
    "MAX_CLUSTERS_SUPPORTED_LIMIT": MAX_CLUSTERS_SUPPORTED_LIMIT,
    "MIN_CLUSTERS_SUPPORTED_LIMIT": MIN_CLUSTERS_SUPPORTED_LIMIT,
    "QUERIES_PER_CLUSTER_PRO_CLASSIC": QUERIES_PER_CLUSTER_PRO_CLASSIC,
}


def scale_up_lag_minutes(warehouse_type: str) -> int:
    """Scale-up lag (minutes) of a warehouse type."""
    return SCALE_UP_LAG_MINUTES_SERVERLESS if warehouse_type == "SERVERLESS" else SCALE_UP_LAG_MINUTES_PRO_CLASSIC


def auto_stop_floor(warehouse_type: str) -> int:
    """Lowest auto-stop (minutes) evaluated for a warehouse type."""
    return AUTO_STOP_FLOOR_SERVERLESS if warehouse_type == "SERVERLESS" else AUTO_STOP_FLOOR_PRO_CLASSIC
# Run scope hierarchy: scenario -> (contract scenario type, technique name -> knob). Every active knob belongs to exactly
# one scenario. Technique names are the scope-file keys; the CLI also accepts them with hyphens (auto-stop).
SCOPE_SCENARIOS = {
    "current": ("IN_PLACE_OPTIMIZATION", {
        "size": "warehouse_size",
        "max_clusters": "max_clusters",
        "min_clusters": "min_clusters",
        "auto_stop": "auto_stop_minutes",
    }),
    "migration": ("WAREHOUSE_TYPE_MIGRATION", {
        "warehouse_type": "warehouse_type",
    }),
}
