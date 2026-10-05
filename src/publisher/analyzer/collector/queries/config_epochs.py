"""Configuration-epoch SQL over system.compute.warehouses snapshots."""
from __future__ import annotations




# Configuration epochs are islands of the decision fields. system.compute.warehouses writes a snapshot whenever any
# property changes, including the name and tags; those cosmetic snapshots are folded into the surrounding period so they
# never reset the epoch-maturity gate, the cost history or the forecast basis. A period starts at the first snapshot
# whose decision fields differ from the previous snapshot; its name and tags come from its latest snapshot. Portable SQL
# (window functions, IS DISTINCT FROM) shared by every collector query that groups activity by configuration epoch.
# The table has no unique snapshot key, so snapshots sharing a change_time are first collapsed to one deterministic row
# (a deleted snapshot wins, so the warehouse fails closed as deleted); every later window then orders by a unique key.
CONFIG_EPOCHS_CTE = """config_snapshot_ties AS (
  SELECT
    account_id, workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_channel, warehouse_size,
    min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
    ROW_NUMBER() OVER (
      PARTITION BY workspace_id, warehouse_id, change_time
      ORDER BY CASE WHEN delete_time IS NULL THEN 1 ELSE 0 END, warehouse_type, warehouse_channel, warehouse_size,
        min_clusters, max_clusters, auto_stop_minutes, warehouse_name
    ) AS rn_tie
  FROM system.compute.warehouses
), config_snapshots AS (
  SELECT
    account_id, workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_channel, warehouse_size,
    min_clusters, max_clusters, auto_stop_minutes, tags, change_time, delete_time,
    CASE WHEN LAG(change_time) OVER w IS NULL
      OR LAG(warehouse_type) OVER w IS DISTINCT FROM warehouse_type
      OR LAG(warehouse_channel) OVER w IS DISTINCT FROM warehouse_channel
      OR LAG(warehouse_size) OVER w IS DISTINCT FROM warehouse_size
      OR LAG(min_clusters) OVER w IS DISTINCT FROM min_clusters
      OR LAG(max_clusters) OVER w IS DISTINCT FROM max_clusters
      OR LAG(auto_stop_minutes) OVER w IS DISTINCT FROM auto_stop_minutes
    THEN 1 ELSE 0 END AS starts_epoch,
    ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time DESC) AS rn_desc
  FROM config_snapshot_ties
  WHERE rn_tie = 1
  WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY change_time)
), config_islands AS (
  SELECT *,
    SUM(starts_epoch) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS epoch_seq
  FROM config_snapshots
), config_island_rows AS (
  SELECT *,
    MIN(change_time) OVER (PARTITION BY workspace_id, warehouse_id, epoch_seq) AS epoch_start,
    ROW_NUMBER() OVER (PARTITION BY workspace_id, warehouse_id, epoch_seq ORDER BY change_time DESC) AS rn_in_epoch
  FROM config_islands
), config_epochs AS (
  SELECT
    account_id, workspace_id, warehouse_id, warehouse_name, warehouse_type, warehouse_channel, warehouse_size,
    min_clusters, max_clusters, auto_stop_minutes, tags, epoch_start,
    LEAD(epoch_start) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY epoch_start) AS epoch_end
  FROM config_island_rows
  WHERE rn_in_epoch = 1
)"""


CONFIG_EPOCHS_SQL = r"""
WITH __CONFIG_EPOCHS__,
active AS (
  SELECT workspace_id, warehouse_id
  FROM config_snapshots
  WHERE rn_desc = 1 AND delete_time IS NULL
)
SELECT
  h.account_id,
  h.workspace_id,
  h.warehouse_id,
  h.warehouse_name,
  h.warehouse_type,
  h.warehouse_channel,
  h.warehouse_size,
  h.min_clusters,
  h.max_clusters,
  h.auto_stop_minutes,
  h.tags,
  h.epoch_start,
  h.epoch_end,
  CASE WHEN h.epoch_end IS NULL THEN TRUE ELSE FALSE END AS is_current
FROM config_epochs h
JOIN active a ON h.workspace_id = a.workspace_id AND h.warehouse_id = a.warehouse_id
WHERE h.epoch_start >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
   OR h.epoch_end IS NULL
   OR h.epoch_end >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
ORDER BY h.workspace_id, h.warehouse_id, h.epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)
