# Threshold posture

The defaults are **balanced-to-conservative overall**. They are policy defaults, not universal Databricks recommendations. The complete list of every numeric default is in the [Parameter reference](#parameter-reference) section at the end of this page.

| Decision | Current policy | Posture | Why |
|---|---|---|---|
| Photon observed state | >=90% non-null feature coverage, >=1 current-epoch DBU, <=14d freshness; >=95% true=ENABLED, <=5%=DISABLED, else MIXED | Conservative | Observation only (v2.7.1): reported in `current_config.photon`, never turned into a recommendation. Databricks publishes no Photon on/off price for SQL warehouses, so no savings can be modeled without guessing. |
| Size down | >=500 queries, <=0.1% spill, <=0.5% queued, p95<=5s, p99<=30s, epoch>=7d, one size only, canary required | Conservative | Multiple independent safety signals; never auto-applies. |
| Size up | spill >=2% of queries and p95 spill **among spilled queries** >=1 GiB, **and** >=20 spilled queries, >=100 queries and epoch >=7d; otherwise `INSUFFICIENT_EVIDENCE` with no resize recommended | Balanced/conservative | Dual frequency+severity signal avoids reacting to isolated spills; sample/maturity gate stops a single query or an immature config regime from driving a resize. |
| Max clusters up | >=5% queued + p95 capacity wait **among queued queries** >=30s + >=20 queued queries + observed saturation; fewer than 20 queued → `INSUFFICIENT_EVIDENCE` | Conservative | Triple gate avoids over-scaling. |
| Max clusters down | <=0.5% queue, <=1% time at max (last 30 days of the resource window), observed max below config | Conservative | Guardrail only; no fake direct savings. |
| Min clusters down | >=100 queries, >=25% time at min, <=0.5% queue, p95 capacity wait among queued queries <=5s (unknown wait severity blocks), <20% time at max (last 30 days of the resource window); **canary required (`TEST_REQUIRED`)**, savings are an upper-bound range (low 0% / base 50% / high 100% of the time-at-min cluster-minutes) | Conservative | Time at the floor shows the autoscaler sat at min, not that a floor cluster was spare; without utilization evidence the reduction is a canary candidate, not an actionable change. |
| Auto-stop | 15/30/45 min by activity; Serverless 10 min target; >=30 gaps; cold-start-sensitive workloads blocked | Balanced; most aggressive cost rule | Uses measured idle gaps, but guards against cold-start latency. |
| Type migration | compatibility gate + pre-canary modeled range + canary | Conservative | IWM/runtime response is not assumed. |
| Failure guard on capacity cuts | failed/cancelled share >5% of queries, or unreported, holds every capacity cut (size down, max/min clusters down, lower auto-stop) | Conservative | A cut on a warehouse that is already failing queries would add risk where the evidence is least trustworthy. Blockers `FAILURE_CANCEL_RATE_HIGH` / `FAILURE_CANCEL_EVIDENCE_UNAVAILABLE`. |
| Forward DBU forecast | published only when every basis day is priced, the current epoch has >=7 billed (positive-cost) days (`min_economics_cost_history_days`) and the run rate is positive; otherwise null (never $0), and savings estimates and type migration are withheld | Conservative | A run rate from a few days, or a floor at zero, would guess the baseline every savings figure scales. Blocker `COST_HISTORY_INSUFFICIENT` (epoch younger than 7 calendar days), `BILLED_COST_DAYS_INSUFFICIENT` (mature epoch with too few billed days) or `DBU_COST_UNAVAILABLE`. |
| Billing settle | a UTC day enters the cost basis `billing_settle_hours` = 24 after it ends and once its workspace's billing watermark has passed it | Conservative | Original billing records are typically available within 12 hours; the buffer keeps a still-arriving day out of the run rate. |
| Pressure window | `pressure_window_days` = 30: time at min, time at max and observed peak come from the last 30 days of the daily resource window | Balanced | Capacity facts describe recent behavior on the same calendar as the DBU forecast rather than the whole epoch. Without the series, max/min-cluster changes are held with `RECENT_RESOURCE_WINDOW_UNAVAILABLE`. |

## Missing pressure evidence fails closed

Spill frequency, queue frequency and p95/p99 execution latency stay null when the collector did not report them; they are never read as 0 ("no pressure"), and findings report them as null.

- Size down is held (`NO_CHANGE`) with `SPILL_EVIDENCE_UNAVAILABLE`, `QUEUE_EVIDENCE_UNAVAILABLE` or `LATENCY_EVIDENCE_UNAVAILABLE`.
- Max-cluster reduction is held with `QUEUE_EVIDENCE_UNAVAILABLE`, or `QUEUE_WAIT_EVIDENCE_UNAVAILABLE` when queries queued but their capacity-wait percentile is missing.
- Min-cluster reduction is held with `QUEUE_EVIDENCE_UNAVAILABLE`.
- Size up and max-cluster increase need known spill / queue evidence.

## Severity is measured among affected queries

Spill, capacity-wait and compute-wait severity percentiles are computed **only over the queries that were affected** (`spilled_local_bytes > 0`, `waiting_at_capacity_duration_ms > 0`, `waiting_for_compute_duration_ms > 0`), and the affected-query counts (`spill_queries_30`, `queued_queries_30`, `compute_wait_queries_30`) are collected beside them. A percentile over all queries hides rare-but-severe events behind the unaffected majority.

- Rules that add capacity/cost (size up, max clusters up) require `severity_min_affected_queries` (20) affected queries; below that a p95 is just the maximum observation.
- Guard rules that block savings (min-cluster reduction queue guard, auto-stop cold-start guard) fire on any affected queries.
- If the collector did not report the affected-query count (older evidence), severity is *unknown*, never zero: min-cluster reduction and auto-stop are blocked (`QUEUE_WAIT_EVIDENCE_UNAVAILABLE`, `COLD_START_EVIDENCE_UNAVAILABLE`). Size-up and max-cluster increase report `INSUFFICIENT_EVIDENCE` (`SPILL_SEVERITY_EVIDENCE_UNAVAILABLE`, `QUEUE_WAIT_EVIDENCE_UNAVAILABLE`) when their frequency threshold is crossed but severity is unknown, instead of a silent no-change.

## Cost-estimator guardrail

Serverless size economics may use the published SQL Serverless DBU/hour schedule. Pro/Classic size economics must **not** reuse that schedule. v2.9.0 uses same-workspace observed Pro/Classic DBU-per-cluster-hour ratios when both sizes exist; otherwise it falls back to nominal size-capacity ratios and preserves the A/B canary requirement.

## Tuning recommendation

Keep these defaults for the first real run. Review false positives/negatives across 10–20 representative warehouses before changing them. Prefer SLA/workspace overrides over globally loosening safety gates.

## Parameter reference

Every field of `Thresholds` in `publisher.py`, one row per field, grouped by decision area. Each default is the dataclass default; a test compares the two. The meaning is taken from how the publisher uses the field. Warehouse-type migration has no dedicated parameter: it is gated by `min_economics_cost_history_days` and priced with the estimator parameters below. Changing any decision-affecting default requires a policy version change and golden-test updates.

### Evidence sufficiency and cost history

Minimum samples and history before any decision or estimate is published.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `min_query_sample_size` | `100` | Minimum number of queries in the 30-day window (count) before any size, max-cluster, min-cluster or auto-stop change is considered; also gates the workload-profile latency and concurrency classes and the evidence-quality status. | [PRD FR-OPT-003](../product/prd.md), [PRD FR-OPT-007](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`INSUFFICIENT_QUERY_SAMPLE`](blocker-codes.md) |
| `strong_query_sample_size` | `500` | Query count in the 30-day window (count) required for a size-down and the full-credit point of the sample component of candidate confidence. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §16 Candidate confidence model](../architecture/lld.md), Gates [`INSUFFICIENT_STRONG_QUERY_SAMPLE`](blocker-codes.md) |
| `min_cost_history_days` | `14` | Billed cost-history days (days) the current epoch needs for the package evidence-quality assessment to count cost history as sufficient; it does not gate any forecast or recommendation. | code only |
| `min_economics_cost_history_days` | `7` | Minimum current-epoch billed (positive-cost) days (days) before a forward DBU forecast, any savings estimate or a cost-judged warehouse-type migration is published; also the cost-history re-evaluation gate length, which must equal the locked contract constant. | [PRD §14.1 Economic concepts](../product/prd.md), [LLD §11.2 Daily run rate](../architecture/lld.md), [LLD §15.7 Warehouse type](../architecture/lld.md), Gates [`COST_HISTORY_INSUFFICIENT`](blocker-codes.md), [`BILLED_COST_DAYS_INSUFFICIENT`](blocker-codes.md), [`DBU_COST_UNAVAILABLE`](blocker-codes.md) |
| `strong_cost_history_days` | `60` | Intended threshold (days) for strong cost-history coverage; no code path reads it. | code only |
| `current_config_stability_days` | `7` | Minimum age of the current configuration epoch (days) before a size, max-cluster, min-cluster or auto-stop change is considered; also the epoch-maturity re-evaluation gate length, which must equal the locked contract constant. | [PRD FR-EPOCH-004](../product/prd.md), [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`CURRENT_CONFIG_EPOCH_TOO_NEW`](blocker-codes.md) |
| `severity_min_affected_queries` | `20` | Minimum number of affected (spilled or queued) queries (count) behind a p95 severity percentile before a rule that adds capacity or cost may act on it. | [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.4 Max clusters](../architecture/lld.md), Gates [`INSUFFICIENT_AFFECTED_QUERY_SAMPLE`](blocker-codes.md), [severity among affected queries](thresholds.md) |

### Photon observation

Photon is observed from current-epoch billing and never recommended; these parameters only decide the reported state.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `photon_min_feature_coverage_pct` | `90.0` | Minimum share of current-epoch DBU (percent) whose billing row carries a non-null Photon feature flag; below it the Photon state is UNKNOWN. | [PRD FR-OPT-002 — Photon observation](../product/prd.md), [LLD §13.2 Thresholds](../architecture/lld.md), [Context §11.2 State model](../architecture/context.md) |
| `photon_enabled_usage_pct` | `95.0` | Share of known-flag DBU (percent) that must be Photon-enabled for the observed state to be ENABLED. | [PRD FR-OPT-002 — Photon observation](../product/prd.md), [LLD §13.2 Thresholds](../architecture/lld.md), [Context §11.2 State model](../architecture/context.md) |
| `photon_disabled_usage_pct` | `5.0` | Share of known-flag DBU (percent) at or below which the observed state is DISABLED; between this and the enabled share it is MIXED. | [PRD FR-OPT-002 — Photon observation](../product/prd.md), [LLD §13.2 Thresholds](../architecture/lld.md), [Context §11.2 State model](../architecture/context.md) |
| `photon_min_epoch_dbu` | `1.0` | Minimum total current-epoch DBU (DBU) for a Photon observation; also scales the volume term of the Photon confidence score (ten times this value gives full credit). | [PRD FR-OPT-002 — Photon observation](../product/prd.md), [LLD §13.2 Thresholds](../architecture/lld.md) |
| `photon_freshness_days` | `14` | Maximum age of the latest supporting billing usage (days) for a Photon observation; also scales the freshness term of the Photon confidence score. | [PRD FR-OPT-002 — Photon observation](../product/prd.md), [LLD §13.2 Thresholds](../architecture/lld.md) |

### Cost forecast and billing windows

Forward DBU run-rate model, billing settle buffer and the recent-pressure window.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `forecast_recent_days` | `28` | Length of the recent window (days) in the DBU run-rate blend and in the trend ratio; also used for the AWS resource-minute blend. | [LLD §11.2 Daily run rate](../architecture/lld.md), [LLD §12.2 Pro/Classic](../architecture/lld.md) |
| `forecast_long_days` | `84` | Length of the long window (days) in the DBU run-rate blend; also the length of the daily resource window and the full-credit point of forecast-confidence history coverage. | [LLD §11.2 Daily run rate](../architecture/lld.md), [LLD §12.2 Pro/Classic](../architecture/lld.md) |
| `billing_settle_hours` | `24` | Hours after a UTC day ends before its billing counts toward the cost basis (hours), in addition to the workspace billing watermark having passed it. | [LLD §11.1 Basis](../architecture/lld.md), [Context §8.2 Source latency is not data quality failure](../architecture/context.md), [HLD §6.1 Collector](../architecture/hld.md) |
| `pressure_window_days` | `30` | Number of most recent days of the daily resource window (days) from which time at min, time at max and the observed peak cluster count are taken. | [LLD §15.4 Max clusters](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`RECENT_RESOURCE_WINDOW_UNAVAILABLE`](blocker-codes.md) |
| `forecast_cv_min_days` | `7` | Minimum number of daily observations (days) in the long window before the cost coefficient of variation is computed; otherwise it is null. | [LLD §11.2 Daily run rate](../architecture/lld.md) |
| `forecast_weight_28_long` | `0.65` | Weight (fraction) on the recent-window mean when the basis has at least the long-window length; the long-window mean gets the remainder. | [LLD §11.2 Daily run rate](../architecture/lld.md) |
| `forecast_weight_28_mid` | `0.75` | Weight (fraction) on the recent-window mean when the basis is at least the recent-window length but shorter than the long window; the all-basis mean gets the remainder. | [LLD §11.2 Daily run rate](../architecture/lld.md) |
| `trend_adjustment_weight` | `0.25` | Fraction of the clamped recent-versus-previous trend ratio applied to the run rate as 1 + weight x (ratio - 1). | [LLD §11.2 Daily run rate](../architecture/lld.md) |
| `trend_ratio_floor` | `0.75` | Lower clamp (ratio) on the recent-versus-previous window mean before the trend adjustment is applied. | [LLD §11.2 Daily run rate](../architecture/lld.md) |
| `trend_ratio_ceiling` | `1.25` | Upper clamp (ratio) on the recent-versus-previous window mean before the trend adjustment is applied. | [LLD §11.2 Daily run rate](../architecture/lld.md) |

### Queue and concurrency

Queue pressure limits shared by size down, max clusters and min clusters.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `queue_query_pct_increase_max` | `5.0` | Share of queries that queued (percent) at or above which concurrency pressure counts toward a max-cluster increase; also the BURSTY boundary of the workload profile. | [PRD FR-OPT-005](../product/prd.md), [LLD §15.4 Max clusters](../architecture/lld.md) |
| `queue_wait_p95_ms_increase_max` | `30000` | p95 capacity wait among queued queries (ms) at or above which a max-cluster increase is considered. | [PRD FR-OPT-005](../product/prd.md), [LLD §15.4 Max clusters](../architecture/lld.md) |
| `queue_query_pct_safe` | `0.5` | Largest share of queries that queued (percent) still treated as low queue pressure for a size down, max-cluster reduction or min-cluster reduction; also the STEADY boundary of the workload profile. | [PRD FR-OPT-003](../product/prd.md), [PRD FR-OPT-007](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.4 Max clusters](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`QUEUE_PRESSURE_PRESENT`](blocker-codes.md) |
| `queue_wait_p95_ms_safe` | `5000` | Largest p95 capacity wait among queued queries (ms) still treated as safe for a min-cluster reduction. | [PRD FR-OPT-007](../product/prd.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`QUEUE_PRESSURE_PRESENT`](blocker-codes.md), [`QUEUE_WAIT_EVIDENCE_UNAVAILABLE`](blocker-codes.md) |

### Warehouse size

Spill, latency and failure limits for size up and size down.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `spill_query_pct_upsize` | `2.0` | Share of queries that spilled to local disk (percent) at or above which an upsize is considered. | [PRD FR-OPT-004](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `spill_p95_bytes_upsize` | `1073741824` | p95 spilled bytes among spilled queries (bytes; 1 GiB) at or above which an upsize is considered. | [PRD FR-OPT-004](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `spill_query_pct_downsize_safe` | `0.1` | Largest share of queries that spilled (percent) still treated as safe for a size down. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`SPILL_PRESENT`](blocker-codes.md) |
| `failure_cancel_pct_downsize_safe` | `5.0` | Largest share of failed or cancelled queries (percent) before every capacity cut (size down, lower max or min clusters, lower auto-stop) is held. | [LLD §15 Optimizer decision algorithms](../architecture/lld.md), Gates [`FAILURE_CANCEL_RATE_HIGH`](blocker-codes.md), [failure guard row](thresholds.md) |
| `p95_execution_ms_downsize_safe` | `5000` | Largest p95 query execution time (ms) still treated as safe for a size down. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`P95_LATENCY_TOO_HIGH`](blocker-codes.md) |
| `p99_execution_ms_downsize_safe` | `30000` | Largest p99 query execution time (ms) still treated as safe for a size down. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`P99_LATENCY_TOO_HIGH`](blocker-codes.md) |

### Max clusters

Saturation, headroom and range limits for the max-cluster ceiling.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `time_at_max_pct_safe_reduce` | `1.0` | Largest share of time at the configured maximum (percent, pressure window) still allowing a max-cluster reduction. | [PRD FR-OPT-006](../product/prd.md), [LLD §15.4 Max clusters](../architecture/lld.md) |
| `time_at_max_pct_saturated` | `5.0` | Share of time at the configured maximum (percent, pressure window) at or above which the ceiling counts as saturated, a precondition of a max-cluster increase. | [PRD FR-OPT-005](../product/prd.md), [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_cluster_headroom_clusters` | `1` | Minimum growth room (clusters) kept above the observed peak when a max-cluster reduction is proposed. | [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_cluster_headroom_pct` | `25.0` | Growth room above the observed peak (percent of the peak) kept by a max-cluster reduction; headroom is the larger of `max_cluster_headroom_clusters` and this percent of the peak, rounded up. | [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_clusters_supported_limit` | `40` | Largest max-cluster value (clusters) the policy will recommend or accept; an increase at this ceiling is blocked. | [LLD §15.4 Max clusters](../architecture/lld.md), [LLD §18 Reviewer Gate 1 — candidate invariants](../architecture/lld.md), Gates [`MAX_CLUSTER_LIMIT_REACHED`](blocker-codes.md) |

### Min clusters

Time-at-floor and range limits for the min-cluster floor.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `time_at_min_pct_reduce` | `25.0` | Share of time at the configured minimum (percent, pressure window) at or above which a min-cluster reduction is considered. | [PRD FR-OPT-007](../product/prd.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`WAREHOUSE_RARELY_AT_MINIMUM`](blocker-codes.md) |
| `time_at_max_pct_block_min_reduce` | `20.0` | Share of time at the configured maximum (percent, pressure window) at or above which a min-cluster reduction is blocked. | [PRD FR-OPT-007](../product/prd.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`SUSTAINED_SCALE_PRESSURE`](blocker-codes.md) |
| `min_clusters_supported_limit` | `30` | Largest min-cluster value (clusters) the reviewer accepts in a candidate. | [LLD §18 Reviewer Gate 1 — candidate invariants](../architecture/lld.md) |

### Auto-stop

Cold-start guards, evidence minimums, activity classes and targets for the auto-stop timeout.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `cold_start_p95_ms_block` | `60000` | p95 compute wait among queries that waited (ms) at or above which, together with the start-rate limit, an auto-stop reduction is blocked as cold-start sensitive. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`COLD_START_SENSITIVE`](blocker-codes.md) |
| `cold_starts_per_active_day_block` | `0.5` | Warehouse starts per active day (starts/day) at or above which, together with the compute-wait limit, an auto-stop reduction is blocked as cold-start sensitive. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`COLD_START_SENSITIVE`](blocker-codes.md) |
| `min_idle_gap_sample_size` | `30` | Minimum number of measured idle gaps (count) in the current epoch before an auto-stop change is considered. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`INSUFFICIENT_IDLE_GAP_SAMPLE`](blocker-codes.md) |
| `min_cluster_minutes_for_cost_model` | `1.0` | Minimum current-epoch cluster-minutes (cluster-minutes) before an auto-stop change is considered. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`INSUFFICIENT_EVENT_RESOURCE_EVIDENCE`](blocker-codes.md) |
| `auto_stop_savings_cap` | `0.6` | Upper bound (fraction) on the share of cluster-minutes an auto-stop change is assumed to remove, in both the candidate estimate and the bundle resource track. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md) |
| `serverless_auto_stop_target_minutes` | `10` | Auto-stop target (minutes) for standard Serverless warehouses. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `low_activity_auto_stop_target_minutes` | `15` | Auto-stop target (minutes) for Pro/Classic warehouses below the low-activity query rate. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `medium_activity_auto_stop_target_minutes` | `30` | Auto-stop target (minutes) for Pro/Classic warehouses between the low and medium query rates. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `high_activity_auto_stop_target_minutes` | `45` | Auto-stop target (minutes) for Pro/Classic warehouses at or above the medium query rate. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `low_activity_queries_per_day` | `50.0` | Queries per active day (queries/day) below which a warehouse is low activity. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `medium_activity_queries_per_day` | `500.0` | Queries per active day (queries/day) below which a warehouse is medium activity; at or above it the warehouse is high activity. | [PRD FR-OPT-008](../product/prd.md), [LLD §15.6 Auto-stop](../architecture/lld.md) |

### Confidence

Deterministic evidence-quality score, levels and caps; confidence is not a probability of realizing savings.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `confidence_coverage_days` | `90.0` | Cost-history days (days) that earn full credit in the coverage component of candidate confidence. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_epoch_maturity_days` | `30.0` | Current-epoch age (days) that earns full credit in the epoch-maturity component of candidate confidence. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_base_weight` | `0.1` | Weight (fraction of the 0-1 score) of the constant base term in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_coverage_weight` | `0.25` | Weight (fraction of the 0-1 score) of the cost-history coverage component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_sample_weight` | `0.25` | Weight (fraction of the 0-1 score) of the query-sample component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_stability_weight` | `0.15` | Weight (fraction of the 0-1 score) of the cost-stability component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_epoch_weight` | `0.15` | Weight (fraction of the 0-1 score) of the epoch-maturity component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_signal_weight` | `0.1` | Weight (fraction of the 0-1 score) of the signal-strength component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_high_threshold` | `0.8` | Confidence score (0-1) at or above which the level is HIGH; applies to candidates, scenarios and the Photon observation. | [PRD FR-CONF-002](../product/prd.md), [LLD §16 Candidate confidence model](../architecture/lld.md), [Context §25 Confidence semantics](../architecture/context.md) |
| `confidence_medium_threshold` | `0.6` | Confidence score (0-1) at or above which the level is MEDIUM; below it the level is LOW. | [PRD FR-CONF-002](../product/prd.md), [LLD §16 Candidate confidence model](../architecture/lld.md), [Context §25 Confidence semantics](../architecture/context.md) |
| `confidence_signal_change` | `1.0` | Signal-strength input (0-1) for a candidate whose status is ACTIONABLE or TEST_REQUIRED. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_signal_no_change` | `0.7` | Signal-strength input (0-1) for every other candidate status. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_test_required` | `0.78` | Maximum confidence score (0-1) for a TEST_REQUIRED candidate. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_actionable` | `0.93` | Maximum confidence score (0-1) for an ACTIONABLE candidate. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_default` | `0.8` | Maximum confidence score (0-1) for candidates that are neither ACTIONABLE nor TEST_REQUIRED. | [LLD §16 Candidate confidence model](../architecture/lld.md) |

### Estimators and realization

Ranges used by the candidate estimator and the stateful bundle estimator; low, base and high bounds are ordered by savings, not by numeric value.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `realization_conservative` | `0.5` | Fraction of the removable idle cluster-minutes assumed removed by an auto-stop change in the conservative (low-savings) bound, in the candidate estimate and the bundle. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md), [LLD §20.1 Tracks](../architecture/lld.md) |
| `realization_base` | `0.75` | Fraction of the removable idle cluster-minutes assumed removed by an auto-stop change in the base case, in the candidate estimate and the bundle. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md), [LLD §20.1 Tracks](../architecture/lld.md) |
| `realization_optimistic` | `1.0` | Fraction of the removable idle cluster-minutes assumed removed by an auto-stop change in the optimistic (high-savings) bound, in the candidate estimate and the bundle. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md), [LLD §20.1 Tracks](../architecture/lld.md) |
| `min_cluster_savings_fraction_cap` | `0.5` | Upper bound (fraction) on the share of cluster-minutes a min-cluster reduction is assumed to remove. | [LLD §17.4 Min-cluster estimator](../architecture/lld.md) |
| `min_cluster_realization_optimistic` | `1.0` | Fraction of the capped time-at-minimum cluster-minutes assumed removed by a min-cluster reduction in the high-savings bound (an upper bound, since time at the floor does not prove a floor cluster was spare). | [LLD §17.4 Min-cluster estimator](../architecture/lld.md), [PRD FR-OPT-007](../product/prd.md) |
| `min_cluster_realization_base` | `0.5` | Fraction of the capped time-at-minimum cluster-minutes assumed removed by a min-cluster reduction in the base case (the policy midpoint). | [LLD §17.4 Min-cluster estimator](../architecture/lld.md), [PRD FR-OPT-007](../product/prd.md) |
| `min_cluster_realization_conservative` | `0.0` | Fraction of the capped time-at-minimum cluster-minutes assumed removed by a min-cluster reduction in the low-savings bound (no savings). | [LLD §17.4 Min-cluster estimator](../architecture/lld.md), [PRD FR-OPT-007](../product/prd.md) |
| `max_cluster_uplift_fraction_cap` | `0.5` | Upper bound (fraction) on the cost uplift a max-cluster increase is assumed to add. | [LLD §17.6 Max-cluster increase estimator](../architecture/lld.md) |
| `max_cluster_base_uplift_weight` | `0.5` | Weight (fraction) applied to the capped uplift to get the base-case cost uplift of a max-cluster increase. | [LLD §17.6 Max-cluster increase estimator](../architecture/lld.md) |
| `downsize_runtime_multiplier_conservative` | `1.55` | Runtime multiplier (ratio to current runtime) assumed after a size-down in the conservative case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `downsize_runtime_multiplier_base` | `1.3` | Runtime multiplier (ratio to current runtime) assumed after a size-down in the base case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `downsize_runtime_multiplier_optimistic` | `1.1` | Runtime multiplier (ratio to current runtime) assumed after a size-down in the optimistic case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `upsize_runtime_multiplier_conservative` | `1.0` | Runtime multiplier (ratio to current runtime) assumed after a size-up in the conservative case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `upsize_runtime_multiplier_base` | `0.7` | Runtime multiplier (ratio to current runtime) assumed after a size-up in the base case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `upsize_runtime_multiplier_optimistic` | `0.5` | Runtime multiplier (ratio to current runtime) assumed after a size-up in the optimistic case. | [LLD §17.5 Size estimator](../architecture/lld.md) |

### Epoch calibration

Use of prior configuration epochs to calibrate size estimates; historical epochs never satisfy decision thresholds.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `min_epoch_calibration_days` | `14` | Minimum billed days (days) a historical epoch needs to be used for matched-epoch calibration of a size estimate. | [LLD §10.2 Current-epoch rule](../architecture/lld.md), [LLD §17.5 Size estimator](../architecture/lld.md), [PRD FR-EPOCH-003](../product/prd.md) |
| `min_epoch_calibration_queries` | `100` | Minimum queries (count) a historical epoch needs to be used for matched-epoch calibration of a size estimate. | [LLD §10.2 Current-epoch rule](../architecture/lld.md), [LLD §17.5 Size estimator](../architecture/lld.md), [PRD FR-EPOCH-003](../product/prd.md) |
| `epoch_calibration_weight` | `0.5` | Weight (fraction) on the matched historical-epoch cost factor when blended with the modeled size factor; the modeled factor gets the remainder. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `epoch_factor_floor` | `0.4` | Lower clamp (ratio) on the historical-epoch cost-per-query factor before blending. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `epoch_factor_ceiling` | `1.6` | Upper clamp (ratio) on the historical-epoch cost-per-query factor before blending. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `cost_factor_floor` | `0.05` | Lower clamp (ratio to baseline cost) on the final size cost factor. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `cost_factor_ceiling` | `3.0` | Upper clamp (ratio to baseline cost) on the final size cost factor. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `cost_normalization_query_count` | `1000.0` | Query count (queries) used to normalize epoch cost into cost per 1,000 queries when comparing epochs. | [LLD §17.5 Size estimator](../architecture/lld.md) |

### Planner

Scenario-planner timing.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `remeasure_days_after_invalidating_change` | `7` | Days a plan step waits for fresh evidence after an upstream change invalidates it. | [PRD FR-PLAN-003](../product/prd.md), [LLD §19.3 Step state machine](../architecture/lld.md) |
