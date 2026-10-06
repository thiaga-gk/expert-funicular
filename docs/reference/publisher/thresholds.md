# Threshold posture

The defaults are **balanced-to-conservative overall** and are the balanced [cost profile](#cost-profiles). They are policy defaults, not universal Databricks recommendations. The complete list of every numeric default is in the [Parameter reference](#parameter-reference) section at the end of this page.

| Decision | Policy (defaults: Parameter reference below) | Posture | Why |
|---|---|---|---|
| Photon observed state | the state is inferred only with enough known-feature coverage (`photon_min_feature_coverage_pct`), enough current-epoch DBU (`photon_min_epoch_dbu`) and fresh usage (`photon_freshness_days`); `photon_enabled_usage_pct` and `photon_disabled_usage_pct` decide ENABLED or DISABLED, otherwise MIXED | Conservative | Observation only: reported in `current_config.photon`, never turned into a recommendation. Databricks publishes no Photon on/off price for SQL warehouses, so no savings can be modeled without guessing. |
| Size down | per cost profile: `strong_query_sample_size` executed queries (not served from the result cache; latency and spill are measured on them), spill at or below `spill_query_pct_downsize_safe`, queued share at or below `queue_query_pct_safe`, p95 and p99 execution at or below `p95_execution_ms_downsize_safe` and `p99_execution_ms_downsize_safe`, a ready configuration (`readiness_min_billed_days` settled billed days), no older epoch within 365 days that peaked at more clusters of demand, query volume not growing (`growth_hold_weekly_ratio`), no cluster peak in the current epoch above the recent peak plus headroom (`max_cluster_headroom_clusters`, `max_cluster_headroom_pct`), one size only, canary required; a batch warehouse (`batch_job_query_share_pct`) is judged on `batch_min_active_days` run days instead of the query sample and the latency limits | Conservative | Multiple independent safety signals; never auto-applies. |
| Size up | spill share at or above `spill_query_pct_upsize` and p95 spill **among spilled queries** at or above `spill_p95_bytes_upsize`, **and** at least `severity_min_affected_queries` spilled queries, `min_query_sample_size` executed queries and a ready configuration (`readiness_min_billed_days` settled billed days); **canary required (`TEST_REQUIRED`)**, one size only, and spilled bytes not concentrated in a few statement sources (`spill_concentration_hold_pct` of the top `spill_concentration_top_sources`, otherwise `NO_CHANGE`); otherwise `INSUFFICIENT_EVIDENCE` with no resize recommended | Balanced/conservative | Dual frequency+severity signal avoids reacting to isolated spills; sample/maturity gate stops a single query or an immature config regime from driving a resize. |
| Max clusters up | queued share at or above `queue_query_pct_increase_max`, p95 capacity wait **among queued queries** at or above `queue_wait_p95_ms_increase_max`, at least `severity_min_affected_queries` queued queries, saturation (`time_at_max_pct_saturated`), plus the sample and readiness gates (`readiness_min_billed_days` settled billed days); too few queued queries gives `INSUFFICIENT_EVIDENCE`. Pro/Classic then moves in one step to the ceiling peak query concurrency needs (`max_cluster_target_percentile` of busy minutes, `QUERIES_PER_CLUSTER_PRO_CLASSIC`, `max_cluster_target_headroom_clusters`; at least `concurrency_min_busy_minutes` busy minutes), and demand that already fits the current ceiling is `NO_CHANGE` | Conservative | Triple gate avoids over-scaling; the target comes from measured concurrency, not a fixed step. |
| Max clusters down | per cost profile: queued share at or below `queue_query_pct_safe`, time at the configured maximum at or below `time_at_max_pct_safe_reduce` (over `pressure_window_days` of the resource window), new ceiling below the current maximum: Pro/Classic from the larger of the recent peak concurrency and the whole current epoch's peak (the level reached in `peak_demand_min_minutes` busy minutes) at `QUERIES_PER_CLUSTER_PRO_CLASSIC` plus `max_cluster_target_headroom_clusters`, never below the recent window's observed cluster peak; Serverless = peak plus headroom (`max_cluster_headroom_clusters`, `max_cluster_cut_headroom_pct`) of the larger of the recent-window and whole-epoch cluster peaks; held when an older epoch within 365 days peaked at more clusters of demand | Conservative | Guardrail only; no fake direct savings. |
| Min clusters down | per cost profile: `min_query_sample_size` queries, a ready configuration (`readiness_min_billed_days`), time at the minimum at least `time_at_min_pct_reduce`, queued share at or below `queue_query_pct_safe`, p95 capacity wait among queued queries at or below `queue_wait_p95_ms_safe` (unknown wait severity blocks), time at the maximum below `time_at_max_pct_block_min_reduce` (over `pressure_window_days`; a pinned warehouse is exempt), at least `min_cluster_min_busy_periods` busy periods, the target floor being the demand busy periods start with (`min_cluster_busy_start_percentile`, `SCALE_UP_LAG_MINUTES_PRO_CLASSIC` / `SCALE_UP_LAG_MINUTES_SERVERLESS`) below the current floor, and no older epoch within 365 days that peaked at more clusters of demand; **canary required (`TEST_REQUIRED`)**; savings are the spare floor capacity measured on the DBU forecast's days (floor minutes whose demand needed fewer clusters, at `QUERIES_PER_CLUSTER_PRO_CLASSIC`), scaled by `realization_optimistic`, `realization_base` and `realization_conservative`, and withheld when that evidence is missing | Conservative | Spare capacity is modeled from per-minute demand, not proven; the canary confirms it before the change is kept. |
| Auto-stop | a ready configuration (`readiness_min_billed_days`); every `auto_stop_step_minutes` step from the type's floor (`AUTO_STOP_FLOOR_PRO_CLASSIC`, `AUTO_STOP_FLOOR_SERVERLESS`) to `AUTO_STOP_MAX_MINUTES`, scored on running idle time (idle gaps intersected with observed running time) per day on the DBU forecast's days and blend (`forecast_long_days`, `forecast_recent_days`, `forecast_weight_28_long`, `forecast_weight_28_mid`): idle cluster-minutes removed minus restarts added (or restarts saved minus idle added when raising; a value covers a gap only when it is `auto_stop_bridge_margin_pct` longer), each restart priced at `restart_cost_cluster_minutes_pro_classic` / `restart_cost_cluster_minutes_serverless` per floor cluster; the restarts a lower Pro/Classic value adds stay within `cold_start_added_share_max_pct` of queries (per cost profile) on the blend and in the recent window alone; the best net wins, and among values within `auto_stop_near_best_pct` of it the smallest change; a raise is a canary (`RESTART_BILLING_UNVERIFIED`); missing running-idle evidence gives `INSUFFICIENT_EVIDENCE`; savings realize `realization_*` of the idle a lower value removes, restarts and a raise's added idle in full | Balanced | Prices only idle time the warehouse actually ran, counts the restarts a change adds, and keeps added cold starts rare. |
| Canary rollback | per cost profile: within 30 days after a canary-type change (one size down or a lower minimum), a failure, queueing or (after a size-down) spill share above its safe level (`failure_cancel_pct_downsize_safe`, `queue_query_pct_safe`, `spill_query_pct_downsize_safe`) and more than `canary_p95_regression_pct` above the previous epoch's, or p95 execution on shared statement sources more than `canary_p95_regression_pct` above the previous epoch's with at least `canary_p95_min_queries` such queries | Conservative | Fail fast on the new epoch's own evidence: restore the previous value; no saving is claimed. |
| Type migration | compatibility gate + pre-canary modeled range + canary; the target is priced at its own cost-minimizing auto-stop (same rules as auto-stop); a migration whose best case still costs more than today is held (`MIGRATION_NOT_COST_EFFECTIVE`) | Conservative | IWM/runtime response is not assumed. |
| Failure guard on capacity cuts | failed/cancelled share above `failure_cancel_pct_downsize_safe`, or unreported, holds every capacity cut (size down, max/min clusters down, lower auto-stop) | Conservative | A cut on a warehouse that is already failing queries would add risk where the evidence is least trustworthy. Blockers `FAILURE_CANCEL_RATE_HIGH` / `FAILURE_CANCEL_EVIDENCE_UNAVAILABLE`. |
| Forward DBU forecast | published only when every basis day is priced, the current epoch has at least `min_economics_cost_history_days` billed (positive-cost) days and the run rate is positive (a periodic workload instead needs `forecast_min_cycles` whole billing cycles, and the billed-day floor applies to the run-rate model); otherwise null (never $0), and savings estimates and type migration are withheld | Conservative | A run rate from a few days, or a floor at zero, would guess the baseline every savings figure scales. Blocker `COST_HISTORY_INSUFFICIENT` (fewer settled basis days, `cost_history_days` (zero-filled settled calendar days in the forecast basis), than `min_economics_cost_history_days`), `BILLED_COST_DAYS_INSUFFICIENT` (mature epoch with too few billed days) or `DBU_COST_UNAVAILABLE`. |
| Growth guard on capacity cuts | per cost profile: the size downsize, max-cluster cut and min-cluster cut wait while the smallest of the last 4 complete weekly query counts is at least `growth_hold_weekly_ratio` times the median of the 4 weeks before; unknown with fewer than 8 complete weeks (nothing held) | Conservative | A cut sized on the recent past would soon be too small for a growing workload; using every recent week keeps one burst week from looking like growth. Blocker `QUERY_VOLUME_GROWING`. |
| Billing settle | a UTC day enters the cost basis `billing_settle_hours` after it ends and once its workspace's billing watermark has passed it | Conservative | Original billing records are typically available within 12 hours; the buffer keeps a still-arriving day out of the run rate. |
| Plan step order | base savings × confidence ÷ risk, a canary step weighted `plan_risk_canary_change` and a human-approval step `plan_risk_hitl_change`; a canary rollback goes first; otherwise a step never precedes its invalidator, and steps another step invalidates follow every ready step | Balanced | The largest, best-evidenced and least disruptive saving goes first; a canary costs a measurement cycle. |
| Pressure window | `pressure_window_days`: time at min, time at max and observed peak come from the last `pressure_window_days` days of the daily resource window | Balanced | Capacity facts describe recent behavior on the same calendar as the DBU forecast rather than the whole epoch. Without the series, max/min-cluster changes are held with `RECENT_RESOURCE_WINDOW_UNAVAILABLE` (series not collected) or `NO_RECENT_RUNNING_TIME` (collected, but the warehouse did not run in the window). |

## Missing pressure evidence fails closed

Spill frequency, queue frequency and p95/p99 execution latency stay null when the collector did not report them; they are never read as 0 ("no pressure"), and findings report them as null.

- Size down is held (`NO_CHANGE`) with `SPILL_EVIDENCE_UNAVAILABLE`, `QUEUE_EVIDENCE_UNAVAILABLE` or `LATENCY_EVIDENCE_UNAVAILABLE`.
- Max-cluster reduction is held with `QUEUE_EVIDENCE_UNAVAILABLE`, or `QUEUE_WAIT_EVIDENCE_UNAVAILABLE` when queries queued but their capacity-wait percentile is missing.
- Min-cluster reduction is held with `QUEUE_EVIDENCE_UNAVAILABLE`, or `QUEUE_WAIT_EVIDENCE_UNAVAILABLE` when queries queued but their capacity-wait percentile is missing.
- Size up and max-cluster increase need known spill / queue evidence.

## Severity is measured among affected queries

Spill, capacity-wait and compute-wait severity percentiles are computed **only over the queries that were affected** (`spilled_local_bytes > 0`, `waiting_at_capacity_duration_ms > 0`, `waiting_for_compute_duration_ms > 0`), and the affected-query counts (`spill_queries_30`, `queued_queries_30`, `compute_wait_queries_30`) are collected beside them (compute wait is collected as evidence only; no decision reads it). A percentile over all queries hides rare-but-severe events behind the unaffected majority.

- Rules that add capacity/cost (size up, max clusters up) require `severity_min_affected_queries` affected queries; below that a p95 is just the maximum observation.
- Guard rules that block savings (the min-cluster reduction queue guard) fire on any affected queries.
- If the collector did not report the affected-query count (older evidence), severity is *unknown*, never zero: min-cluster reduction is blocked (`QUEUE_WAIT_EVIDENCE_UNAVAILABLE`). Size-up and max-cluster increase report `INSUFFICIENT_EVIDENCE` (`SPILL_SEVERITY_EVIDENCE_UNAVAILABLE`, `QUEUE_WAIT_EVIDENCE_UNAVAILABLE`) when their frequency threshold is crossed but severity is unknown, instead of a silent no-change.

## Cost-estimator guardrail

Serverless size economics may use the published SQL Serverless DBU/hour schedule. Pro/Classic size economics must **not** reuse that schedule. The publisher prices a Pro/Classic size change from the workspace's billed DBU per cluster-hour only when the rates rise with size and the pair ratio stays within `size_rate_published_band` (25%) of the published per-size ratio; otherwise the estimate is withheld (`PRO_CLASSIC_SIZE_RATE_UNVERIFIED`) and the change needs an A/B canary. There is no nominal capacity-ratio fallback. A change made to save money is held as `NO_CHANGE` when its own base savings are not positive (`COST_CHANGE_NOT_COST_EFFECTIVE`) or are below `cost_change_min_annual_savings` (500 a year, `COST_SAVINGS_BELOW_MATERIALITY`). A warehouse-type migration is priced relative to billing: the same model applied to the current warehouse must land within 0.70 to 1.30 of its billed DBU forecast (`migration_reconciliation_min` / `migration_reconciliation_max`), otherwise the estimate is withheld (`MIGRATION_MODEL_NOT_RECONCILED`).

## Tuning recommendation

Keep these defaults for the first real run. Review false positives/negatives across 10–20 representative warehouses before changing them. Prefer a cost profile per warehouse (below) over globally loosening safety gates. The conservative and aggressive values are starting points, not calibrated on live data; calibrating them is part of the real-data run (`docs/proposals/real-data-run.md`).

## Cost profiles

Every warehouse is evaluated under one cost profile: **aggressive** (highest savings, most performance risk), **balanced** (the defaults on this page) or **conservative** (lowest performance risk). A warehouse whose current-epoch tags mark it nonprod (`nonprod_tag_keys`, `nonprod_tag_values`) gets aggressive; every other warehouse gets balanced. `--cost-profile PROFILE` overrides that for every warehouse and `--cost-profile PROFILE=WH[,WH...]` (a warehouse ID or `workspace_id/warehouse_id`) for the listed ones; a listed warehouse wins over an all-warehouse flag, which wins over the tag. A listed warehouse that was not collected refuses the run. The package records the profile, why it was chosen and the values it applied (`cost_profile`, Reporter JSON Contract section 17.1).

A profile changes only how much performance risk a cut that the evidence supports may take: the thresholds below. Evidence minimums, product constants, estimator and forecast parameters, confidence, classification rules, the failure-cancel guard and the rules that add capacity (size up, max clusters up) are the same under every profile, so a candidate with the same recommended value has the same economics under every profile. The one exception is the Pro target of a Serverless-to-Pro migration, priced at its best auto-stop within the profile's cold-start budget.

| Technique | Threshold | Conservative | Balanced | Aggressive | Why it moves |
|---|---|---|---|---|---|
| size | `spill_query_pct_downsize_safe` | `0.05` | `0.1` | `0.25` | More spilling queries tolerated before a size down is held. |
| size | `p95_execution_ms_downsize_safe` | `3000` | `5000` | `10000` | Slower typical queries tolerated before a size down is held. |
| size | `p99_execution_ms_downsize_safe` | `20000` | `30000` | `60000` | Slower worst-case queries tolerated before a size down is held. |
| max_clusters | `time_at_max_pct_safe_reduce` | `0.5` | `1.0` | `2.0` | More time at the ceiling tolerated before a ceiling cut is held. |
| max_clusters | `max_cluster_cut_headroom_pct` | `35.0` | `25.0` | `15.0` | Less growth room kept above the peak by a Serverless ceiling cut. |
| min_clusters | `time_at_min_pct_reduce` | `40.0` | `25.0` | `15.0` | Less time at the floor needed before a floor cut is considered. |
| min_clusters | `queue_wait_p95_ms_safe` | `2000` | `5000` | `10000` | Longer capacity waits tolerated before a floor cut is held. |
| min_clusters | `min_cluster_busy_start_percentile` | `99.0` | `95.0` | `90.0` | The floor carries a smaller share of busy-period starts. |
| auto_stop | `cold_start_added_share_max_pct` | `1.0` | `2.0` | `10.0` | More queries may meet a restart a lower auto-stop adds. |
| cross_technique | `cost_change_min_annual_savings` | `1000.0` | `500.0` | `250.0` | Smaller savings are worth a change. |
| cross_technique | `queue_query_pct_safe` | `0.25` | `0.5` | `1.0` | More queued queries tolerated before a cut is held (size, max and min clusters, canary rollback). |
| cross_technique | `canary_p95_regression_pct` | `15.0` | `25.0` | `40.0` | A larger regression after a canary-type change before it is rolled back. |
| cross_technique | `growth_hold_weekly_ratio` | `1.05` | `1.10` | `1.20` | Faster query growth tolerated before capacity cuts wait. |

Each profile keeps every safe level below the level that adds capacity (for example `queue_query_pct_safe` below `queue_query_pct_increase_max`); a test checks this, and that this table equals the code (`src/publisher/governor/cost_profiles.py`). Changing a profile value is a policy change: bump the policy version and regenerate the goldens.

## Parameter reference

Every field of `Thresholds` in `src/publisher/governor/thresholds.py`, one row per field, grouped by decision area. Databricks product facts the rules also use (scale-up lag, auto-stop floors and maximum, cluster limits, queries per cluster) are not policy and are listed in [product-constants.md](product-constants.md); upper-case names below refer to them. Each default is the dataclass default; a test compares the two. The meaning is taken from how the publisher uses the field. Warehouse-type migration has no dedicated parameter: it is gated by `min_economics_cost_history_days` and priced with the estimator parameters below. Changing any decision-affecting default requires a policy version change and golden-test updates.

### Evidence sufficiency and cost history

Minimum samples and history before any decision or estimate is published.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `min_query_sample_size` | `100` | Minimum number of queries in the 30-day window (count) before any size, max-cluster or min-cluster change is considered; also gates the workload-profile latency and concurrency classes and the evidence-quality status. | [PRD FR-OPT-003](../product/prd.md), [PRD FR-OPT-007](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`INSUFFICIENT_QUERY_SAMPLE`](blocker-codes.md) |
| `strong_query_sample_size` | `500` | Query count in the 30-day window (count) required for a size-down and the full-credit point of the sample component of candidate confidence. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §16 Candidate confidence model](../architecture/lld.md), Gates [`INSUFFICIENT_STRONG_QUERY_SAMPLE`](blocker-codes.md) |
| `min_cost_history_days` | `14` | Settled cost-history basis days (days; zero-filled settled calendar days in the forecast basis) the current epoch needs for the package evidence-quality assessment to count cost history as sufficient; it does not gate any forecast or recommendation. | code only |
| `min_economics_cost_history_days` | `7` | Minimum days (days) before a forward DBU forecast, any savings estimate or a cost-judged warehouse-type migration is published: billed (positive-cost) days for the forecast status (`INSUFFICIENT_ACTIVE_COST_DAYS`), settled basis days for `COST_HISTORY_INSUFFICIENT`; also the cost-history re-evaluation gate length, which must equal the locked contract constant. | [PRD §14.1 Economic concepts](../product/prd.md), [LLD §11.2 Daily run rate](../architecture/lld.md), [LLD §15.7 Warehouse type](../architecture/lld.md), Gates [`COST_HISTORY_INSUFFICIENT`](blocker-codes.md), [`BILLED_COST_DAYS_INSUFFICIENT`](blocker-codes.md), [`DBU_COST_UNAVAILABLE`](blocker-codes.md) |
| `readiness_min_billed_days` | `2` | Settled billed days (days with billed usage) the current configuration epoch needs before a size, max-cluster, min-cluster or auto-stop change is decided (the configuration is ready); also the readiness re-evaluation gate length, which must equal the locked contract constant. | [PRD FR-EPOCH-004](../product/prd.md), [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`CURRENT_CONFIG_EPOCH_TOO_NEW`](blocker-codes.md) |
| `canary_p95_regression_pct` | `25.0` | Rise (%) over the previous epoch above which a canary-type change (one size down or a lower minimum) is rolled back: of p95 execution time on statement sources seen in both epochs, and, together with its safe level, of the failure/cancel, queued and (after a size-down) spilled share. | [PRD FR-EPOCH-005](../product/prd.md), Gates [`CANARY_P95_REGRESSION`](blocker-codes.md) |
| `canary_p95_min_queries` | `300` | Executed queries (count) on shared statement sources in the current epoch the p95 rollback guard needs (fewer: the guard does not judge). | [PRD FR-EPOCH-005](../product/prd.md), Gates [`CANARY_P95_REGRESSION`](blocker-codes.md) |
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
| `forecast_cadence_gap_days` | `7` | Rest (days) above which a workload is periodic: runs of billed days no longer than this (median calendar length) that rest longer than this between them (median idle gap) use the cadence model (whole billing cycles); a median rest of exactly this (weekly) uses the cadence model only when the run-rate model has no number. Also the slack (days) beyond one reference cycle (the upper quartile of cycles, or the only cycle) within which the last run must have started for the cadence to count as current. | [LLD §11.2 Daily run rate](../architecture/lld.md), [LLD §12.2 Pro/Classic](../architecture/lld.md) |
| `forecast_run_bridge_days` | `3` | Largest gap (days) between billed days that still belong to one run, so a weekend inside a month-end or quarter-end close does not split it into two runs (which would shorten the cycles the cadence model reads); run lengths and rests are measured on these runs, and a run starting within this many days of the basis start is not a cycle anchor (it may continue before the basis). | [LLD §11.2 Daily run rate](../architecture/lld.md) |
| `forecast_min_cycles` | `2` | Whole run-to-run billing cycles (count; that is, one more run) the cadence model needs before it publishes a forward DBU cost; also the covered cycles the AWS model needs under the cadence model. | [LLD §11.2 Daily run rate](../architecture/lld.md), Gates [`INSUFFICIENT_BILLING_CYCLES`](blocker-codes.md), [`CADENCE_CYCLES_NOT_COVERED_BY_EVENTS`](blocker-codes.md) |
| `billing_settle_hours` | `24` | Hours after a UTC day ends before its billing counts toward the cost basis (hours), in addition to the workspace billing watermark having passed it. | [LLD §11.1 Basis](../architecture/lld.md), [Context §8.2 Source latency is not data quality failure](../architecture/context.md), [HLD §6.1 Analyzer: collector](../architecture/hld.md) |
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
| `queue_query_pct_safe` | `0.5` | Largest share of queries that queued (percent) still treated as low queue pressure for a size down, max-cluster reduction or min-cluster reduction; also the STEADY boundary of the workload profile, which always uses the balanced value so the profile reads the same under every cost profile. | [PRD FR-OPT-003](../product/prd.md), [PRD FR-OPT-007](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), [LLD §15.4 Max clusters](../architecture/lld.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`QUEUE_PRESSURE_PRESENT`](blocker-codes.md) |
| `queue_wait_p95_ms_safe` | `5000` | Largest p95 capacity wait among queued queries (ms) still treated as safe for a min-cluster reduction. | [PRD FR-OPT-007](../product/prd.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`QUEUE_PRESSURE_PRESENT`](blocker-codes.md), [`QUEUE_WAIT_EVIDENCE_UNAVAILABLE`](blocker-codes.md) |

### Warehouse size

Spill, latency and failure limits for size up and size down.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `spill_query_pct_upsize` | `2.0` | Share of queries that spilled to local disk (percent) at or above which an upsize is considered. | [PRD FR-OPT-004](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `spill_p95_bytes_upsize` | `1073741824` | p95 spilled bytes among spilled queries (bytes; 1 GiB) at or above which an upsize is considered. | [PRD FR-OPT-004](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `spill_query_pct_downsize_safe` | `0.1` | Largest share of queries that spilled (percent) still treated as safe for a size down. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`SPILL_PRESENT`](blocker-codes.md) |
| `spill_concentration_top_sources` | `3` | Number of top statement sources whose share of spilled bytes is measured. | [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `spill_concentration_hold_pct` | `80.0` | Share of spilled bytes (%) from those top sources at or above which an upsize is held for a rewrite. | [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`SPILL_CONCENTRATED_IN_FEW_STATEMENTS`](blocker-codes.md) |
| `failure_cancel_pct_downsize_safe` | `5.0` | Largest share of failed or cancelled queries (percent) before every capacity cut (size down, lower max or min clusters, lower auto-stop) is held. | [LLD §15 Optimizer decision algorithms](../architecture/lld.md), Gates [`FAILURE_CANCEL_RATE_HIGH`](blocker-codes.md), [failure guard row](thresholds.md) |
| `p95_execution_ms_downsize_safe` | `5000` | Largest p95 query execution time (ms) still treated as safe for a size down. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`P95_LATENCY_TOO_HIGH`](blocker-codes.md) |
| `p99_execution_ms_downsize_safe` | `30000` | Largest p99 query execution time (ms) still treated as safe for a size down. | [PRD FR-OPT-003](../product/prd.md), [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`P99_LATENCY_TOO_HIGH`](blocker-codes.md) |
| `batch_job_query_share_pct` | `80.0` | Share of last-30-day queries from jobs (%) at or above which a warehouse is batch: its downsize canary skips the interactive latency gates and uses run days as the sample. | [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `batch_min_active_days` | `20` | Active days in the last 30 a batch warehouse needs before a downsize canary. | [LLD §15.3 Warehouse size](../architecture/lld.md), Gates [`INSUFFICIENT_BATCH_RUN_DAYS`](blocker-codes.md) |

### Max clusters

Saturation, headroom and range limits for the max-cluster ceiling.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `time_at_max_pct_safe_reduce` | `1.0` | Largest share of time at the configured maximum (percent, pressure window) still allowing a max-cluster reduction. | [PRD FR-OPT-006](../product/prd.md), [LLD §15.4 Max clusters](../architecture/lld.md) |
| `time_at_max_pct_saturated` | `5.0` | Share of time at the configured maximum (percent, pressure window) at or above which the ceiling counts as saturated, a precondition of a max-cluster increase. | [PRD FR-OPT-005](../product/prd.md), [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_cluster_headroom_clusters` | `1` | Minimum growth room (clusters) kept above the observed peak when a max-cluster reduction is proposed. | [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_cluster_headroom_pct` | `25.0` | Growth room above the recent peak (percent of the peak) that the size-down hold for a peak outside the recent window (`PEAK_OUTSIDE_RECENT_WINDOW`) compares the epoch peak with; headroom is the larger of `max_cluster_headroom_clusters` and this percent of the peak, rounded up. Not cost-profiled. | [LLD §15.3 Warehouse size](../architecture/lld.md) |
| `max_cluster_cut_headroom_pct` | `25.0` | Growth room above the observed peak (percent of the peak) a Serverless max-cluster reduction keeps; headroom is the larger of `max_cluster_headroom_clusters` and this percent of the peak, rounded up. Cost-profiled. | [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_cluster_target_percentile` | `99.5` | Percentile of the recent window's busy minutes' running + queued queries that sets the Pro/Classic peak demand (the whole current epoch uses the level reached in `peak_demand_min_minutes` busy minutes). | [LLD §15.4 Max clusters](../architecture/lld.md) |
| `max_cluster_target_headroom_clusters` | `1` | Clusters added above peak demand for the Pro/Classic ceiling target. | [LLD §15.4 Max clusters](../architecture/lld.md) |
| `min_cluster_busy_start_percentile` | `95.0` | Percentile of busy periods' peak demand in their first scale-up-lag minutes that the floor must carry. | [LLD §15.5 Min clusters](../architecture/lld.md) |
| `concurrency_min_busy_minutes` | `200` | Busy minutes in the recent window needed before a peak-demand ceiling target is used. | [LLD §15.4 Max clusters](../architecture/lld.md), Gates [`INSUFFICIENT_BUSY_MINUTE_SAMPLE`](blocker-codes.md) |
| `peak_demand_min_minutes` | `30` | Long windows (the whole current epoch, older epochs) measure peak demand as the level of running + queued queries reached in at least this many busy minutes. | [LLD §15.4 Max clusters](../architecture/lld.md), Gates [`OLDER_EPOCH_PEAK_DEMAND_HIGHER`](blocker-codes.md) |
| `min_cluster_min_busy_periods` | `20` | Busy periods in the recent window needed before a floor target is used. | [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`INSUFFICIENT_BUSY_PERIOD_SAMPLE`](blocker-codes.md) |

### Min clusters

Time-at-floor and range limits for the min-cluster floor.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `time_at_min_pct_reduce` | `25.0` | Share of time at the configured minimum (percent, pressure window) at or above which a min-cluster reduction is considered. | [PRD FR-OPT-007](../product/prd.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`WAREHOUSE_RARELY_AT_MINIMUM`](blocker-codes.md) |
| `time_at_max_pct_block_min_reduce` | `20.0` | Share of time at the configured maximum (percent, pressure window) at or above which a min-cluster reduction is blocked. | [PRD FR-OPT-007](../product/prd.md), [LLD §15.5 Min clusters](../architecture/lld.md), Gates [`SUSTAINED_SCALE_PRESSURE`](blocker-codes.md) |

### Auto-stop

Search range, restart cost, cold-start budget and the nonprod tag for the auto-stop timeout.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `min_cluster_minutes_for_cost_model` | `1.0` | Minimum current-epoch cluster-minutes (cluster-minutes) before an auto-stop change is considered. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`INSUFFICIENT_EVENT_RESOURCE_EVIDENCE`](blocker-codes.md) |
| `auto_stop_step_minutes` | `5` | Step (minutes) between evaluated auto-stop values; the running-idle collector measures every step from the lower floor to `AUTO_STOP_MAX_MINUTES`. | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `auto_stop_bridge_margin_pct` | `10.0` | A value covers an idle gap (keeps the warehouse up until the next query, so no restart) only when it is at least this much (%) longer than the gap. | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `running_idle_full_window_fraction` | `0.99` | Share of an idle span the warehouse must have been running for a lower auto-stop to count an added restart there (and below which a gap counts as stopped). | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `kept_alive_gap_tolerance_minutes` | `2.0` | Minutes a warehouse may run past the current auto-stop, in a gap it ran through to the next query, before the gap counts as kept alive by non-query activity (stop events can trail the auto-stop); a kept-alive gap contributes no removable time and no added restart. | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `kept_alive_stopped_gap_minutes` | `10.0` | Minutes past the current auto-stop after which a gap the warehouse stopped in before the next query still counts as kept alive (a session held it open); shorter overruns are a late stop event. | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `restart_cost_cluster_minutes_pro_classic` | `4.0` | Priced cost of one Pro/Classic restart (cluster-minutes per floor cluster): the documented start time of about 4 minutes, assuming the starting minutes are billed (not documented). | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`RESTART_BILLING_UNVERIFIED`](blocker-codes.md) |
| `restart_cost_cluster_minutes_serverless` | `0.1` | Priced cost of one Serverless restart (cluster-minutes per floor cluster): the upper end of the documented 2–6 second start. | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `cold_start_added_share_max_pct` | `2.0` | Largest share of queries (%) that may meet a restart a lower Pro/Classic auto-stop adds (also the Pro target of a Serverless-to-Pro migration). Cost-profiled. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`COLD_START_BUDGET_EXCEEDED`](blocker-codes.md) |
| `auto_stop_near_best_pct` | `2.0` | Auto-stop values whose net saving is within this share (%) of the best count as equally good; the smallest change among them is chosen. | [LLD §15.6 Auto-stop](../architecture/lld.md) |
| `nonprod_tag_keys` | `("env", "environment", "stage", "tier")` | Warehouse tag keys (case-insensitive) that mark the environment. | [Cost profiles](#cost-profiles) |
| `nonprod_tag_values` | `("dev", "development", "test", "testing", "qa", "sandbox", "nonprod", "non-prod")` | Values of those keys that make a warehouse nonprod, which selects the aggressive cost profile by default; staging and UAT stay production. | [Cost profiles](#cost-profiles) |
| `kept_alive_flag_pct` | `10.0` | Running time after the current auto-stop with no query running, as a share of running time (%), at or above which the warehouse is flagged as kept alive by non-query activity. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`ACTIVITY_WITHOUT_QUERIES_KEEPS_WAREHOUSE_RUNNING`](blocker-codes.md) |
| `always_on_running_pct` | `90.0` | Share of the window (%) a warehouse must run to be flagged as kept up by tiny, frequent queries. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING`](blocker-codes.md) |
| `always_on_busy_pct_max` | `10.0` | Largest share of running time (%) with a query running for that flag. | [LLD §15.6 Auto-stop](../architecture/lld.md), Gates [`SHORT_QUERIES_KEEP_WAREHOUSE_RUNNING`](blocker-codes.md) |

### Confidence

Deterministic evidence-quality score, levels and caps; confidence is not a probability of realizing savings.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `confidence_coverage_days` | `90.0` | Cost-history days (days) that earn full credit in the coverage component of candidate confidence. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_epoch_maturity_days` | `30.0` | Current-epoch age (days) that earns full credit in the epoch-maturity component of candidate confidence. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_base_weight` | `0.1` | Weight (fraction of the 0-1 score) of the constant base term in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_coverage_weight` | `0.25` | Weight (fraction of the 0-1 score) of the cost-history coverage component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_sample_weight` | `0.25` | Weight (fraction of the 0-1 score) of the sample component (the sample the knob's decision is made on) in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_stability_weight` | `0.15` | Weight (fraction of the 0-1 score) of the cost-stability component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_epoch_weight` | `0.15` | Weight (fraction of the 0-1 score) of the epoch-maturity component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_signal_weight` | `0.1` | Weight (fraction of the 0-1 score) of the signal-strength component in the candidate confidence score; the six weights sum to 1.0. | [LLD §16 Candidate confidence model](../architecture/lld.md), [PRD FR-CONF-001](../product/prd.md) |
| `confidence_high_threshold` | `0.8` | Confidence score (0-1) at or above which the level is HIGH; applies to candidates, scenarios and the Photon observation. | [PRD FR-CONF-002](../product/prd.md), [LLD §16 Candidate confidence model](../architecture/lld.md), [Context §25 Confidence semantics](../architecture/context.md) |
| `confidence_medium_threshold` | `0.6` | Confidence score (0-1) at or above which the level is MEDIUM; below it the level is LOW. | [PRD FR-CONF-002](../product/prd.md), [LLD §16 Candidate confidence model](../architecture/lld.md), [Context §25 Confidence semantics](../architecture/context.md) |
| `confidence_signal_change` | `1.0` | Signal-strength input (0-1) for a candidate whose status is ACTIONABLE or TEST_REQUIRED. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_signal_no_change` | `0.7` | Signal-strength input (0-1) for every other candidate status. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_test_required` | `0.78` | Maximum confidence score (0-1) for a TEST_REQUIRED candidate. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_actionable` | `0.93` | Maximum confidence score (0-1) for an ACTIONABLE candidate. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_strong_busy_minutes` | `1000` | Busy minutes in the recent window (minutes) that earn full credit in the sample component of a max-cluster candidate's confidence. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_strong_busy_periods` | `100` | Busy periods in the recent window (count) that earn full credit in the sample component of a min-cluster candidate's confidence. | [LLD §16 Candidate confidence model](../architecture/lld.md) |

Derived values. These are not fields: each is computed from the cut-offs above, so a cap can never drift above the level it keeps a candidate below (a test compares each with the code).

| Value | Computed as | Description | Used by |
|---|---|---|---|
| `confidence_cap_default` | `confidence_high_threshold` − 0.01 (0.79) | Maximum confidence score (0-1) for candidates that are neither ACTIONABLE, TEST_REQUIRED nor INSUFFICIENT_EVIDENCE: below HIGH, so a decision that changes nothing is never HIGH. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_insufficient_evidence` | `confidence_medium_threshold` − 0.01 (0.59) | Maximum confidence score (0-1) for an INSUFFICIENT_EVIDENCE candidate: below MEDIUM, so it shows as LOW. | [LLD §16 Candidate confidence model](../architecture/lld.md) |
| `confidence_cap_serverless_ceiling_increase` | `confidence_medium_threshold` − 0.01 (0.59) | Confidence cap for a Serverless max-cluster increase (queue-based, one step): below MEDIUM, so it shows as LOW. | [LLD §15.4 Max clusters](../architecture/lld.md) |

The six `confidence_*_weight` fields must sum to 1.0; `Thresholds` refuses to build otherwise.

### Estimators and realization

Ranges used by the candidate estimator and the stateful bundle estimator; low, base and high bounds are ordered by savings, not by numeric value.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `realization_conservative` | `0.5` | Fraction of the idle running cluster-minutes a lower auto-stop removes, and of the spare floor capacity a lower min-cluster floor removes, assumed realized in the conservative (low-savings) bound, in the candidate estimate and the bundle. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md), [LLD §17.4 Min-cluster estimator](../architecture/lld.md), [LLD §20.1 Tracks](../architecture/lld.md) |
| `realization_base` | `0.75` | Fraction of the auto-stop idle removed and of the spare floor capacity assumed realized in the base case, in the candidate estimate and the bundle. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md), [LLD §17.4 Min-cluster estimator](../architecture/lld.md), [LLD §20.1 Tracks](../architecture/lld.md) |
| `realization_optimistic` | `1.0` | Fraction of the auto-stop idle removed and of the spare floor capacity assumed realized in the optimistic (high-savings) bound, in the candidate estimate and the bundle. | [LLD §17.3 Auto-stop estimator](../architecture/lld.md), [LLD §17.4 Min-cluster estimator](../architecture/lld.md), [LLD §20.1 Tracks](../architecture/lld.md) |
| `max_cluster_base_uplift_weight` | `0.5` | Weight (fraction) applied to the worst-case uplift (minutes at the ceiling x added clusters / cluster-minutes, uncapped) to get the base-case cost uplift of a max-cluster increase. | [LLD §17.6 Max-cluster increase estimator](../architecture/lld.md) |
| `downsize_runtime_multiplier_conservative` | `2.0` | Runtime multiplier (ratio to current query runtime, applied to the busy share of running time) assumed after a size-down in the conservative case: one size down halves the workers per cluster, so a fully linear slowdown doubles query runtime. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `downsize_runtime_multiplier_base` | `1.3` | Runtime multiplier (ratio to current query runtime, applied to the busy share of running time) assumed after a size-down in the base case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `downsize_runtime_multiplier_optimistic` | `1.1` | Runtime multiplier (ratio to current query runtime, applied to the busy share of running time) assumed after a size-down in the optimistic case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `upsize_runtime_multiplier_conservative` | `1.0` | Runtime multiplier (ratio to current query runtime, applied to the busy share of running time) assumed after a size-up in the conservative case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `upsize_runtime_multiplier_base` | `0.7` | Runtime multiplier (ratio to current query runtime, applied to the busy share of running time) assumed after a size-up in the base case. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `upsize_runtime_multiplier_optimistic` | `0.5` | Runtime multiplier (ratio to current query runtime, applied to the busy share of running time) assumed after a size-up in the optimistic case. | [LLD §17.5 Size estimator](../architecture/lld.md) |

### Size price factor and value gate

Clamps on the size cost factor, the check that a workspace's billed rates can price a Pro/Classic size change, the floor below which a cost-saving change is held, and the band a migration estimate must reproduce the billed forecast within. Historical epochs never satisfy decision thresholds.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `size_rate_published_band` | `0.25` | Largest relative difference (fraction) between a workspace's billed Pro/Classic DBU-per-cluster-hour ratio for a size pair and the published per-size ratio; outside it the size estimate is withheld. | [LLD §17.5 Size estimator](../architecture/lld.md), Gates [`PRO_CLASSIC_SIZE_RATE_UNVERIFIED`](blocker-codes.md) |
| `cost_change_min_annual_savings` | `500.0` | Annual base savings (warehouse currency) a change made to save money must reach to be published; below it, or at or below zero, the change is held as `NO_CHANGE`. | [LLD §17.1 General range convention](../architecture/lld.md), Gates [`COST_SAVINGS_BELOW_MATERIALITY`](blocker-codes.md), [`COST_CHANGE_NOT_COST_EFFECTIVE`](blocker-codes.md) |
| `migration_reconciliation_min` | `0.7` | Lowest ratio (modeled / billed) the migration cost model may show when applied to the current warehouse; below it the migration estimate is withheld. | [LLD §17.7 Migration estimator](../architecture/lld.md), Gates [`MIGRATION_MODEL_NOT_RECONCILED`](blocker-codes.md) |
| `migration_reconciliation_max` | `1.3` | Highest ratio (modeled / billed) the migration cost model may show when applied to the current warehouse; above it the migration estimate is withheld. | [LLD §17.7 Migration estimator](../architecture/lld.md), Gates [`MIGRATION_MODEL_NOT_RECONCILED`](blocker-codes.md) |
| `cost_factor_floor` | `0.05` | Lower clamp (ratio to baseline cost) on the final size cost factor. | [LLD §17.5 Size estimator](../architecture/lld.md) |
| `cost_factor_ceiling` | `3.0` | Upper clamp (ratio to baseline cost) on the final size cost factor. | [LLD §17.5 Size estimator](../architecture/lld.md) |

### Planner

Scenario-planner timing and step order.

| Parameter | Default | Description | Used by |
|---|---|---|---|
| `remeasure_days_after_invalidating_change` | `7` | Days a plan step waits for fresh evidence after an upstream change invalidates it. | [PRD FR-PLAN-003](../product/prd.md), [LLD §19.3 Step state machine](../architecture/lld.md) |
| `growth_hold_weekly_ratio` | `1.10` | Capacity cuts (downsize, max-cluster cut, min-cluster cut) wait while every one of the last 4 complete weeks has at least this many times the median weekly query count of the 4 weeks before; unknown (not held) when the epoch has fewer than 8 complete weeks. | [PRD FR-OPT-003](../product/prd.md), [LLD §15 Optimizer decision algorithms](../architecture/lld.md), Gates [`QUERY_VOLUME_GROWING`](blocker-codes.md) |
| `plan_risk_hitl_change` | `1.0` | Risk divisor for an `ACTIONABLE` (human-approved) step when plan steps are ordered by base savings × confidence / risk. | [PRD FR-PLAN-001](../product/prd.md), [LLD §19.1 Plan A](../architecture/lld.md) |
| `plan_risk_canary_change` | `2.0` | Risk divisor for a `TEST_REQUIRED` (canary) step in the same plan ordering; a canary costs a measurement cycle and a rollback path. | [PRD FR-PLAN-001](../product/prd.md), [LLD §19.1 Plan A](../architecture/lld.md) |
