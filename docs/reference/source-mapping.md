# Source mapping

| Need | Source | Notes |
|---|---|---|
| Current + historical warehouse config snapshots | `system.compute.warehouses` | Authoritative source for config epochs: a new epoch starts when type, channel, size, min/max clusters or auto-stop changes; name/tag-only snapshots do not start one. |
| Query latency/queue/spill/cache/tags/source | `system.query.history` | Current-epoch aggregates; raw SQL is not exported. Latency (p95/p99/max execution) and spill (share, count, p95 bytes) are measured on the decision population: queries not served from the result cache (failed and cancelled queries stay in, a timeout is latency). `decision_queries_30` counts it and is the size sample basis (missing: the size change is held, `DECISION_QUERY_EVIDENCE_UNAVAILABLE`). Queue and failure shares use all queries. Queries per complete UTC week (`queries_week_1` to `queries_week_8`, week 1 being the seven days before the analysis day; null for a week not wholly inside the current epoch) feed the growth guard on capacity cuts. |
| Compilation and result-fetch time | `system.query.history.compilation_duration_ms`, `result_fetch_duration_ms` | p95 over the decision population (`p95_compilation_ms_30`, `p95_result_fetch_ms_30`). Evidence on the size candidate only: a size change does not shorten either, and no decision reads them. |
| Task-time utilization evidence | `system.query.history.total_task_duration_ms` | Summed over the current epoch (`epoch_task_duration_ms`) and divided by the same epoch's cluster-minutes from `system.compute.warehouse_events`: query task-minutes per running cluster-minute. Evidence only; no decision uses it yet. |
| Databricks query origin | `system.query.history.query_source` | JOB, DASHBOARD/LEGACY_DASHBOARD, NOTEBOOK, ALERT, GENIE and related entity references. |
| External client source | `system.query.history.client_application` | Used for SQL_EDITOR, POWER_BI, TABLEAU, LOOKER and other client attribution. |
| Starts/scaling/cluster-minutes/time at min/max | `system.compute.warehouse_events` | Retention is 365 days: running time is known only from a warehouse's first retained event (`event_coverage_start`), and the query-history aggregates that are divided by event totals are restricted to queries after it so both cover the same period; the idle-gap query returns the busy minutes (wall-clock minutes with at least one query running) over that period, which split running time into busy and idle for the size estimate. A warehouse billed but with no retained event has no series (unavailable, not idle). Whole-epoch totals (idle/event savings ratios) plus a daily series for the current epoch over the last 120 UTC days (uptime, cluster-minutes, minutes at min/max, recycle minutes, peak excluding recycling transients, starts); event segments are split at UTC midnight. The series drives the AWS baseline, migration resource bounds and recent time at min/max/observed peak. |
| Running idle time (auto-stop) | `system.query.history` + `system.compute.warehouse_events` | Idle gaps (latest busy end before a query to that query's start, plus the trailing gap to now) intersected with `RUNNING` warehouse-event segments, current epoch, from the later of the epoch start and the first retained event to now, at millisecond precision. Two collector queries: one row per warehouse (`gaps.running_idle_window_start`, `running_idle_window_end`, `running_idle_queries`, `running_segment_count`, `observed_running_cluster_minutes`, `kept_alive_wall_minutes`, `kept_alive_gap_count`) and one row per auto-stop value with a non-zero field (`gaps.running_idle_by_target`, a sparse map keyed by minutes: removable running wall and cluster minutes, additional starts, avoided starts, added idle wall minutes; an absent value is a known zero). Missing keys mean not collected and fail closed. Kept-alive time is running time past the current auto-stop with no query running. |
| Per-minute query concurrency | `system.query.history` (`start_time`, `end_time`) | Time-weighted running plus queued queries per UTC minute, level rounded up, as `query.concurrency_histogram` (`{scope: {level: minutes}}`): `EPOCH` (every epoch in the last 365 days) and, for the current epoch, `RECENT` (last 30 days) and `BUSY_START` (per busy period that follows more than the scale-up lag of idle minutes, its highest level in its first lag minutes). A missing histogram means not collected and fails closed. Older epochs' concurrency depends on their configuration, so it only holds capacity cuts. |
| Spill by statement source | `system.query.history` (`query_source`, `statement_text`, spilled bytes) | Current epoch, last 30 days, non-result-cache queries that spilled: spilled bytes per source (saved query, dashboard, legacy dashboard, job, notebook, else a hash of the statement text; a redacted or null text counts by its statement ID, never merged). Fields `query.spill_sources_30` and `query.top_spill_source_share_pct_30` (share carried by the top `spill_concentration_top_sources`). The statement text is never exported. |
| DBU quantity / SQL warehouse attribution | `system.billing.usage` | Signed quantities preserve billing corrections. `usage_end_time` gives the per-workspace billing watermark; `ingestion_date` gives the per-workspace ingested-through date and, with `--billing-ingested-through`, pins billing queries to records ingested on or before a date. |
| Billing correction share | `system.billing.usage.record_type` | Per day, the magnitude of RETRACTION DBU and the RESTATEMENT DBU are summed beside the signed totals; `record_type` only labels these quantities and never filters rows. Over the settled trailing 365 days: retracted DBU / originally billed DBU (net + retracted - restated). It can exceed 1 when a day is corrected more than once. Package-level cost-basis quality evidence only (not referenced by candidates); null when any day in the window lacks the collected quantities. |
| Observed Photon status | `system.billing.usage.product_features.is_photon` + signed DBUs | Inferred inside current config epoch with coverage/freshness guards; emits ENABLED/DISABLED/MIXED/UNKNOWN. |
| Warehouse owner | `system.billing.usage.identity_metadata.owned_by` | Latest current-epoch owner evidence; observed owners retained. May be null when no attributable billing evidence exists. |
| Billed node type | `system.billing.usage.usage_metadata.node_type` | Non-Serverless warehouses only; driver and worker are not distinguished. Collected per epoch (`observed_node_types`) and checked against the modeled AWS topology (the size's driver instance plus `i3.2xlarge` workers): a node type outside it makes the AWS cost unavailable (`AWS_NODE_TYPE_MISMATCH`); `aws_cost_model.node_type_check` is `VERIFIED` or `UNVERIFIED`. |
| Billed SQL tier | `system.billing.usage.product_features.sql_tier` | `CLASSIC`, `PRO` or null. Collected per epoch (`observed_sql_tiers`, `billing_features.current_epoch_sql_tiers`) and shown as the `billed_sql_tiers` finding on the warehouse-type candidate. Evidence only: no decision reads it. |
| Effective DBU list price / target SKU price | `system.billing.list_prices` | Temporal baseline join (left join; usage with no price row is reported as unpriced DBU) and target-SKU price registry. |
| Pro/Classic EC2 topology | Databricks SQL warehouse sizing docs | Driver type + worker count by size. |
| AWS EC2 price | `pricing/aws_ec2_on_demand.json` | Versioned and region explicit; no hidden fallback. |

## Photon semantics

`product_features.is_photon` is workload/billing attribution, not a direct config read. The result is therefore called **observed Photon state**. If enough current-epoch signed DBU is recent and >=95% of known Photon-attributed DBU is true, state is `ENABLED`; <=5% is `DISABLED`; otherwise `MIXED`. Low feature coverage, stale billing, or insufficient DBU yields `UNKNOWN`.

This gives a system-table-only deterministic Photon *observation* (reported in `current_config.photon`) while refusing to overstate ambiguous evidence. The publisher makes no Photon recommendation: Databricks publishes no Photon on/off price for SQL warehouses.

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
