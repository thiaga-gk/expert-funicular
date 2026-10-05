# Golden scenarios for SQL warehouse cost optimization

Status: **Living table.** Rows are updated with each fix; pass/fail reflects the code at the time of the last update. Finding IDs appear only in the Tracker column; details and live status are in `docs/proposals/gap-review-2.8.0.md` and the tracker (https://claude.ai/artifact/TMmDwx8SBk2ZuzCQbrzr1w).

- **In scope:** yes = the optimizer should decide it (size, min/max clusters, auto-stop, warehouse type); no = observe, flag or ignore only.
- **Code coverage:** covered = the code has a rule or evidence path for the scenario; not covered = none.
- **Status:** pass = the code behaves as expected (by code reading or a constructed run on mutated demo evidence); fail = it does not.
- **Tracker:** finding IDs that change the behaviour; — when nothing needs to change.

The `--demo` fleet has one simulated warehouse per scenario listed in `publisher.DEMO_FLEET` (config tag `golden_scenario`); `tests/test_demo_fleet.py` asserts the passing rows it contains.

| ID | Scenario | Signature | Expected behavior | In scope | Code coverage | Status | Tracker |
|---|---|---|---|---|---|---|---|
| C01 | Always-on idle | `warehouses.auto_stop_minutes = 0`; billed hours ≫ `query.history` busy hours | Auto-stop is the first ready step. Target = cheapest value on the allowed grid (Pro/Classic 10–60), counting idle running time kept plus the cost of restarts the change adds, with added cold-start queries ≤ 2% of queries. Any finite target captures most of the saving (constructed run: Pro MEDIUM, 1 cluster, 2,000 weekday queries a month, about 79–80k/yr). If a polling client keeps gaps short, flag the client instead of claiming savings. | yes | covered | pass | K1, M1, G32, G40, G11, K5, G44 |
| C02 | Long auto-stop, bursty BI | Gaps between `query.history.start_time` shorter than auto-stop; long auto-stop | Same cost-minimizing target search as C01; the result can be anywhere from the floor to the current value. Constructed run: Serverless at 30 with bursty BI goes to the value its gaps support. | yes | covered | pass | K1, M1 |
| C03 | Uptime with zero queries | Billing hours with no or trivial `query.history` rows | Flag the client or session keeping the warehouse running; do not claim auto-stop savings it cannot achieve. A zero-query warehouse with uptime gets a review item. | yes | covered | pass | G40, G8, G44, G44b |
| C04 | Metadata / keep-alive polling | Frequent short statements (`statement_type`, `client_application`) at intervals below auto-stop | Flag the polling client; show no auto-stop savings until polling stops. | yes | covered | fail | G40, S4 |
| C05 | Scheduled ETL, idle most of the day | Narrow windows of `query_source.job_info` activity; long auto-stop | The nightly start happens at any auto-stop, so a lower value adds no cold starts: Pro/Classic auto-stop goes to the 10-minute floor (constructed run: 45 → 10). Job queries are judged with workload-appropriate latency and sample gates. Serverless compared at its own optimum. | yes | covered | pass | G8, G32, G37, K1, K5 |
| C06 | Over-sized | No spill, low latency, low `total_task_duration_ms` per cluster-hour | One size down as a canary, only when direction-correct pricing shows material savings; latency judged on finished, non-cache, non-metadata queries; savings range widened for extra scale-out; utilization shown as evidence. | yes | covered | pass | K4, G29, G30, G6, G9, G25, S1 |
| C07 | Under-sized (spill) | `spilled_local_bytes` > 0 on many queries | One size up as a canary, with the speed-up applied to busy time only. | yes | covered | pass | G24, K4 |
| C08 | Concurrency-constrained | `waiting_at_capacity_duration_ms` > 0 with `cluster_count` at max | Classic/Pro: max set in one move to the concurrency-derived target (one cluster per 10 concurrent running + queued queries, P99.5, +1, capped at 40), only on a mature epoch with enough queries; cost bound covers the full added capacity. Serverless: queue-based rule at low confidence. | yes | covered | pass | K2, G16, G35, M3b |
| C09 | Unused max-cluster ceiling | Peak concurrency / 10 ≪ max | Lower max to the concurrency-derived target in one move, with no priced saving and without invalidating other steps; hold before seasonal peaks or growth. | yes | covered | pass | K2, G33, G11 |
| C10 | Over-provisioned floor | `cluster_count` at min, low utilization | Min set in one canary to the smallest value covering demand at the start of busy periods (Classic/Pro scale-up lag); Serverless usually 1. Constructed run: Serverless min 4, 90% of time at min, should move toward 1. | yes | covered | pass | K3, S1 |
| C11 | Pinned min = max > 1 | `min_clusters = max_clusters > 1` | Treated as fixed capacity: floor from busy-start demand, ceiling from peak demand, pressure judged on queueing only. | yes | covered | pass | G15, K3, K2 |
| C12 | Bursty morning peaks | `SCALED_UP` events and queue wait at start of day | Keep the floor that covers busy-start demand during the scale-up lag; Serverless as the alternative. | yes | covered | pass | K3, K5 |
| C13 | Cold-start-sensitive interactive (Pro/Classic) | High `waiting_for_compute_duration_ms` after stops | Lower auto-stop only if the cold starts it adds stay within 2% of queries; Serverless compared at its own optimum. | yes | covered | pass | G32, K1, K5 |
| C14 | Serverless auto-stop above the floor | Serverless `auto_stop_minutes` > 5 | Serverless grid 5–30 with near-zero restart cost and no cold-start budget, so usually 5; values below 5 set through the API are left alone. | yes | covered | pass | K1 |
| C15 | Pro/Classic → Serverless, intermittent use | Billed hours ≫ busy hours on Pro/Classic | Plan B experiment priced at Serverless's own best auto-stop and concurrency, scaled to actual billing, using the billed SKU's tier and region and a verified region; a real range, never a collapsed point. Plan A stays recommended. | yes | covered | fail | K5, G36, G20, G5, S3, G41 |
| C16 | Serverless → Pro, steady 24/7 load | Continuous high utilization on Serverless | Pro priced at its own best auto-stop (10-minute floor, 4-minute starts) and concurrency, scaled to billing. | yes | covered | pass | K5, G20 |
| C17 | Classic → Pro | Classic, point-lookup / MERGE heavy | Not optimized; flagged as a performance option. | no | not covered | fail | OOS |
| C18 | Preview channel | `warehouse_channel = PREVIEW` | Observe only: no recommendation, plan step or invalidation; reported in the current configuration with an informational note. Constructed run: a pending size change must not be blocked by the channel. | no | covered | pass | G42 |
| C19 | Newly changed config | Recent `change_time` | Wait until the new configuration is ready (enough settled billed days); show the earliest re-evaluation time. | yes | covered | pass | — |
| C20 | Sparse / new warehouse | Few billing days or queries | Abstain; unknown cost stays unknown, never $0. | yes | covered | pass | G18 |
| C21 | Result-cache-heavy BI | `from_result_cache` high | Cache hits excluded from latency checks; Pro/Classic restart cost includes cache warm-up. | yes | covered | fail | G9, S4, K1 |
| C22 | A few heavy queries drive spill | Spill concentrated in few statements | No upsize when a few statements carry most spilled bytes; flag for query rewrite. Constructed run: 3% of queries with p95 spill 5 GiB is upsized today. | yes | covered | pass | K4 |
| C23 | Mixed ETL and BI on one warehouse | Mixed `query_source` / `client_application` | Not optimized; flagged as a workload-isolation option. | no | not covered | fail | OOS |
| C24 | Many small duplicate warehouses | Same owner, low utilization each | Not optimized (cross-warehouse); flagged. | no | not covered | fail | OOS |
| C25 | Off-hours / weekend idle | Weekday-only activity, long or zero auto-stop | Overnight and weekend idle captured at any finite auto-stop; value set by the added cold-start budget. | yes | covered | pass | K1, M1 |
| C26 | Failure-heavy warehouse | `execution_status` FAILED/CANCELED high | No capacity cuts of any kind while the failure share is high; failures surfaced. Constructed run: at 12% failures only size is blocked. | yes | covered | pass | G34 |
| C27 | Long queries keep warehouse alive | Queries near the 2-day statement timeout | Not optimized; flagged. | no | not covered | fail | OOS |
| C28 | Cluster recycling transients | Short count spikes above max | Not read as demand. | yes | covered | pass | — |
| C29 | Deleted / recreated warehouse | `delete_time` set; new id | Identities kept separate. | yes | covered | pass | — |
| C30 | Billing corrections | `record_type` RETRACTION / RESTATEMENT | Signed sums; only settled days used; replay pinned to an ingestion cut-off; correction share reported. | yes | covered | pass | G21, S2, S7a |
| C31 | Multi-region account | Workspaces in several regions | Per-region collection merged by id; billing completeness judged per workspace. | yes | covered | pass | G21 |
| C32 | Unpriced / promotional SKU | `sku_name` with no `list_prices` row | Cost unavailable (null), never silently dropped. | yes | covered | pass | G2 |
| C33 | Photon | `product_features.is_photon` | Observe only. | no | covered | pass | — |
| C34 | 5X-Large | `cluster_size` 5X-Large (Public Preview) | Never priced without a published rate; an upsize to it is a canary at most. | yes | covered | pass | G28, G24 |
| C35 | Spot policy | REST `spot_instance_policy` | Not modeled; AWS cost labeled as an on-demand upper bound. | no | covered | pass | S6 |
| C36 | Tag / rename churn | Snapshot rows with only `tags` or name changed | No new configuration period, no reset of maturity gates or cost history. | yes | covered | pass | G3 |
| C37 | Month / quarter-end seasonality | Peaks outside the 30-day window | Capacity cuts held when longer-window or prior-period pressure shows a peak. Constructed run: max 8 → 3 proposed weeks before a known quarter-end peak. | yes | covered | pass | G33 |
| C38 | Growth trend | Rising query volume | Downsizes and ceiling cuts held while volume is growing. Constructed run: doubling monthly volume still gets a downsize canary. | yes | covered | pass | G33 |
| C39 | Dev / test warehouses | Tags or owner mark nonprod | Versioned tag-based policy profile (for example a relaxed cold-start budget). | yes | covered | pass | G38 |
| C40 | Serverless budget / usage policies | `usage_policy_id` | Attribution only; not evaluated. | no | not covered | pass | — |
| C41 | Genie / AI-BI dashboards | `query_source.genie_space_id`, dashboard ids | Never propose a type Genie cannot use (only Pro or Serverless targets). | yes | covered | pass | — |
| C42 | Queue near the 1,000 limit | Very high queued counts | Max from concurrency; size only if spill is also present. | yes | covered | pass | K2, G39 |
| C43 | Serverless networking / egress | Billing network usage | Excluded from economics and stated as excluded. | no | not covered | fail | OOS |
| C44 | Result fetch / open sessions keep warehouse active | Activity after query end | Flag the client; idle measured on actual running time. | yes | covered | pass | G40, M1 |
| C45 | Steady 24/7 Pro at high utilization | `cluster_count` steady at 2, continuous queries, 100% of time at min | No floor cut (load needs about 1.6 clusters); no auto-stop change with zero savings; Serverless canary plausible. | yes | covered | pass | K3, S1, G26 |
| C46 | Queueing below the ceiling | High queue %, long capacity wait, time at max just under 5% | A reason code for unresolved queueing; floor or scale-up lag evaluated. Constructed run: 47.5% queued with 4.9% time at max gives no-change with a queueing-below-the-ceiling code and the burst demand against the floor. | yes | covered | pass | G39, M3b, K3 |
| C47 | Already at the smallest size | 2X_SMALL, low latency, no spill | No-change with an explicit "already smallest" reason. | yes | covered | pass | G39 |
| C48 | Long-unchanged warehouse | Last configuration change over 365 days ago | Still discovered and analyzed. | yes | covered | pass | G4 |
| C49 | Auto-stop too low | Pro auto-stop 10, many starts, long compute waits | Auto-stop raised when the restarts it saves outweigh the idle time added. | yes | covered | pass | K1 |
| C50 | Single-cluster warehouse with heavy queueing | `max_clusters = 1`, 40% queued | Max set to peak demand + 1 in one move (about 19 concurrent queries: 1 → 3); base cost about double (demand fills a second cluster all day), worst case about triple. | yes | covered | pass | G35 |
| C51 | Mostly idle warehouse, long auto-stop | Pro 1/1, about 95% of running time idle, auto-stop 120 | About 95% of running time removable (not capped at 60%); Serverless shown as strongly positive, not negative. | yes | covered | pass | G19, M1, G36, K5 |
| C52 | Periodic low-frequency workload | Billed days spaced weekly or rarer (bi-weekly, monthly, month-end close, quarterly, yearly) | Forward cost forecast from enough observed cycles over a window long enough to hold them (up to the 365 days collected); unknown, never a wrong number, until then. | yes | covered | pass | G43 |
| C53 | Long auto-stop, gaps of 1–4 hours | Pro `auto_stop_minutes` in the hours (e.g. 275); queries every ~90 minutes during the day | Auto-stop drops to the smallest 5-minute value that still covers the daytime gaps with a 10% margin (constructed run: 275 → 100, no added cold starts, about 11.9k/yr); values at or below the gap would restart on every gap. | yes | covered | pass | K1 |
| C54 | Very long auto-stop, two daily jobs hours apart | Pro `auto_stop_minutes` = 365; two short job windows ~3 hours apart each morning | Auto-stop drops to the value covering the gap between the two jobs (constructed run: 365 → 210, no added cold starts, about 10.5k/yr) and cuts the long overnight tail. | yes | covered | pass | K1 |
| C55 | Canary regression after a downsize | Size cut one step a few days ago; p95 execution on the same statement sources well above the previous size's | Fail fast: the next run recommends restoring the previous size (ACTIONABLE, `CANARY_GUARDRAIL_BREACHED` with the guard that tripped), first in the plan, no savings claimed. | yes | covered | fail | G50 |
| A01 | Serverless auto-stop below 5 via API | Serverless `auto_stop_minutes` < 5 | Read the actual value; leave it alone. | yes | covered | pass | — |
| A02 | Pro/Classic auto-stop below 10 | Target < 10 on Pro/Classic | Never emitted; rejected by the reviewer. | yes | covered | pass | K1 |
| A03 | Serverless sizing and concurrency | Serverless with high max | Queue-based ceiling rule at low confidence. | yes | covered | pass | K2 |
| A04 | Cluster limits | Targets above 40 max or 30 min | Clamped. | yes | covered | pass | — |
| A05 | Region or networking blocks Serverless | No Serverless in region; custom networking | No migration proposed; region check unknown unless verified. | yes | covered | pass | G41 |
| A06 | Starter warehouse left running | Auto-created default warehouse idle | Treated as always-on or zero-query idle. | yes | covered | pass | G8, K1 |
| A07 | Billing freshness | Last day partially ingested | Days used only once settled (24 h after the day ends and past the workspace watermark). | yes | covered | pass | G21, S2 |
| A08 | Owner attribution | `identity_metadata.owned_by` | Owner reported; redacted values become unknown. | yes | covered | pass | G13 |
| A09 | Statement-timeout change restarts warehouse | — | Never touched. | no | not covered | pass | — |
| A10 | Scheduled dashboard refresh keeps warehouse alive | Regular dashboard-sourced queries | Refresh schedule flagged. | yes | covered | pass | G40 |
| A11 | Cache warm-up after stop | Slow first queries after restart | Restart cost includes warm-up. | yes | covered | pass | K1 |
| A12 | Per-query cost attribution | — | No per-query dollars emitted. | no | not covered | pass | — |

Sources: Databricks documentation (docs.databricks.com/aws/en/compute/sql-warehouse/ create, warehouse-behavior, warehouse-types, monitor; admin/system-tables query-history, warehouses, warehouse-events, billing; sql/user/queries/query-caching; the Warehouses REST API reference), Databricks community and blog posts, and constructed runs of publisher 2.9.0. Per-scenario citations are on the review page.
