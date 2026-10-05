"""system.billing SQL: daily cost, features, watermark, attribution, SKU prices and unseen billed warehouses."""
from __future__ import annotations

from publisher.analyzer.collector.queries.config_epochs import CONFIG_EPOCHS_CTE


# Usage with no matching list_prices row, or a row without a list price (promotional or not-yet-listed SKU), is kept
# and counted as unpriced DBU:
# an inner join would silently drop it and understate cost. Its cost is unknown (null), never zero. When price rows
# overlap, each usage record takes the latest price_start_time, so it is counted once. Every record_type stays in the
# signed sums (RETRACTION rows are negative); record_type only labels the retracted and restated quantities, which feed
# the billing correction share (a quality signal, not a filter).
EPOCH_DAILY_COST_SQL = r"""
WITH __CONFIG_EPOCHS__,
epochs AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),
cost AS (
  SELECT
    u.workspace_id,
    u.usage_metadata.warehouse_id AS warehouse_id,
    u.usage_date, u.usage_end_time,
    p.currency_code,
    u.sku_name,
    CAST(u.usage_quantity AS DOUBLE) AS dbu_quantity,
    CASE WHEN p.sku_name IS NULL OR p.pricing.effective_list.default IS NULL THEN CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS unpriced_dbu_quantity,
    CASE WHEN u.record_type='RETRACTION' THEN -CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS retracted_dbu_quantity,
    CASE WHEN u.record_type='RESTATEMENT' THEN CAST(u.usage_quantity AS DOUBLE) ELSE 0.0 END AS restated_dbu_quantity,
    CAST(u.usage_quantity AS DOUBLE)*CAST(p.pricing.effective_list.default AS DOUBLE) AS list_cost
  FROM system.billing.usage u
  LEFT JOIN system.billing.list_prices p
    ON p.account_id=u.account_id AND p.sku_name=u.sku_name AND p.cloud=u.cloud
   AND u.usage_end_time>=p.price_start_time
   AND (p.price_end_time IS NULL OR u.usage_end_time<p.price_end_time)
  WHERE u.usage_metadata.warehouse_id IS NOT NULL
    AND u.billing_origin_product='SQL'
    AND u.usage_date>=DATE_SUB(CURRENT_DATE(),365) AND u.usage_date<CURRENT_DATE()
    AND (__INGESTION_CUTOFF__ IS NULL OR u.ingestion_date <= __INGESTION_CUTOFF__)
  QUALIFY ROW_NUMBER() OVER (PARTITION BY u.record_id ORDER BY p.price_start_time DESC NULLS LAST, p.currency_code, p.price_end_time) = 1
)
SELECT c.workspace_id,c.warehouse_id,e.epoch_start,c.usage_date,c.currency_code,
  SUM(c.dbu_quantity) AS dbu_quantity, SUM(c.unpriced_dbu_quantity) AS unpriced_dbu_quantity, SUM(c.list_cost) AS list_cost,
  SUM(c.retracted_dbu_quantity) AS retracted_dbu_quantity, SUM(c.restated_dbu_quantity) AS restated_dbu_quantity
FROM cost c JOIN epochs e ON c.workspace_id=e.workspace_id AND c.warehouse_id=e.warehouse_id
  AND c.usage_end_time>=e.epoch_start AND (e.epoch_end IS NULL OR c.usage_end_time<e.epoch_end)
GROUP BY c.workspace_id,c.warehouse_id,e.epoch_start,c.usage_date,c.currency_code
ORDER BY c.workspace_id,c.warehouse_id,c.usage_date
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)


BILLING_FEATURE_SQL = r"""
SELECT
  workspace_id,
  usage_metadata.warehouse_id AS warehouse_id,
  100.0*SUM(CASE WHEN product_features.is_photon=TRUE THEN CAST(usage_quantity AS DOUBLE) ELSE 0 END)
    /NULLIF(SUM(CAST(usage_quantity AS DOUBLE)),0) AS photon_usage_pct_365,
  100.0*SUM(CASE WHEN product_features.is_serverless=TRUE THEN CAST(usage_quantity AS DOUBLE) ELSE 0 END)
    /NULLIF(SUM(CAST(usage_quantity AS DOUBLE)),0) AS serverless_usage_pct_365,
  MAX(cloud) AS cloud,
  COLLECT_SET(sku_name) AS sku_names
FROM system.billing.usage
WHERE usage_metadata.warehouse_id IS NOT NULL AND billing_origin_product='SQL'
  AND usage_date>=DATE_SUB(CURRENT_DATE(),365) AND usage_date<CURRENT_DATE()
  AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
GROUP BY workspace_id, usage_metadata.warehouse_id
"""


# Billing ingestion watermark: the newest usage hour already present in system.billing.usage. Databricks documents
# ORIGINAL records as "typically available within 12 hours" (longer for new workspaces) and gives no bound for
# RETRACTION/RESTATEMENT corrections, so a calendar day is treated as covered only once the watermark has passed its
# end. That is ingestion progress, not finality: corrections can still revise a covered day. Timestamps are UTC.
BILLING_WATERMARK_SQL = r"""
SELECT workspace_id, MAX(usage_end_time) AS billing_watermark, MAX(ingestion_date) AS billing_ingested_through
FROM system.billing.usage
WHERE usage_date>=DATE_SUB(CURRENT_DATE(),30) AND billing_origin_product='SQL'
  AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
GROUP BY workspace_id
"""


EPOCH_BILLING_ATTRIBUTION_SQL = r"""
WITH __CONFIG_EPOCHS__,
epochs AS (SELECT workspace_id, warehouse_id, epoch_start, epoch_end FROM config_epochs),
usage AS (
  SELECT
    workspace_id,
    usage_metadata.warehouse_id AS warehouse_id,
    usage_end_time,
    CAST(usage_quantity AS DOUBLE) AS dbu_quantity,
    product_features.is_photon AS is_photon,
    -- A redacted or blank owner is unknown, never an owner, so it cannot win the latest-owner pick.
    CASE WHEN UPPER(TRIM(identity_metadata.owned_by)) IN ('__REDACTED__','<REDACTED>','REDACTED') OR TRIM(identity_metadata.owned_by)=''
         THEN NULL ELSE identity_metadata.owned_by END AS owned_by,
    usage_metadata.node_type AS node_type,
    product_features.sql_tier AS sql_tier
  FROM system.billing.usage
  WHERE usage_metadata.warehouse_id IS NOT NULL
    AND billing_origin_product='SQL'
    AND usage_unit='DBU'
    AND usage_date>=DATE_SUB(CURRENT_DATE(),365) AND usage_date<CURRENT_DATE()
    AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
)
SELECT
  u.workspace_id,
  u.warehouse_id,
  e.epoch_start,
  SUM(CASE WHEN u.is_photon=TRUE THEN u.dbu_quantity ELSE 0 END) AS photon_true_dbu,
  SUM(CASE WHEN u.is_photon=FALSE THEN u.dbu_quantity ELSE 0 END) AS photon_false_dbu,
  SUM(CASE WHEN u.is_photon IS NULL THEN u.dbu_quantity ELSE 0 END) AS photon_null_dbu,
  MAX(u.usage_end_time) AS latest_usage_time,
  MAX_BY(u.owned_by, CASE WHEN u.owned_by IS NOT NULL THEN u.usage_end_time END) AS latest_owner,
  ARRAY_SORT(COLLECT_SET(u.owned_by)) AS observed_owners,
  ARRAY_SORT(COLLECT_SET(u.node_type)) AS observed_node_types,
  ARRAY_SORT(COLLECT_SET(u.sql_tier)) AS observed_sql_tiers
FROM usage u
JOIN epochs e
  ON u.workspace_id=e.workspace_id
 AND u.warehouse_id=e.warehouse_id
 AND u.usage_end_time>=e.epoch_start
 AND (e.epoch_end IS NULL OR u.usage_end_time<e.epoch_end)
GROUP BY u.workspace_id,u.warehouse_id,e.epoch_start
""".replace("__CONFIG_EPOCHS__", CONFIG_EPOCHS_CTE)


SQL_SKU_PRICE_SQL = r"""
SELECT
  account_id, sku_name, cloud, currency_code, usage_unit,
  CAST(pricing.effective_list.default AS DOUBLE) AS effective_list_price
FROM system.billing.list_prices
WHERE price_end_time IS NULL
  AND usage_unit = 'DBU'
  AND UPPER(sku_name) LIKE '%SQL%'
ORDER BY account_id, sku_name
"""


# SQL warehouses billed in the last 30 days with no row in system.compute.warehouses. The table keeps 365 days and
# writes a row only when a configuration changes; whether an unchanged warehouse's only row survives the retention
# period is not documented, and such a warehouse would be invisible to the collector. Reported, never analyzed.
# Billing is account-wide but the warehouses table is regional, so only warehouses active in this region count: a
# warehouse event in the events table's retention, or a query in the last 30 days (an always-on warehouse may emit no
# event for months).
UNSEEN_BILLED_WAREHOUSES_SQL = r"""
WITH billed AS (
  SELECT workspace_id, usage_metadata.warehouse_id AS warehouse_id,
    SUM(CAST(usage_quantity AS DOUBLE)) AS billed_dbu_30, MAX(usage_date) AS last_usage_date
  FROM system.billing.usage
  WHERE usage_metadata.warehouse_id IS NOT NULL AND billing_origin_product='SQL' AND usage_unit='DBU'
    AND usage_date>=DATE_SUB(CURRENT_DATE(),30) AND usage_date<CURRENT_DATE()
    AND (__INGESTION_CUTOFF__ IS NULL OR ingestion_date <= __INGESTION_CUTOFF__)
  GROUP BY workspace_id, usage_metadata.warehouse_id
), regional AS (
  SELECT DISTINCT workspace_id, warehouse_id FROM system.compute.warehouse_events
  WHERE event_time >= CURRENT_TIMESTAMP() - INTERVAL 365 DAYS
  UNION
  SELECT DISTINCT workspace_id, compute.warehouse_id AS warehouse_id FROM system.query.history
  WHERE compute.type = 'WAREHOUSE' AND compute.warehouse_id IS NOT NULL AND start_time >= CAST(DATE_SUB(CURRENT_DATE(),30) AS TIMESTAMP)
), snapshots AS (
  SELECT DISTINCT workspace_id, warehouse_id FROM system.compute.warehouses
)
SELECT b.workspace_id, b.warehouse_id, b.billed_dbu_30, b.last_usage_date
FROM billed b
LEFT SEMI JOIN regional r ON b.workspace_id=r.workspace_id AND b.warehouse_id=r.warehouse_id
LEFT ANTI JOIN snapshots s ON b.workspace_id=s.workspace_id AND b.warehouse_id=s.warehouse_id
ORDER BY b.workspace_id, b.warehouse_id
"""
