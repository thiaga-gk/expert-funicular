# Audit M-items, group B: design proposals (M1, M2, M3b, M4, M9)

Status: **DRAFT DESIGN PROPOSAL. Documentation only.** No code, contract, schema, threshold or fixture has been changed. **All SQL in this file is UNEXECUTED**: it has never run against a Databricks workspace, and some function spellings are marked ASSUMED.

Baseline read: `publisher.py` on branch `fix/v2.7-audit-m-items` (`EPOCH_QUERY_METRICS_SQL`, `EPOCH_EVENT_METRICS_SQL`, `build_epoch_gap_metrics_sql`, `extract_evidence`, `Analyzer.analyze`, `Optimizer.optimize`, `Estimator._factors`, `Estimator._matched_epoch_factor`, `StatefulBundleEstimator._apply/estimate`, `Modeler.forecast_from_daily_cost`, `model_aws_baseline_cost`). The local M3 fix (`time_at_max_pct_saturated = 5.0`, max-reduction headroom) is already in place.

Binding rules applied throughout: the analyzer observes, the optimizer decides, the candidate estimator prices isolated changes, and the bundle estimator prices stateful scenarios. Unknown is null, never zero. The current epoch controls recommendations. Identical evidence must give identical output. Public contract changes need the `databricks-contract-designer` skill plus user sign-off.

## 0. Verified Databricks facts used below

| # | Claim | Status |
|---|---|---|
| V1 | `system.compute.warehouse_events` columns: `account_id`, `workspace_id`, `warehouse_id`, `event_type` (string), `cluster_count` (integer, "The number of clusters that are actively running."), `event_time` (timestamp, "Timestamp of when the event took place in UTC."). `event_type` values: `SCALED_UP` ("A new cluster was added to the warehouse."), `SCALED_DOWN` ("A cluster was removed from the warehouse."), `STOPPING` ("The warehouse is in the process of stopping."), `RUNNING` ("The warehouse is actively running."), `STARTING` ("The warehouse is in the process of starting up."), `STOPPED` ("The warehouse has completely stopped running."). | VERIFIED: https://docs.databricks.com/aws/en/admin/system-tables/warehouse-events |
| V2 | warehouse_events, warehouses and query history: 365-day retention, **regional**, GA. Billable usage: 365 days, **global**. "No support for real-time monitoring. Data is updated throughout the day." | VERIFIED: https://docs.databricks.com/aws/en/admin/system-tables/ |
| V3 | `system.query.history`: `start_time` = "The time when Databricks received the request" (UTC offset recorded). `end_time` = "The time the statement execution ended, excluding result fetch time". `waiting_for_compute_duration_ms` = "Time spent waiting for compute resources to be provisioned". `waiting_at_capacity_duration_ms` = "Time spent waiting in queue for available compute capacity". `compilation_duration_ms`, `execution_duration_ms`, `total_duration_ms` (excludes result fetch), `statement_type`, `read_bytes`, `read_rows`, `from_result_cache`, `client_application`, `query_source` (struct), `compute` (struct with type and `warehouse_id`). `execution_status` ∈ {`FINISHED`,`FAILED`,`CANCELED`}. | VERIFIED: https://docs.databricks.com/aws/en/admin/system-tables/query-history |
| V4 | "During a cluster recycle, this count might temporarily exceed the configured maximum." | VERIFIED: https://docs.databricks.com/aws/en/compute/sql-warehouse/monitor |
| V5 | Classic/Pro: "If a query waits in the queue for 5 minutes, the warehouse scales up." "If load remains low for 15 consecutive minutes, the warehouse scales down to the minimum needed to handle the peak load from that period." Serverless IWM queues a query when capacity is unavailable and provisions more clusters when wait times rise. | VERIFIED: https://docs.databricks.com/aws/en/compute/sql-warehouse/warehouse-behavior |
| V6 | Warehouse "re-provisioning, which happens approximately every 24 hours". This is the Azure page; the same cadence on AWS is **ASSUMED**. | VERIFIED (Azure only): https://learn.microsoft.com/en-us/azure/databricks/compute/sql-warehouse/warehouse-behavior |
| V7 | `auto_stop_mins`: "The amount of time in minutes that a SQL warehouse must be idle (i.e., no RUNNING queries) before it is automatically stopped." It must be 0 or ≥10, and **0 disables auto-stop**. `max_num_clusters` ≤ 40. `min_num_clusters` ≤ min(max, 30). | VERIFIED: https://docs.databricks.com/api/workspace/warehouses/create |
| V8 | Serverless auto-stop: UI minimum 5 minutes, and "via API … as low as 1 minute". Pro/Classic: default 45, minimum 10. This conflicts with V7's "0 or ≥10", so the policy must treat sub-10 Serverless values as valid observed configs. | VERIFIED: https://docs.databricks.com/aws/en/compute/sql-warehouse/create |
| V9 | `system.billing.usage`: `usage_start_time`/`usage_end_time` are UTC. RETRACTION/RESTATEMENT corrections exist. ORIGINAL records are "typically available within 12 hours". **Record granularity is not stated**; hourly is ASSUMED. | VERIFIED: https://docs.databricks.com/aws/en/admin/system-tables/billing |

Behaviour the docs do not state, so it stays **ASSUMED** until real data confirms it:
- A1: `cluster_count` during `STARTING`/`STOPPING`/`STOPPED`.
- A2: whether DBU billing starts at `STARTING` or at `RUNNING`.
- A3: whether two events can share an `event_time`. This makes `LEAD` ordering ambiguous, and the existing `EPOCH_EVENT_METRICS_SQL` has the same exposure.
- A4: where in a query's lifetime the capacity-queue wait sits (before or after compilation).
- A5: how long a recycle transient lasts.
- A6: per-region event-delivery latency.

Defects found in the current collectors while reading. These are inputs to M1/M2, not separate items:

| Defect | Where | Effect |
|---|---|---|
| D1 | `EPOCH_EVENT_METRICS_SQL` `LEAD(event_time) ORDER BY event_time` has no tie-breaker | Under A3, which segment carries which `cluster_count` is nondeterministic. |
| D2 | Events are filtered to `event_time >= now-365d` with no seed state | The state before the first in-window event is unknown, yet the time is silently treated as not-running. |
| D3 | The open last segment runs to `CURRENT_TIMESTAMP()` | Event latency (V2) can extend a stale `RUNNING` state, and the bound is not `analysis_time`. |
| D4 | The gap SQL uses `MAX(end_time)`, which ignores NULL `end_time` | A long query still running (or with no `end_time`) does not cover its busy time, which creates false gaps. |
| D5 | The gap SQL drops the trailing gap after an epoch's last query | Usually minor; it understates removable idle. |
| D6 | All collectors use `CURRENT_TIMESTAMP()`, while the evidence `analysis_time` is taken in Python | Windows differ by seconds from the recorded `analysis_time`. The collector should render one literal window end. |

---

## M1. Auto-stop savings must intersect idle gaps with observed RUNNING intervals

### 1. Problem
`build_epoch_gap_metrics_sql` credits `GREATEST(LEAST(gap, auto_stop or gap) - T, 0)` minutes per inter-query gap. It assumes the warehouse ran for `min(gap, auto_stop)` minutes after each query. That is false when the warehouse was stopped manually, by a scheduler/API, or by its own timeout before the next query. With `auto_stop = 0` the entire gap is credited. `Estimator._factors` then multiplies by `current_min` clusters, regardless of how many clusters actually ran.

**Worked example.** PRO, `auto_stop=0`, `min=max=1`. Queries run 08:00–18:00. An ops job stops the warehouse at 19:00, and it restarts on the first query at 08:00. The policy target is 30 minutes. Each workday has one 840-minute gap (18:00 to 08:00).

| | Today (v2.7.1) | Proposed |
|---|---|---|
| Removable minutes per gap | `840 − 30 = 810` | `[18:30, 08:00) ∩ RUNNING[18:00, 19:00)` = **30** |
| Over 20 workdays | 16,200 | 600 |
| Running cluster-minutes (08:00–19:00 × 20) | 13,200 | 13,200 |
| `raw` fraction | `16,200/13,200` → capped at **0.60** | `600/13,200` = **0.045** |
| Base DBU savings on a $50,000 baseline (realization 0.75) | **$22,500** | **$1,705** |

### 2. Evidence and authority

| Evidence | Source | Authority |
|---|---|---|
| Current `auto_stop_minutes`, `min/max_clusters` | `system.compute.warehouses` | AUTHORITATIVE |
| RUNNING segments: `cluster_count > 0` between consecutive events | `system.compute.warehouse_events` | OBSERVED (A1 ASSUMED) |
| Busy intervals `[start_time, COALESCE(end_time, start_time + total_duration_ms))` | `system.query.history` | OBSERVED |
| Removable running wall- and cluster-minutes per target T | idle gaps ∩ RUNNING segments | DERIVED |
| Additional restarts per target T | gaps that were running continuously from `gap_start+T` to the next query | DERIVED |
| Billed hours vs hours with any RUNNING minute | `system.billing.usage` × events | DERIVED cross-check (billing DBU quantity is AUTHORITATIVE usage; granularity ASSUMED hourly, V9) |
| Event watermark `MAX(event_time)` in region | `system.compute.warehouse_events` | OBSERVED |

### 3. SQL (UNEXECUTED)
The collector renders `{window_end}` as a `TIMESTAMP'YYYY-MM-DD 00:00:00'` literal: the UTC midnight of the `analysis_time` date, exclusive (fixes D6). It renders `{lookback_days}` and `{targets}` from `Thresholds`, the same way `build_epoch_gap_metrics_sql` renders targets today. Phase 1 uses `lookback_days = 365`, which equals today's epoch window. M2 then switches it to 84. Every interval is half-open `[start, end)`, all times are UTC, and each warehouse is joined to its current epoch exactly as the existing collectors do.

```sql
-- UNEXECUTED DRAFT (M1 running-idle collector). Read-only.
WITH epochs AS (
  SELECT workspace_id, warehouse_id, auto_stop_minutes, min_clusters, max_clusters,
         change_time AS epoch_start,
         LEAD(change_time) OVER (PARTITION BY workspace_id, warehouse_id ORDER BY change_time) AS epoch_end
  FROM system.compute.warehouses
), cur AS (                                   -- current epoch only, clipped to the bounded window
  SELECT workspace_id, warehouse_id, epoch_start, auto_stop_minutes,
         GREATEST(epoch_start, {window_end} - INTERVAL {lookback_days} DAYS) AS w_start,
         {window_end} AS w_end
  FROM epochs WHERE epoch_end IS NULL AND epoch_start < {window_end}
), ev AS (                                    -- D1/D2: deterministic tie-break + 7-day seed before w_start
  SELECT workspace_id, warehouse_id, event_type, cluster_count, event_time,
         LEAD(event_time) OVER (PARTITION BY workspace_id, warehouse_id
                                ORDER BY event_time, cluster_count, event_type) AS next_event_time
  FROM system.compute.warehouse_events
  WHERE event_time >= {window_end} - INTERVAL {lookback_days_plus_seed} DAYS
    AND event_time <  {window_end}
), running AS (                               -- RUNNING segments clipped to [w_start, w_end)
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start,
         GREATEST(v.event_time, c.w_start)                     AS seg_start,
         LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end)  AS seg_end,  -- D3: bounded by w_end, not now()
         v.cluster_count
  FROM ev v JOIN cur c ON v.workspace_id = c.workspace_id AND v.warehouse_id = c.warehouse_id
  WHERE COALESCE(v.cluster_count, 0) > 0
    AND v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
), q AS (                                     -- D4: NULL end_time falls back to start + total_duration
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.w_start, c.w_end, x.start_time,
         COALESCE(x.end_time, TIMESTAMPADD(MILLISECOND, COALESCE(x.total_duration_ms, 0), x.start_time)) AS busy_end
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id
  WHERE x.compute.type = 'WAREHOUSE'
    AND x.start_time >= GREATEST(c.epoch_start, c.w_start - INTERVAL 1 DAY)  -- seed the busy frontier
    AND x.start_time <  c.w_end
), ordered AS (                               -- overlapping queries: running max of prior busy_end
  SELECT *, MAX(busy_end) OVER (PARTITION BY workspace_id, warehouse_id
                                ORDER BY start_time, busy_end
                                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_busy_end
  FROM q
), gaps AS (
  SELECT workspace_id, warehouse_id, epoch_start, w_start, w_end,
         prev_busy_end AS gap_start, start_time AS gap_end, TRUE AS has_next_query
  FROM ordered WHERE prev_busy_end IS NOT NULL AND start_time > prev_busy_end
  UNION ALL                                   -- D5: trailing gap after the last busy end
  SELECT workspace_id, warehouse_id, epoch_start, MAX(w_start), MAX(w_end), MAX(busy_end), MAX(w_end), FALSE
  FROM q GROUP BY workspace_id, warehouse_id, epoch_start HAVING MAX(busy_end) < MAX(w_end)
), removable AS (                             -- under target T the warehouse would stop at gap_start + T
  SELECT g.*, t.target_min,
         GREATEST(TIMESTAMPADD(MINUTE, t.target_min, g.gap_start), g.w_start) AS r_start,
         LEAST(g.gap_end, g.w_end) AS r_end
  FROM gaps g CROSS JOIN (VALUES {targets}) AS t(target_min)
), overlap AS (
  SELECT r.workspace_id, r.warehouse_id, r.epoch_start, r.target_min, r.gap_start, r.has_next_query,
         TIMESTAMPDIFF(SECOND, r.r_start, r.r_end) / 60.0 AS window_min,
         SUM(GREATEST(0, TIMESTAMPDIFF(SECOND, GREATEST(r.r_start, s.seg_start), LEAST(r.r_end, s.seg_end)))) / 60.0 AS wall_min,
         SUM(GREATEST(0, TIMESTAMPDIFF(SECOND, GREATEST(r.r_start, s.seg_start), LEAST(r.r_end, s.seg_end))) * s.cluster_count) / 60.0 AS cluster_min
  FROM removable r
  LEFT JOIN running s ON s.workspace_id = r.workspace_id AND s.warehouse_id = r.warehouse_id
                     AND s.seg_start < r.r_end AND s.seg_end > r.r_start
  WHERE r.r_end > r.r_start
  GROUP BY r.workspace_id, r.warehouse_id, r.epoch_start, r.target_min, r.gap_start, r.has_next_query, r.r_start, r.r_end
)
SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.w_start AS running_idle_window_start, c.w_end AS running_idle_window_end,
  (SELECT COUNT(*) FROM running s WHERE s.workspace_id = c.workspace_id AND s.warehouse_id = c.warehouse_id) AS running_segment_count,
  (SELECT SUM(TIMESTAMPDIFF(SECOND, seg_start, seg_end) * cluster_count) / 60.0 FROM running s
     WHERE s.workspace_id = c.workspace_id AND s.warehouse_id = c.warehouse_id) AS observed_running_cluster_minutes,
  -- one triple per target, rendered from Thresholds (example T = 30):
  COALESCE(SUM(CASE WHEN o.target_min = 30 THEN o.wall_min END), 0)    AS removable_running_wall_minutes_if_30,
  COALESCE(SUM(CASE WHEN o.target_min = 30 THEN o.cluster_min END), 0) AS removable_running_cluster_minutes_if_30,
  COUNT_IF(o.target_min = 30 AND o.has_next_query AND o.wall_min >= 0.99 * o.window_min) AS additional_starts_if_30
FROM cur c LEFT JOIN overlap o ON o.workspace_id = c.workspace_id AND o.warehouse_id = c.warehouse_id
GROUP BY c.workspace_id, c.warehouse_id, c.epoch_start, c.w_start, c.w_end
```

Billed-minutes cross-check (UNEXECUTED). Billing is global and events are regional (V2), so only warehouses present in the regional `cur` set are kept. Hours after the billing watermark are excluded (M7 watermark already exists).

```sql
-- UNEXECUTED DRAFT (M1 billing/event coverage). Reuses cur/running CTEs above.
, billed AS (
  SELECT u.workspace_id, u.usage_metadata.warehouse_id AS warehouse_id, DATE_TRUNC('HOUR', u.usage_start_time) AS hr
  FROM system.billing.usage u
  JOIN cur c ON u.workspace_id = c.workspace_id AND u.usage_metadata.warehouse_id = c.warehouse_id
  WHERE u.billing_origin_product = 'SQL'
    AND u.usage_start_time >= c.w_start AND u.usage_start_time < LEAST(c.w_end, {billing_watermark})
  GROUP BY 1, 2, 3
  HAVING SUM(CAST(u.usage_quantity AS DOUBLE)) > 0          -- signed: retractions net out
), run_hours AS (
  SELECT DISTINCT workspace_id, warehouse_id, h AS hr
  FROM running LATERAL VIEW explode(sequence(DATE_TRUNC('HOUR', seg_start),
                                             DATE_TRUNC('HOUR', TIMESTAMPADD(SECOND, -1, seg_end)),
                                             INTERVAL 1 HOUR)) t AS h
)
SELECT b.workspace_id, b.warehouse_id,
  COUNT(*) AS billed_hours,
  COUNT_IF(r.hr IS NULL) AS billed_hours_without_running_events
FROM billed b LEFT JOIN run_hours r USING (workspace_id, warehouse_id, hr)
GROUP BY b.workspace_id, b.warehouse_id
-- plus the symmetric count running_hours_without_billing (run_hours LEFT JOIN billed), restricted to hours < watermark
```

The interval join is O(gaps × segments) per warehouse. A Databricks range-join hint may help (ASSUMED to be available on SQL warehouses). Phase 2 (M2) bounds the window to 84 days.

### 4. New evidence fields (current epoch, keys added to `EpochEvidence.gaps` / `.events`)

| Field | Type | Null semantics |
|---|---|---|
| `running_idle_window_start`, `running_idle_window_end` | ISO string | null means not collected |
| `running_segment_count` | int | null = not collected; 0 = collected, warehouse never ran in window (known zero) |
| `observed_running_cluster_minutes` | float | null = not collected |
| `removable_running_wall_minutes_if_{T}` | float | null = not collected; 0 = known none |
| `removable_running_cluster_minutes_if_{T}` | float | as above |
| `additional_starts_if_{T}` | int | as above |
| `billed_hours`, `billed_hours_without_running_events`, `running_hours_without_billing` | int | null = cross-check unavailable (e.g. no watermark) |
| `events_watermark` | ISO string | null = unknown event freshness |

Backward compatibility: these are keys inside existing dicts, so `_epoch_from_dict` loads old evidence unchanged, and an absent key reads as `None`. The analyzer must use the `conditional_severity` idiom here (absent means unknown, never 0). Old evidence therefore **fails closed**: the auto-stop decision becomes `INSUFFICIENT_EVIDENCE` with the blocker `RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE`, and the estimate is null. The legacy `saved_idle_minutes_if_{T}` is kept as an analyzer finding (labelled "upper bound, not RUNNING-intersected") but stops feeding the estimator.

### 5. Where it plugs in

| Layer | Function | Change |
|---|---|---|
| Collector | new `build_running_idle_sql(t, window_end)` + coverage SQL; `extract_evidence` adds two `client.query` calls and merges rows by `_row_key` | Bounded SQL. No decision logic. |
| Analyzer | `Analyzer.analyze`, auto-stop section | Adds facts `removable_running_wall`, `removable_running_cluster`, `observed_running_cluster_minutes`, `running_evidence_status ∈ {AVAILABLE, UNAVAILABLE, INCONSISTENT}`, `billing_event_mismatch_pct`, `additional_starts_per_active_day`, and findings with source/window. |
| Optimizer | `Optimizer.optimize`, auto-stop branch | Missing evidence → `INSUFFICIENT_EVIDENCE` with `RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE`. Mismatch above tolerance → `INSUFFICIENT_EVIDENCE` with `RUNNING_INTERVAL_BILLING_MISMATCH`. `removable_running_cluster / observed_running_cluster_minutes < auto_stop_min_removable_pct` → `NO_CHANGE` with reason `NO_MATERIAL_RUNNING_IDLE`. Otherwise today's rules apply. |
| Candidate estimator | `Estimator._factors`, `auto_stop_minutes` branch | `raw = removable_running_cluster_if_T / observed_running_cluster_minutes` (same window, already cluster-weighted, so the `× current_min` goes away), capped by `auto_stop_savings_cap`. |
| Bundle estimator | `StatefulBundleEstimator._apply`: **no code change** | The effect stays `idle_cluster_minute_reduction` with `idle_minutes = removable_running_wall_if_T` and `max_removable_cluster_minutes = min(cap × basis, removable_running_cluster_if_T)`. After a min-cluster step, `idle_minutes × st.current_min` falls as it does today. **Constraint:** `estimate()` seeds `original_resource` from `events.cluster_minutes`, which must be the same window as the M1 numerator. In Phase 1 (365-day lookback = epoch window) they match. In Phase 2 both move together under M2. |

### 6. Thresholds (all judgment calls)

| Constant | Default | Rationale |
|---|---|---|
| `running_idle_lookback_days` | 365 (Phase 1), then 84 (M2) | Matches the existing window first and changes one thing at a time. |
| `event_state_seed_days` | 7 | Seeds the state at `w_start`. A warehouse with no event in 7 days before the window is stopped or idle (ASSUMED). |
| `running_billing_mismatch_pct_max` | 5.0 (% of billed hours with no RUNNING event) | Tolerates hour-boundary and ASSUMED-granularity noise. |
| `auto_stop_min_removable_pct` | 2.0 | Below this the change is not worth a HITL action. |
| `max_event_watermark_lag_hours` | 24 | Older events → `INSUFFICIENT_EVIDENCE`, `EVENT_EVIDENCE_STALE`. |
| (observation only) `additional_starts_per_active_day` | no gate in Phase 1 | Shown next to the cold-start guard. Promote it to a blocker only after real data. |

### 7. Contract / schema / workbook impact
**Schema: NONE.** New blocker codes match `^[A-Z][A-Z0-9_]*$`, and new findings are free-form `EvidenceRecord` metrics. Package *values* and evidence IDs change deterministically, so bump `ANALYZER_VERSION`, `OPTIMIZER_VERSION`, `ESTIMATOR_VERSION` and `POLICY_VERSION`. Workbook: NONE.

### 8. Tests and fixtures
- Pure-Python reference implementation of the interval intersection, used as a test oracle for synthetic segments and gaps. Cases: manual stop mid-gap; `auto_stop=0` never stopped (removable = full window); `auto_stop=0` stopped nightly (the worked example: 600, not 16,200); gap fully stopped (0, not null); overlapping queries (no gap); NULL `end_time`; trailing gap; cluster_count 2 during the idle tail (cluster-weighted minutes = 2× wall).
- Optimizer: evidence missing the new keys → `INSUFFICIENT_EVIDENCE` with `RUNNING_INTERVAL_EVIDENCE_UNAVAILABLE` and a null estimate. Mismatch 6% → `INSUFFICIENT_EVIDENCE`. Removable share 1.9% → `NO_CHANGE`.
- SQL text tests (as in `test_publisher.py` around line 690): every target is rendered, the window end is a literal (no `CURRENT_TIMESTAMP`), and `LEAD` carries a tie-breaker.
- Goldens: `idle-expensive-warehouse`, `concurrency-constrained`, `spill-constrained`, `photon-disabled`, `aws-region-missing` need the new keys added, or their auto-stop expectation flips to `INSUFFICIENT_EVIDENCE`. Add a new golden `auto-stop-zero-manually-stopped` with the before/after above: base savings $22,500 → $1,705, status still `ACTIONABLE`.
- Bundle: a scenario with a min-cluster step followed by auto-stop. Assert no double counting and that `original_resource` equals the M1 window basis.

### 9. Risks / unknowns (only real data answers these)
- A1/A2: if `STARTING` is billed but reports `cluster_count = 0`, removable time is slightly understated. The billing cross-check shows this.
- How often manual or scheduled stops actually happen, which sizes the real-world impact.
- Cost of the range join on 365 days for busy warehouses (gap counts can reach the hundreds of thousands).
- Hourly billing granularity (V9 does not state it).
- Whether metadata-only statements keep a warehouse "busy" for auto-stop purposes. V7 defines idle as "no RUNNING queries".

### 10. Priority: **P1**. Largest known overstatement (worked example: about 13×). It affects an `ACTIONABLE` (non-canary) decision.

---

## M2. Window alignment (decision, resource and economics windows)

### 1. Problem
Each consumer reads a different window:

| Consumer | Window today |
|---|---|
| Query-pressure decision facts (`*_30`) | last 30 days ∩ current epoch |
| `time_at_min/max_pct`, `cluster_minutes`, `uptime`, gaps | whole current epoch (up to 365 days) |
| DBU forecast (`forecast_from_daily_cost`) | 0.65·mean28 + 0.35·mean84, × trend |
| AWS baseline (`model_aws_baseline_cost`) and migration resource bounds (`_annualized_resource_bounds`) | whole-epoch cluster-minutes/day × 365 × the same trend factor |

**Worked example.** The current epoch is 300 days old. Cluster-minutes ran at 600/day for the first 216 days and 150/day for the last 84 days, after a team moved off the warehouse. The illustrative AWS rate is $10 per cluster-hour.

| | Today | Aligned |
|---|---|---|
| DBU forecast basis | ≈150/day (recent-weighted) | ≈150/day |
| AWS annual cluster-minutes | `(216·600 + 84·150)/300 × 365` = 173,010 | `(0.65·150 + 0.35·150) × 365` = 54,750 |
| AWS baseline | **$28,835** | **$9,125** (3.2× lower) |
| `time_at_min_pct` used by min-cluster rule | dominated by the old regime | last 30 days |

DBU and AWS describe two different years, so total savings and the DBU/AWS split are both wrong.

### 2. Evidence and authority

| Evidence | Source | Authority |
|---|---|---|
| Daily `uptime_minutes`, `cluster_minutes`, `minutes_at_min`, `minutes_at_max`, `recycle_minutes` (cluster_count > max), `max_cluster_count`, `starts`, for the current epoch over the last 84 complete UTC days | `system.compute.warehouse_events` | OBSERVED |
| Daily removable running idle per target (M1) | events × query history | DERIVED |
| Window coverage `window_days = min(84, complete epoch days)` | config epoch | AUTHORITATIVE (config) / DERIVED |
| Regime ratio `rate_28 / rate_prev56` for queries/day and cluster-minutes/day | daily series | DERIVED |

### 3. SQL (UNEXECUTED)
One daily series replaces the many fixed-window scalars. Python re-anchors every window on the **same** zero-filled calendar and the **same** billing `basis_end` as `forecast_from_daily_cost`, so DBU and AWS share one basis. Overlap that crosses midnight is split across days.

```sql
-- UNEXECUTED DRAFT (M2 daily resource series, current epoch, last 84 complete UTC days).
WITH epochs AS ( /* as in M1 */ ),
cur AS (
  SELECT workspace_id, warehouse_id, epoch_start, min_clusters, max_clusters,
         GREATEST(epoch_start, {window_end} - INTERVAL 84 DAYS) AS w_start, {window_end} AS w_end
  FROM epochs WHERE epoch_end IS NULL AND epoch_start < {window_end}
),
ev AS ( /* as in M1: tie-broken LEAD, 7-day seed */ ),
seg AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, c.min_clusters, c.max_clusters, v.event_type, v.event_time,
         GREATEST(v.event_time, c.w_start) AS seg_start,
         LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end) AS seg_end,
         COALESCE(v.cluster_count, 0) AS cluster_count
  FROM ev v JOIN cur c USING (workspace_id, warehouse_id)
  WHERE v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
),
days AS (
  SELECT c.workspace_id, c.warehouse_id, d AS day_start, TIMESTAMPADD(DAY, 1, d) AS day_end
  FROM cur c LATERAL VIEW explode(sequence(CAST(DATE(c.w_start) AS TIMESTAMP),
                                           TIMESTAMPADD(DAY, -1, c.w_end), INTERVAL 1 DAY)) t AS d
),
piece AS (
  SELECT d.workspace_id, d.warehouse_id, s.epoch_start, s.min_clusters, s.max_clusters, s.cluster_count,
         DATE(d.day_start) AS usage_day,
         GREATEST(0, TIMESTAMPDIFF(SECOND, GREATEST(s.seg_start, d.day_start), LEAST(s.seg_end, d.day_end))) / 60.0 AS m
  FROM days d JOIN seg s ON s.workspace_id = d.workspace_id AND s.warehouse_id = d.warehouse_id
                        AND s.seg_start < d.day_end AND s.seg_end > d.day_start
)
SELECT workspace_id, warehouse_id, epoch_start, usage_day,
  SUM(CASE WHEN cluster_count > 0 THEN m END)                       AS uptime_minutes,
  SUM(CASE WHEN cluster_count > 0 THEN m * cluster_count END)       AS cluster_minutes,
  SUM(CASE WHEN cluster_count > 0 AND cluster_count = min_clusters THEN m END) AS minutes_at_min,
  SUM(CASE WHEN cluster_count > 0 AND cluster_count = max_clusters THEN m END) AS minutes_at_max,
  SUM(CASE WHEN cluster_count > max_clusters THEN m END)            AS recycle_minutes,
  MAX(cluster_count)                                                AS max_cluster_count
FROM piece GROUP BY workspace_id, warehouse_id, epoch_start, usage_day
-- starts per day: COUNT_IF(event_type = 'STARTING') grouped by DATE(event_time) within [w_start, w_end), joined in.
-- M1 removable idle per day: the M1 overlap CTE grouped additionally by DATE(overlap start); midnight split as above.
```

### 4. New evidence fields
- `EpochEvidence.resource_daily: Optional[List[Dict]] = None` (current epoch only). Rows: `{usage_day, uptime_minutes, cluster_minutes, minutes_at_min, minutes_at_max, recycle_minutes, max_cluster_count, starts, removable_running_cluster_minutes_if_{T}, removable_running_wall_minutes_if_{T}}`.
- `events["resource_daily_window_start" | "resource_daily_window_end"]`: ISO strings.
- Null semantics: `None` = not collected (old evidence). `[]` = collected, no rows (no activity, a known zero). **`_epoch_from_dict` must use `x.get("resource_daily")` without `or []`**, so it does not collapse null into a known zero. Missing day rows inside the window are zero-filled by Python, exactly as the DBU forecast does.
- Size: ≤84 rows × about 14 numbers ≈ 1,200 values (≈15–25 KB pretty JSON) per warehouse. That is smaller than the existing `daily_cost_365` (365 rows). Historical epochs get no series (calibration only), except the M9 case of the immediately prior epoch.
- Old evidence fails closed. Window-dependent decision facts (`time_at_*_pct_30`) are unknown, so min-cluster reduction and max-cluster reduce/increase become `INSUFFICIENT_EVIDENCE` with `RECENT_RESOURCE_WINDOW_UNAVAILABLE`. The AWS baseline and migration resource bounds become **null** with blocker `RECENT_RESOURCE_WINDOW_UNAVAILABLE`, not the whole-epoch rate.

### 5. Where it plugs in

| Layer | Function | Change |
|---|---|---|
| Collector | new `build_resource_daily_sql`; `extract_evidence` attaches rows to the current `EpochEvidence` | — |
| Modeler | new pure helper `windowed_resource_run_rate(series, basis_start, basis_end, t)` that reuses `forecast_recent_days`, `forecast_long_days`, `forecast_weight_28_*` and the same trend factor | **Same branch structure as `forecast_from_daily_cost`** (≥84 days blend; 28–83 mid blend; <28 epoch mean) |
| Modeler | `model_aws_baseline_cost`, `Estimator._annualized_resource_bounds` | Consume the windowed run-rate instead of `cluster_minutes / days × 365 × trend` |
| Analyzer | `Analyzer.analyze`: min/max/auto-stop sections | Facts `time_at_min_pct_30`, `time_at_max_pct_30`, `max_observed_clusters_30` (excluding `recycle_minutes`) and the M1 removable idle over 84 days. Findings carry `window` = "last N complete days within current epoch (N=…)". Regime finding (observation only). |
| Optimizer | min/max branches | Read the `_30` facts instead of whole-epoch facts. Thresholds unchanged. |
| Estimator / bundle | `_factors` (min-cluster, auto-stop, max-cluster uplift); `StatefulBundleEstimator.estimate` `original_resource` | All use the **84-day** cluster-minute basis consistently (numerator and denominator in the same window). |

Shorter recent window than nominal: windows are clipped to the epoch start. `window_days` is recorded and shown in the finding's `window` text. The epoch-maturity gate (7 days) and sample gates are unchanged. A 20-day-old epoch uses 20 days for the "30-day" facts and the `<28` branch for run-rates. This mirrors the DBU forecast, so the two cannot disagree.

Regime detection (Phase 2, **observation only**): `regime_ratio = mean(last 28 days) / mean(previous 56 days)` for queries/day and cluster-minutes/day, when there are at least 84 complete days. Outside `[0.67, 1.5]` the analyzer emits the finding `WORKLOAD_REGIME_SHIFT_OBSERVED`, which feeds confidence only, not decisions. It may be promoted to a blocker after real data.

### 6. Thresholds (judgment calls)
Reuse `forecast_recent_days=28`, `forecast_long_days=84` and `forecast_weight_28_*` (no new economics constants). New constants:
- `pressure_window_days=30`: formalizes today's literal 30.
- `regime_ratio_low=0.67`, `regime_ratio_high=1.5`, `regime_min_days=84`.

### 7. Contract impact
**Schema: NONE.** Evidence JSON is the internal replay format, not the public contract. Values change, so bump `MODEL_VERSION`, `ANALYZER_VERSION` and `ESTIMATOR_VERSION`. Workbook: NONE. If the regime finding is later meant to surface as a typed field rather than a finding, that would need the contract designer.

### 8. Tests and fixtures
- Run-rate helper: a series of 216 days × 600 then 84 days × 150 gives 54,750 annual cluster-minutes (not 173,010). Branches at 27, 28, 83 and 84 days are identical to the DBU forecast branches. Missing days are zero-filled. `basis_end` trimmed by the watermark is honored.
- Midnight-split test for a segment spanning 23:30–00:30.
- Recycle exclusion: `cluster_count = max+1` minutes counted in `recycle_minutes` and excluded from `max_observed_clusters_30`.
- Old evidence (no `resource_daily`) → AWS baseline null with the blocker, and min/max decisions `INSUFFICIENT_EVIDENCE`. All 10 goldens need a synthetic `resource_daily` added, or their expectations change. A `tools/` helper should generate the series deterministically from existing totals for fixtures only.
- Determinism: the same evidence with different list order of `resource_daily` gives an identical package (sort by `usage_day`).

### 9. Risks / unknowns
- Recent windows are noisier. Sample gates exist for queries but not for resource time, so a minimum of `uptime_minutes_84` may be needed (real data).
- With heavy seasonality (month-end), 84 days can still mislead. The regime finding only exposes this.
- The daily series doubles events-table scans unless M1 and M2 share one collector pass (recommended).

### 10. Priority: **P1**. Ship together with M1, since both share the window parameter, collector pass and `original_resource` basis.

---

## M3b. Temporal correlation of queue pressure with time at the max-cluster ceiling

### 1. Problem
After the local M3 fix, an increase needs `queued_query_pct_30 ≥ 5`, `p95 capacity wait ≥ 30 s` and `time_at_max_pct ≥ 5` (whole epoch). These are three separate aggregates. Queueing may happen while the warehouse is **below** max, which is autoscaler lag: Classic/Pro scale up only after a 5-minute queue wait (V5). Recycle transients (V4, V6) also distort `cluster_count`.

**Worked example.** `max=2`, `min=1`, 30 days. 10,000 queries, 600 queued (6%), p95 wait 40 s. Running 18,000 min, 1,440 min at max (8%). Total queued wait 300 min: 270 min while `cluster_count=1` (morning ramp-up), 30 min while at 2.

| | Today | Proposed |
|---|---|---|
| Increase rule | all three thresholds pass → **ACTIONABLE 2→3**, base cost uplift ≈ `0.5 × 1,440/19,440` ≈ 3.7% of baseline | wait-at-max share = 30/300 = **10% < 50%** → `NO_CHANGE`, reason `QUEUE_NOT_CORRELATED_WITH_CEILING` (the queueing is scale-up lag) |
| Same warehouse, with 210 of 300 min at max | ACTIONABLE | share 70%, 420 queries ≥ 20 → **ACTIONABLE** (unchanged) |

### 2. Evidence and authority

| Evidence | Source | Authority |
|---|---|---|
| `waiting_at_capacity_duration_ms`, `waiting_for_compute_duration_ms`, `start_time` per query | `system.query.history` | OBSERVED (V3) |
| Wait interval `[start + wfc, start + wfc + wac)` | derived | DERIVED, with position **ASSUMED** (A4) |
| At-max segments `cluster_count = max_clusters`, excluding recycle transients | `system.compute.warehouse_events` + config | DERIVED |
| `max_clusters` | `system.compute.warehouses` | AUTHORITATIVE |

### 3. SQL (UNEXECUTED)

```sql
-- UNEXECUTED DRAFT (M3b queue-at-ceiling correlation; last 30 days within current epoch).
WITH epochs AS ( /* as in M1, incl. max_clusters */ ),
cur AS (
  SELECT workspace_id, warehouse_id, epoch_start, max_clusters,
         GREATEST(epoch_start, {window_end} - INTERVAL 30 DAYS) AS w_start, {window_end} AS w_end
  FROM epochs WHERE epoch_end IS NULL AND epoch_start < {window_end}
),
ev AS (
  SELECT workspace_id, warehouse_id, cluster_count, event_time,
         LEAD(event_time)   OVER w AS next_event_time,
         LAG(cluster_count) OVER w AS prev_count,
         LEAD(cluster_count) OVER w AS next_count
  FROM system.compute.warehouse_events
  WHERE event_time >= {window_end} - INTERVAL 37 DAYS AND event_time < {window_end}
  WINDOW w AS (PARTITION BY workspace_id, warehouse_id ORDER BY event_time, cluster_count, event_type)
),
at_max AS (                                   -- exclude recycle transients
  SELECT c.workspace_id, c.warehouse_id,
         GREATEST(v.event_time, c.w_start) AS seg_start,
         LEAST(COALESCE(v.next_event_time, c.w_end), c.w_end) AS seg_end
  FROM ev v JOIN cur c USING (workspace_id, warehouse_id)
  WHERE v.cluster_count = c.max_clusters
    AND NOT ((COALESCE(v.prev_count, 0) > c.max_clusters OR COALESCE(v.next_count, 0) > c.max_clusters)
             AND TIMESTAMPDIFF(SECOND, v.event_time, COALESCE(v.next_event_time, c.w_end)) < {recycle_transient_max_seconds})
    AND v.event_time < c.w_end AND COALESCE(v.next_event_time, c.w_end) > c.w_start
),
qw AS (
  SELECT c.workspace_id, c.warehouse_id, c.epoch_start, x.statement_id,
         x.waiting_at_capacity_duration_ms AS wait_ms,
         TIMESTAMPADD(MILLISECOND, COALESCE(x.waiting_for_compute_duration_ms, 0), x.start_time) AS wait_start,
         TIMESTAMPADD(MILLISECOND, COALESCE(x.waiting_for_compute_duration_ms, 0) + x.waiting_at_capacity_duration_ms, x.start_time) AS wait_end
  FROM system.query.history x
  JOIN cur c ON x.workspace_id = c.workspace_id AND x.compute.warehouse_id = c.warehouse_id
  WHERE x.compute.type = 'WAREHOUSE' AND x.waiting_at_capacity_duration_ms > 0
    AND x.start_time >= c.w_start AND x.start_time < c.w_end
),
ov AS (
  SELECT q.workspace_id, q.warehouse_id, q.epoch_start, q.statement_id, q.wait_ms,
         COALESCE(SUM(GREATEST(0, UNIX_MILLIS(LEAST(q.wait_end, m.seg_end)) - UNIX_MILLIS(GREATEST(q.wait_start, m.seg_start)))), 0) AS wait_ms_at_max
  FROM qw q LEFT JOIN at_max m ON m.workspace_id = q.workspace_id AND m.warehouse_id = q.warehouse_id
                              AND m.seg_start < q.wait_end AND m.seg_end > q.wait_start
  GROUP BY q.workspace_id, q.warehouse_id, q.epoch_start, q.statement_id, q.wait_ms
)
SELECT workspace_id, warehouse_id, epoch_start,
  SUM(wait_ms)        AS queued_wait_ms_30,
  SUM(wait_ms_at_max) AS queued_wait_ms_at_max_30,
  COUNT_IF(wait_ms_at_max >= 0.5 * wait_ms) AS queued_queries_at_max_30,
  PERCENTILE_APPROX(CASE WHEN wait_ms_at_max >= 0.5 * wait_ms THEN wait_ms END, 0.95) AS p95_wait_capacity_ms_at_max_30
FROM ov GROUP BY workspace_id, warehouse_id, epoch_start
```

### 4. New evidence fields (keys in current `EpochEvidence.query`)
`queued_wait_ms_30` (float), `queued_wait_ms_at_max_30` (float), `queued_queries_at_max_30` (int), `p95_wait_capacity_ms_at_max_30` (float).
- Null = not collected.
- `queued_wait_ms_30 = 0` = no queueing (known zero; share undefined, and the increase rule does not fire anyway).

Old evidence fails closed **only on the increase path**, where queue pressure would otherwise trigger: `INSUFFICIENT_EVIDENCE` with `QUEUE_CEILING_CORRELATION_UNAVAILABLE`. The reduce path keeps today's rules (plus M2's 30-day window).

### 5. Where it plugs in
- Collector: new `build_queue_ceiling_sql`.
- Analyzer, max-cluster section: facts `queue_wait_at_max_share_pct = 100·at_max/total` (None if either is None), `queued_queries_at_max_30`, `p95_wait_at_max`, plus findings.
- Optimizer, max-cluster increase branch: the `saturated` predicate becomes `share ≥ queue_wait_at_max_share_min AND queued_queries_at_max_30 ≥ severity_min_affected_queries AND p95_wait_at_max ≥ queue_wait_p95_ms_increase_max`. `time_at_max_pct ≥ 5` stays as a secondary guard. When `max_clusters == min_clusters` the share is trivially 100% when running, which is correct: there is no autoscaling.
- Estimator: unchanged. The uplift still uses `minutes_at_max`, but from M2's 30-day recycle-excluded series. Bundle: unchanged.

### 6. Thresholds (judgment calls)
- `queue_wait_at_max_share_min = 50.0` (%).
- `recycle_transient_max_seconds = 600`. A5 is unknown. V6 implies roughly one recycle per day, so a 10-minute tail is about 0.7% of a day.
- Reuse `severity_min_affected_queries = 20`.

### 7. Contract impact
**NONE** (new blocker/reason codes, free-form findings). Bump `ANALYZER_VERSION` and `OPTIMIZER_VERSION`. Workbook: NONE.

### 8. Tests and fixtures
- Oracle for the interval overlap.
- The worked example: 10% share → `NO_CHANGE`; 70% share → `ACTIONABLE`; exactly 50% → boundary passes.
- Recycle transient: max=1 with a 2-cluster blip of 5 minutes. The adjacent at-max piece under 600 s is excluded, and a long at-max piece is kept.
- Missing keys + queue pressure → `INSUFFICIENT_EVIDENCE`. Missing keys + no queue pressure → unaffected.
- `concurrency-constrained` golden gets the new keys (share ≥ 50%) so `max_clusters_target: 3` still holds.

### 9. Risks / unknowns
- A4 is the main one. If the capacity queue precedes `waiting_for_compute`, or compilation sits in between, the interval is shifted. At the first real run, compute the share under both orderings and record the sensitivity.
- Serverless IWM queueing semantics may differ from Classic/Pro (V5).
- The 1,000-query queue cap (V5) truncates extreme pressure.

### 10. Priority: **P2**. It only affects increases, which are cost-adding performance actions, and M3 already removed the worst confounder.

---

## M4. Historical size calibration (`Estimator._matched_epoch_factor`)

### 1. Problem
The size estimate blends 50% of a "historical epoch factor" whenever a past epoch had the target size and identical other knobs. The factor is the ratio of `list_cost / epoch_queries × 1000` between epochs. Cost per 1,000 queries is not a workload-invariant unit: query mix, bytes read, concurrency, data growth and the time period all differ between epochs.

**Worked example (actual golden output).** `idle-expensive-warehouse` (also `cold-start-sensitive`, `photon-disabled`, `aws-region-missing`) recommends LARGE→MEDIUM, `TEST_REQUIRED`. The prior MEDIUM epoch is used:

| | prior MEDIUM epoch | current LARGE epoch |
|---|---|---|
| cost / queries / days | $3,900 / 30,000 / 106 | $21,100 / 110,000 / 259 |
| cost per 1,000 queries | $130.0 | $191.8 |
| queries per day | 283 | 425 (ratio 0.666) |

The factor is 0.678 at weight 0.5:

| Size candidate (total DBU+AWS savings) | low | base | high | width |
|---|---|---|---|---|
| with calibration (today) | $8,004 | $11,010 | $13,416 | $5,412 |
| without calibration | $6,691 | $11,385 | $15,140 | $8,449 |

The calibration **narrows the range by 36%**, which is false precision. It comes from an epoch with a 33% lower query rate, and the evidence has no information about mix or bytes.

### 2. Decision: **remove calibration from pricing** (recommended), keep it as an observation

Reasons:
- **Not causal.** A same-config match fixes config but not time. Workload drift, data growth and seasonality between epochs are confounded with size. Even with workload gates passing, the comparison stays observational and n=1.
- **Rarely gated safely.** The gates need the full query-mix, bytes and concurrency profile per *historical* epoch, collected epoch-wide (today only `epoch_queries`, `epoch_p95_execution_ms`, `epoch_queued_query_pct` and `epoch_spill_query_pct` exist). That is a larger collector and evidence expansion for an estimator that is only used on canary-gated (`TEST_REQUIRED`) downsizes and HITL upsizes.
- **Canary supersedes it.** Downsizes are `TEST_REQUIRED`, so the measured canary is the real calibration. Upsizes are performance-led.
- **The most likely match is a revert**, for example MEDIUM→LARGE because of spill and now LARGE→MEDIUM. Lower historical cost per query there is exactly the regime that failed on performance.
- Allowed by PRD FR-EPOCH-003 ("*may* be used … only when configuration compatibility is explicitly satisfied").

The matched epoch stays as an **analyzer finding** (`historical_target_size_cost_per_1000_queries`, window "historical epoch <id>", interpretation "observational, workload comparability unverified; not used in pricing"). Authority: DERIVED, CALIBRATION/BACKTEST-only per CLAUDE.md.

Alternative, if the user prefers to keep it: require **all** of the gates below (judgment-call thresholds), and fail closed when any input is absent.

| Gate | Threshold |
|---|---|
| queries/active-day ratio | within [0.67, 1.5] |
| median `read_bytes` per query ratio | within [0.8, 1.25] |
| source/client mix L1 distance (multi-label shares normalized) | ≤ 0.20 |
| SELECT `statement_type` share difference | ≤ 10 pp |
| `from_result_cache` share difference | ≤ 10 pp |
| p95 concurrent queries (peak running+queued) ratio | within [0.67, 1.5] |
| historical epoch ended | ≤ 180 days ago |
| historical epoch size | ≥ 14 days, ≥ 100 queries (existing) |

On the goldens, the historical epochs carry none of these fields, so every gate fails closed. The query-rate ratio 0.666 also fails on its own. **Both options give the same golden outcome: no calibration.**

### 3. SQL
None for removal. For the gated alternative, the existing `EPOCH_QUERY_METRICS_SQL` would add epoch-wide aggregates: `PERCENTILE_APPROX(read_bytes, 0.5)`, `COUNT_IF(statement_type = 'SELECT')`, source/client counts without the 30-day filter, and a concurrency profile (an interval self-join, the expensive part). This is not drafted further unless chosen.

### 4. Evidence fields
Removal: none. The finding uses existing fields. Backward compatibility is trivial.

### 5. Where it plugs in
- `Estimator._factors`, `warehouse_size` branch: delete the `calib` blend. The `method` becomes "capacity ratio × runtime elasticity". `_matched_epoch_factor` moves to a pure analyzer helper, `Analyzer.analyze` size section, as a finding with no economic effect.
- Optimizer and bundle: unchanged.

### 6. Thresholds
Removal deletes `epoch_calibration_weight`, `epoch_factor_floor` and `epoch_factor_ceiling`. `min_epoch_calibration_days/queries` stay for the finding.

### 7. Contract impact
**NONE.** `historical_epoch_calibration` is internal candidate metadata, not exported to the package. Values change, so bump `ESTIMATOR_VERSION`. Workbook: NONE.

### 8. Tests
- Replace `test_current_epoch_drives_findings_and_history_calibrates_estimator` (`tests/test_publisher.py` around line 51). New assertions: the size candidate's metadata has no calibration blend, the matched-epoch finding exists with a historical `epoch_id`, and the findings used for decisions all reference the current epoch.
- Before/after on `idle-expensive-warehouse`: total savings $8,004/$11,010/$13,416 → $6,691/$11,385/$15,140. DBU only: $7,388/$9,076/$10,426 → $6,075/$9,450/$12,150.

### 9. Risks / unknowns
Ranges get wider, which is the honest result. Only a real-data backtest (predicted versus canary-measured) could show whether a gated calibration adds skill. Revisit after realization ingestion exists.

### 10. Priority: **P2, but do it first.** It is the smallest, estimator-local change and is independent of the others.

---

## M9. Show the next step right after an applied change (design sketch, user request)

### 1. Problem
Run 1: Plan A for `idle-expensive-warehouse` is

| Step | State | Pricing basis | Running savings (base) |
|---|---|---|---|
| 1. size LARGE→MEDIUM | `READY_FOR_CANARY` | AUTHORITATIVE | $11,010 |
| 2. max | `PENDING_REEVALUATION` | CONTINGENT | $11,010 |
| 3. min | `PENDING_REEVALUATION` | CONTINGENT | $15,776 |
| 4. auto-stop | `PENDING_REEVALUATION` | CONTINGENT | contingent total $20,066 |

The user applies step 1 on day 0. Run 2 on day 1 sees a new current epoch of age 0:
- every knob carries `CURRENT_CONFIG_EPOCH_TOO_NEW`;
- auto-stop is `BLOCKED` with a null estimate;
- cost history is under 7 days, so economics are withheld;
- Plan A is empty (as in golden `newly-changed-config`).

The user sees **nothing** until day 7, even though run 1 had already shown the next steps as contingent upside.

Constraints: stateless publisher; no realization ingestion (Phase 1 emits `NOT_APPLIED`); H1 (pre-change-evidence steps are CONTINGENT, never authoritative); the current epoch controls recommendations.

### 2. Options

| | A. Derive the applied step from the epoch config diff; replay the prior epoch's plan as a contingent preview | B. Carry the prior epoch's observed run-rate × the applied step's modeled effect as the new baseline | C. Show next steps as BLOCKED/PENDING with blocker and earliest re-evaluation time, no number |
|---|---|---|---|
| Mechanism | `diff(prior epoch config, current config)`. If it changes exactly one knob, and re-running Analyzer→Optimizer→Estimator→Planner on the **immediately prior epoch** (as of its `epoch_end`) yields step 1 with that exact target, the step is derived as applied. The remaining prior steps are shown `PENDING_REEVALUATION`, priced on the prior contingent chain. | Baseline_new = prior 28/84-day run-rate × factor(step 1). Next step priced against it. | Steps listed with `BLOCKED`, blockers (`CURRENT_CONFIG_EPOCH_TOO_NEW`, `COST_HISTORY_INSUFFICIENT`) and `earliest_reevaluation_time = max(epoch_start + 7 d, first day 7 complete billing days exist)`. Which knobs appear comes from a deterministic replay as in A; no economics. |
| Deterministic | Yes, if the prior epoch's windows are anchored at its `epoch_end` (needs the M2 daily series for the prior epoch, collected only when the current epoch is younger than maturity). | Yes. | Yes. |
| Honest about uncertainty | Good. It is exactly the figure run 1 already published as contingent, never authoritative, never in package economics. It needs a label like "applied change not yet measured". | **Poor.** It publishes a modeled number as a *baseline*, compounding model on model. It conflicts with "unknown cost is null" for the current epoch, and next-step *decisions* still lack current-epoch evidence, so it only fills the economics. | Best, but it gives no number, which is what the user asked to avoid. |
| Rule friction | It uses a historical epoch for a (contingent) recommendation preview, an **exception** to "historical epochs are calibration/backtest only". | Same exception, plus a synthetic baseline. | None for decisions. |
| Contract impact | Needs the contract designer: a `COMPLETED` step derived from config (enum exists, semantics and validation undefined); `PENDING_REEVALUATION` steps in a scenario with no authoritative step; an `earliest_reevaluation_time` field; possibly realization `APPLIED`. | Needs the contract designer: a new baseline basis (e.g. `CARRIED_FORWARD_MODELED`), a cost-basis enum change, and package-envelope rules. | Needs the contract designer, but small: `BLOCKED` plan steps (today candidate `BLOCKED` means no counterfactual and steps exclude them), plus `earliest_reevaluation_time`. |

**Recommendation: C as the always-on base, plus A's contingent preview when the diff matches exactly (C+A).** If the diff does not match, because several knobs changed, a different value was applied, or the type changed, the prior plan is `INVALIDATED` and only C is shown. Reject B.

Staged delivery:
- Stage 1: C only.
- Stage 2: A's preview, after contract sign-off and the policy exception are approved.

Unblock-time arithmetic. Example: `epoch_start = 2026-10-01T15:00Z`.
- Maturity gate: met at `2026-10-08T15:00Z`.
- Cost-history floor: `basis_start = 2026-10-01`, so 7 days needs `basis_end ≥ 2026-10-07`, which is analysis date ≥ 2026-10-08 with the watermark past `2026-10-08T00:00Z`.
- Earliest re-evaluation: `2026-10-08T15:00Z`. It is labelled "earliest", because query-sample gates may still block.

Run 2 under C+A on day 1:

| Step | State | Running savings (base) |
|---|---|---|
| 1. size | `COMPLETED` (config observed, savings unmeasured) | — |
| 2. max | `PENDING_REEVALUATION` (CONTINGENT) | $11,010 |
| 3. min | `PENDING_REEVALUATION` (CONTINGENT) | $15,776 |
| 4. auto-stop | `PENDING_REEVALUATION` (CONTINGENT) | contingent $20,066 |

Earliest re-evaluation is 2026-10-08T15:00Z. Authoritative package economics are UNAVAILABLE/null (unchanged).

### 3. Evidence and authority
| Evidence | Authority |
|---|---|
| Epoch config diff | AUTHORITATIVE, `system.compute.warehouses` |
| Prior-epoch replay | DERIVED from a historical epoch, CONTINGENT use only |
| Applied-change realization | UNAVAILABLE (no ingestion) |

### 4. Fields
- Collector: when `epoch_age < current_config_stability_days`, add the immediately prior epoch's `resource_daily` and its `_30` query aggregates **anchored at that epoch's `epoch_end`**. That is the same SQL with `{window_end}` = prior `epoch_end`, which is bounded.
- Old evidence without these fields: C only (fail closed for A).

### 5. Plug-in points
- A new pure step `derive_prior_plan(e)` in the planner layer. It calls the existing Analyzer/Optimizer/Estimator/ScenarioPlanner on a `WarehouseEvidence` view whose current epoch is the prior epoch and whose `analysis_time = prior epoch_end`. No responsibilities merge; it is a replay.
- `ScenarioPlanner.scenarios` merges in: applied step → `COMPLETED`; remaining steps → `PENDING_REEVALUATION`, priced by `StatefulBundleEstimator.estimate` on the replay evidence, contingent chain only.
- `_CONTRACT_EXECUTION_STATE` gains `COMPLETED`/`BLOCKED`/`INVALIDATED` mappings only after the contract is updated.

### 6. Constants (judgment calls)
- `applied_step_match_policy = EXACT_SINGLE_KNOB`.
- No new time constants: reuse `current_config_stability_days` and `min_economics_cost_history_days`.

### 7. Contract impact
**Needs the `databricks-contract-designer` skill and user sign-off** for C (Stage 1) and for A (Stage 2). A also needs sign-off on the CLAUDE.md "historical epochs are calibration only" exception. Workbook: the Scenario Plans sheet would show the new states and dates through the existing columns if the contract fields project. Not designed here.

### 8. Tests
- Golden `applied-size-step`: prior epoch = current golden LARGE epoch; current epoch = MEDIUM, age 1 day. Expect step 1 `COMPLETED`, steps 2–4 contingent with the run-1 figures, earliest time as computed, and package authoritative economics null.
- Golden `applied-different-value` (LARGE→SMALL): the prior plan is `INVALIDATED` and only C applies.
- Golden `two-knobs-changed`: C only.
- Determinism: the same evidence gives an identical package regardless of when it is replayed.

### 9. Risks / unknowns
- How often users apply exactly the recommended value.
- Whether showing contingent figures right after a change is read as a promise (UX wording).
- Prior-epoch windows anchored at `epoch_end` cost an extra bounded scan.

### 10. Priority: **P3**. It is UX value, depends on M2 (daily series) and on contract sign-off.

### ExecutionState taxonomy (contract enum already has all seven)

| State | Emitted today | Proposed meaning | Derivable statelessly when |
|---|---|---|---|
| `READY` | yes (planner `READY_FOR_HITL`) | Current-epoch evidence is mature and the candidate is `ACTIONABLE`/`REVIEW`; apply after HITL approval. | as today |
| `READY_FOR_CANARY` | yes | Candidate `TEST_REQUIRED`; apply as an A/B canary, then remeasure. | as today |
| `PENDING_REEVALUATION` | yes | An upstream step in this plan invalidates this knob's evidence; priced CONTINGENT only. | as today; also M9-A remaining steps |
| `REQUIRED` | yes (non-candidate gates) | Compatibility validation or remeasure gate that must happen. | as today |
| `BLOCKED` | **never** | A candidate step exists as a direction, but a named, time- or action-clearable blocker stops it now (epoch too new, cost history short, compatibility FAIL). It carries blockers and `earliest_reevaluation_time`, with no economics. Distinct from candidate status `BLOCKED` (no target at all). | M9-C: from blockers + epoch start + watermark |
| `COMPLETED` | **never** | The step's `recommended_value` is the AUTHORITATIVE current config, and the prior epoch held its `current_value`. This means config applied, **not savings realized**; realization stays separate. | M9-A: exact single-knob diff matching the replayed prior plan |
| `INVALIDATED` | **never** | A step from the replayed prior plan whose premise no longer holds: its knob changed to another value, an upstream invalidating knob changed, the type changed, or the replay no longer recommends it. | M9-A: diff mismatch, or a replayed step absent from the current plan after maturity |

Open contract question: should `COMPLETED` imply a realization record with status `APPLIED` (enum exists), or must realization stay `NOT_APPLIED` until ingestion exists?

---

## Prioritization

| Item | Priority | Effort | Contract | Depends on | Order |
|---|---|---|---|---|---|
| M4 remove size calibration | P2 | S (estimator + 1 test) | NONE | — | 1 |
| M1 RUNNING-intersected auto-stop | **P1** | M (collector + analyzer/optimizer/estimator; goldens) | NONE | — (Phase 1 365-day window) | 2 |
| M2 aligned 30/28/84-day windows (daily series) | **P1** | M–L (collector, modeler, AWS model, all goldens) | NONE | shares M1 collector pass | 3 |
| M3b queue-at-ceiling correlation | P2 | M | NONE | M2 (30-day recycle-excluded series) | 4 |
| M9 next step after applied change | P3 | L | **contract designer + sign-off** | M2; contract approval | 5 |

Recommended order: **M4 → M1 → M2 → M3b → M9.** M4 is an isolated quick win. M1 and M2 should share one collector pass.

## Questions that need the user's decision

1. **M9:** approve Stage 1 (BLOCKED next steps with an earliest re-evaluation time, no number) for contract design now? And for Stage 2, accept the CLAUDE.md exception that lets the immediately prior epoch drive a **CONTINGENT-only** next-step preview, with `COMPLETED` derived from the config diff?
2. **M4:** remove size calibration from pricing (recommended), or keep it behind the workload gates in §M4.2 and fund the historical-epoch collector expansion?
3. **Fail-closed rollout (M1/M2/M3b):** accept that existing evidence files and all goldens without the new fields turn auto-stop, min/max decisions and the AWS baseline into `INSUFFICIENT_EVIDENCE` or null until regenerated? The alternative is a labelled legacy method, which the brief disallows.
4. Threshold defaults (all judgment calls): billing/event mismatch 5%, minimum removable idle 2%, queue-wait-at-max share 50%, recycle transient 600 s, regime band [0.67, 1.5].
5. M2 windows: keep three windows (30 for pressure, 28/84 for economics), or unify query facts to 28 days to match the forecast (changes the `*_30` threshold semantics)?
6. Should `additional_starts_if_T` (M1) become a cold-start blocker, or stay observation-only until real data?
7. Should the normalized evidence JSON carry an explicit `evidence_format_version` so replay can name the missing capability, instead of inferring it from absent keys?
8. Should `COMPLETED` (M9) emit realization `APPLIED`, or keep `NOT_APPLIED` until ingestion exists?
