"""Product defaults: warehouse sizes, published DBU and AWS topology tables, the active recommendation knobs and the supported (fail-closed) warehouse types."""
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
