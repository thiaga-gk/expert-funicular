# Source mapping — v2.10.0

| Need | Source | Notes |
|---|---|---|
| Current + historical warehouse config snapshots | `system.compute.warehouses` | Authoritative source for config epochs: a new epoch starts when type, channel, size, min/max clusters or auto-stop changes; name/tag-only snapshots do not start one. |
| Query latency/queue/spill/cache/tags/source | `system.query.history` | Current-epoch aggregates; raw SQL is not exported. |
| Databricks query origin | `system.query.history.query_source` | JOB, DASHBOARD/LEGACY_DASHBOARD, NOTEBOOK, ALERT, GENIE and related entity references. |
| External client source | `system.query.history.client_application` | Used for SQL_EDITOR, POWER_BI, TABLEAU, LOOKER and other client attribution. |
| Starts/scaling/cluster-minutes/time at min/max | `system.compute.warehouse_events` | Resource state and AWS annualization. |
| DBU quantity / SQL warehouse attribution | `system.billing.usage` | Signed quantities preserve billing corrections. |
| Observed Photon status | `system.billing.usage.product_features.is_photon` + signed DBUs | Inferred inside current config epoch with coverage/freshness guards; emits ENABLED/DISABLED/MIXED/UNKNOWN. |
| Warehouse owner | `system.billing.usage.identity_metadata.owned_by` | Latest current-epoch owner evidence; observed owners retained. May be null when no attributable billing evidence exists. |
| Effective DBU list price / target SKU price | `system.billing.list_prices` | Temporal baseline join (left join; usage with no price row is reported as unpriced DBU) and target-SKU price registry. |
| Pro/Classic EC2 topology | Databricks SQL warehouse sizing docs | Driver type + worker count by size. |
| AWS EC2 price | `pricing/aws_ec2_on_demand.json` | Versioned and region explicit; no hidden fallback. |

## Photon semantics

`product_features.is_photon` is workload/billing attribution, not a direct config read. v2.7.1 therefore calls the result **observed Photon state**. If enough current-epoch signed DBU is recent and >=95% of known Photon-attributed DBU is true, state is `ENABLED`; <=5% is `DISABLED`; otherwise `MIXED`. Low feature coverage, stale billing, or insufficient DBU yields `UNKNOWN`.

This gives a system-table-only deterministic Photon *observation* (reported in `current_config.photon`) while refusing to overstate ambiguous evidence. v2.7.1 makes no Photon recommendation: Databricks publishes no Photon on/off price for SQL warehouses.

## Query source semantics

Query source is intentionally multi-label. A query can have a Databricks entity source and an external client application simultaneously; percentages across categories can therefore exceed 100%. `primary_category` is a deterministic presentation convenience, not an assertion that all other categories are absent.

## Regional scale

Warehouse/config/query/event system tables are regional. Billing/pricing are account/global. Collect once per region for full account coverage, then merge normalized evidence by `(workspace_id, warehouse_id)`.

## Read-only API boundary

- `GET /api/2.0/sql/config/warehouses`: compatibility evidence for the directly connected workspace only; values are redacted/not retained.
- `GET /api/2.1/clusters/get`: validates an all-purpose execution target is not jobs compute.
- SCIM `Me` response header: resolves workspace/org ID when needed for an all-purpose SQL connection path or compatibility scope.
- These calls never supply optimizer tunable configs.

## Config evidence not optimized

Spot policy and effective warehouse-level statement timeout remain outside the active optimizer because the selected authoritative config system table does not expose them. Photon is different: it is now assessed from billing-system-table observed workload state rather than a configuration API.
