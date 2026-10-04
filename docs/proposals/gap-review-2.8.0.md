# Gap review of publisher 2.8.0: findings, decisions and batch plan

Status: **PLAN ONLY. No code, contract, schema, threshold or fixture has been changed.** Reviewed 2026-10-03 against `docs/prd.md`, `docs/hld.md`, `docs/lld.md`, `docs/context.md`, `publisher.py` and current Databricks documentation. Items already tracked in `docs/known-limitations.md` and `docs/proposals/audit-m-group-b.md` (M1, M2, M3b, M4, M9, D1–D6, list-price and on-demand semantics) are not repeated. Demo figures come from a fresh 15-warehouse `--demo` run of 2.8.0.

No change to `schema/recommendation-package.schema.json` is needed for any item. Existing enums and fields cover every fix (`availability: PARTIAL`, `overall_status: NO_CHANGE / REVIEW_REQUIRED`, blocker codes). Most fixes change decisions or IDs, so each batch needs the matching policy/analyzer/optimizer/estimator/bundle/decision-graph version bump and regenerated goldens.

Live status for every item, in priority order by batch: https://claude.ai/artifact/TMmDwx8SBk2ZuzCQbrzr1w (rows are updated as each item clears its approval gate; this file holds the design).

## Findings

| ID | Band | Finding | Where | Worked example |
|---|---|---|---|---|
| G1 | P0 | One unsupported warehouse type aborts the whole run (zero packages for every warehouse). HLD expects a per-warehouse unsupported assessment. | `evaluate_warehouses` → `_validate_supported_warehouse_types` | 200 warehouses, 1 Lakehouse Real-Time: today 0 packages; after 199 packages + 1 manifest exclusion. |
| G2 | P0 | Inner join to `system.billing.list_prices` silently drops usage that has no price row, so cost is understated, not unavailable. | `EPOCH_DAILY_COST_SQL` | 1,000 DBU billed, 150 unpriced: today cost of 850 DBU reported COMPLETE; after `unpriced_dbu=150`, `availability=PARTIAL`. |
| G3 | P0 | Every `system.compute.warehouses` snapshot row is an epoch. Databricks writes a snapshot "when the properties change", and name and tags are properties, so a rename or tag edit resets the 7-day maturity gate, the cost-history floor, the forecast basis and confidence. | `CONFIG_EPOCHS_SQL` and every epoch CTE | Tagging 300 warehouses blocks every candidate for 7 days (`CURRENT_CONFIG_EPOCH_TOO_NEW`) and withholds economics. |
| G4 | P0 | Gap SQL window orders by `start_time` only (equal start times give order-dependent results). Dates use the SQL session timezone while billing `usage_date` is UTC. Unverified risk: 365-day retention of `system.compute.warehouses` might drop the only snapshot of a warehouse unchanged for over a year. | `build_epoch_gap_metrics_sql`; all `DATE()`/`CURRENT_DATE()` | Determinism invariant violated on tied start times. |
| G5 | P1 | Migration target price pools all current `%SQL%` SKUs across tiers and regions; more than one distinct price → AMBIGUOUS, so Plan B is never priced on real accounts (or the wrong row is used if prices coincide). | `SQL_SKU_PRICE_SQL`, `classify_sql_sku`, `resolve_target_sku_price` | Premium + Enterprise Pro SKUs in one account: every Serverless→Pro Plan B unpriced. |
| G6 | P1 | Pro/Classic size pricing falls back to `2^n` capacity; published DBU/hour per cluster is 4, 6, 12, 24, 40, 80, 144, 272, 528. Empirical rate divides billed DBU by event cluster-minutes (M2 misalignment) and is missing for sizes never run. | `SIZE_UNITS`/`size_ratio`, `Estimator._factors`, `_prepare_workspace_calibration` | LARGE→MEDIUM: today factor 0.50×1.30=0.65 (35% saving); published 0.60×1.30=0.78 (22%). |
| G7 | P1 | Migration optimistic bound is uptime × 1 cluster; package `high` is the max across scenarios, so the experiment sets it in 12/15 demo packages and the portfolio upside sums those. | `Estimator._annualized_resource_bounds`, `_package_economics`, Executive Summary | "ETL Heavy": forward 138,770; Plan A base −45,040; Plan B high +112,962 (81%). |
| G8 | P1 | Auto-stop sample gates (≥100 queries/30d, ≥30 gaps) block sparse warehouses where idle time dominates; zero-query warehouses with uptime give no signal. | `Optimizer.optimize` auto-stop | "Sandbox Sparse": 45 queries on 4 days, auto-stop 120 → BLOCKED, Plan A empty. |
| G9 | P1 | Latency percentiles include result-cache hits (~0 ms) and failed/cancelled queries, so the downsize safety gate is more permissive than intended. | `EPOCH_QUERY_METRICS_SQL` | 40% cache hits: executed p95 7 s (blocked) vs mixed p95 4.1 s (passes). |
| G10 | P2 | `overall_status` is OPTIMIZATION_AVAILABLE for 14/15 demo packages because the migration experiment is always TEST_REQUIRED. | `_assessment_contract` | "Sandbox Sparse": Plan A 0 steps, Plan B −1,443 on all bounds. |
| G11 | P2 | Invalidation broader than the mechanism: a no-cost ceiling cut invalidates min-clusters; auto-stop waits behind every canary. 13/15 recommended plans have pending steps. | `Optimizer.optimize` invalidates; `ScenarioPlanner.scenarios` | "BI - Sales": authoritative base 10,338 vs contingent 24,471. |
| G12 | P2 | `--source-region` defaults to `unknown`; registry has only `us-east-1`. Pro/Classic AWS, total and package economics silently become null; a wrong label prices the wrong region. | `parse_args`, `pricing/aws_ec2_on_demand.json` | Live run without the flag reports success with null economics. |
| G13 | P3 | Data hygiene: `__REDACTED__` owner → null; empty/encrypted `statement_text` counted visible; Hive count should be null at low visibility; connector MAP as list of pairs; unweighted IO-cache average; demo SKU names invented and Pro priced at 0.22 (Classic rate). | various | — |
| G14 | P3 | Docs still describe Photon as a seventh domain: PRD §23 item 3; context §14.1, §22.1; HLD §6.8 diagram; LLD §6 "AnalysisResult[7]", §15.8, §32.3. | docs | — |

### Second pass (decision and estimation algorithms)

Examples were produced by calling the real 2.8.0 functions on mutated demo evidence.

| ID | Band | Finding | Where | Worked example |
|---|---|---|---|---|
| G15 | P1 | Pinned warehouses (min = max > 1) can never shrink: `time_at_max_pct = 100` fires `SUSTAINED_SCALE_PRESSURE`, and max reduction needs `max > min`. | `Optimizer.optimize` min/max rules | min = max = 3, 0% queueing: min and max both NO_CHANGE. |
| G16 | P1 | Max-cluster increase has no epoch-age or query-sample gate (every other change rule has both); repeated +1 ratchet across new epochs. | `Optimizer.optimize` ~1954 | Epoch 1 day old, 25 queries: `max_clusters ACTIONABLE 2→3` while size is held as too new. |
| G17 | P0 | Missing pressure evidence fails open: `queue_pct`, `spill_pct`, `p95`, `p99` default to 0 via `safe_float`; max-reduce never checks missing capacity wait. | `Analyzer.analyze` ~1788; downsize, max-reduce | All pressure fields None: downsize TEST_REQUIRED and max reduce ACTIONABLE. |
| G18 | P0 | Unknown DBU baseline published as $0: zero-filled days pass the 7-day floor with `active_cost_days = 0`; a large net-negative day floors the forecast at 0. | `Modeler.forecast_from_daily_cost` | No DBU rows for 30 days: forward DBU 0.0 (FORECAST), size savings 4,690/6,801/8,208 all AWS, Plan B −14,581 vs $0. |
| G19 | P1 | Removable idle cluster-minutes larger than running cluster-minutes are clamped to the 0.60 cap and priced instead of rejected. | `Estimator._factors` auto-stop | Events 500 cluster-min, gaps 15,840: ACTIONABLE, DBU savings 8,100/12,150/16,200 on 27,000. |
| G20 | P1 | Migration savings subtract a modeled target (event minutes × rate × price) from a billed baseline with no same-type reconciliation. | `Estimator._migration_costs` | Same formula on the current Pro warehouse: 8,268 modeled vs 27,000 billed (0.31). |
| G21 | P1 | Billing latency and boundaries: watermark is the account-wide leading edge; partly ingested days lower the forecast; replays not pinned to ingestion; event minutes run to now while billing days stop at yesterday. | `BILLING_WATERMARK_SQL`, Modeler, `model_aws_baseline_cost` | Half-ingested last day: −6.2% (8-day epoch), −1.8% (mature). AWS on a 7-day epoch: +0.3% at 00:30, +14% at 23:30. |
| G22 | P2 | Forecast day-of-week bias on epochs that are not whole weeks. | Modeler | Weekday-only $100/day, true 26,071: 9-day epoch 20,278 (−22%), 10-day 29,200 (+12%). |
| G23 | P2 | Confidence: default cap 0.80 equals HIGH, so blocked/no-change can be HIGH; sample term ignores the knob's own sample; empty scenario = 1.0 HIGH. | `Estimator._confidence`, `_scenario_confidence` | Auto-stop BLOCKED with 5 gaps scores 0.80 HIGH. |
| G24 | P2 | Upsize and max increase are ACTIONABLE without canary; runtime multiplier is applied to idle uptime too, understating upsize cost. | Optimizer, `Estimator._factors` size | 50% idle upsize: modeled 1.40 vs 1.70. |
| G25 | P2 | Downsize savings ignore the extra scale-out smaller clusters cause on multi-cluster warehouses. | `Estimator._factors` size | Avg 1.6→2.4 clusters: 25% saving, not the base 35%. |
| G26 | P2 | Auto-stop ACTIONABLE with $0 savings; gap count includes sub-second gaps; fixed target table ignores collected p50/p90 gaps and restart cost. | Optimizer auto-stop; gap SQL | No gap longer than any target: 120→30 ACTIONABLE, savings 0/0/0. |
| G27 | P3 | Bundle conservative track applies the reduced minimum to auto-stop although it realized 0% of the min reduction. | `StatefulBundleEstimator._apply` | Plan [min, auto-stop] low 3,834 = half of stand-alone auto-stop low 7,668. |
| G28 | P3 | Plan B named "Pro migration" and checked against PRO for a Pro warehouse when migration is not evaluated; 4X_LARGE upsizes to Beta 5X_LARGE with no published DBU rate. | `ScenarioPlanner.scenarios`; `SIZE_ORDER` | — |

### Billing latency

Databricks: query history "typically available within one hour"; billing usage "typically available within 12 hours", longer for new workspaces, for ORIGINAL records only; RETRACTION/RESTATEMENT have no bound; `system.billing.usage.ingestion_date` exists ("Date the record was ingested"); hourly granularity is not documented.

Today the watermark is `MAX(usage_end_time)` of SQL usage over 30 days, account-wide, and a day counts as complete once the watermark passes it. Proposed (G21): a per-workspace watermark plus a settle buffer (`billing_settle_hours`, proposed 24) so a day is complete only when `analysis_time ≥ day end + buffer`; all windows end at the same complete-day boundary (with M2 and group B defects D3/D6); evidence records `billing_ingested_through = MAX(ingestion_date)` and replays filter `ingestion_date ≤` it, so identical evidence stays reproducible after late corrections; correction DBU share becomes a quality signal.

### Unused data points

| ID | Field | What it fixes | Value |
|---|---|---|---|
| S1 | `query.history.total_task_duration_ms` | Utilization ratio (task-seconds per running cluster-second): proof of spare floor capacity (known limitation 13), downsize headroom (G25), pinned warehouses (G15) | High |
| S2 | `billing.usage.ingestion_date` | Watermark and reproducible replay (G21) | High |
| S3 | `billing.usage.product_features.sql_tier` | Billed tier for SKU matching (G5); cross-check of `warehouse_type` | High |
| S4 | `query.history.statement_type`, `compilation_duration_ms`, `result_fetch_duration_ms` | Remove metadata statements from latency, spill and sample counts; flag latency size cannot fix (extends G9) | Medium |
| S5 | `billing.usage.usage_metadata.node_type` | Confirm the AWS topology's instance family per warehouse | Medium |
| S6 | Warehouses REST `spot_instance_policy` (not in system tables) | Spot share of Pro/Classic AWS cost (known limitation 15); needs a CLAUDE.md REST-scope decision | Medium |
| S7 | `record_type`, `query_source.job_info`, `system.access.audit` | Correction share; scheduled vs interactive; audit log as change history for G3 | Low |

Not usable: `system.compute.node_timeline` excludes SQL warehouses; `system.access.workspaces_latest` has no region column (G12 cannot be auto-resolved); `system.billing.cloud_infra_cost` is not GA.

### Third pass (per-knob optimization and per-step savings), re-checked on publisher 2.9.0

M9 Stage 1 changed only the contract adapter; earlier findings stand. Since M9, G3 also hides a real `observed_change` when a tag edit follows a configuration change.

| ID | Band | Finding | Worked example |
|---|---|---|---|
| G29 | P1 | Size runtime multipliers are chosen from the capacity ratio, not the change direction; billing-derived Pro/Classic DBU per cluster-hour is not monotone in the demo (SMALL 164, MEDIUM 245, LARGE 198), so LARGE→MEDIUM downsizes get the upsize speed-up multipliers and shrink the resource tracks for every later step. | "BI - Sales" LARGE→MEDIUM: DBU factor 0.869, AWS 0.42, savings 10,338; direction-correct about 8,520. Following min-cluster step adds 8,110, more than its stand-alone 7,745. |
| G30 | P1 | COST-objective changes are not gated on their own modeled economics. | "Supply Chain BI" downsize TEST_REQUIRED with modeled savings −4,835. |
| G31 | P2 | The plan states no per-step annual savings: JSON has cumulative `running_savings_365d` only; workbook shows none. | "BI - Sales": per-step 10,338 · 0 · 8,110 · 6,022 vs stand-alone 10,338 · 0 · 7,745 · 11,501. |

Databricks facts used (docs.databricks.com/aws/en/compute/sql-warehouse/, updated Sep 11, 2026): Classic/Pro "have a fixed limit of one cluster per 10 concurrent queries"; Classic/Pro scale up by estimated load (2–6 min of load: +1 cluster; 6–12: +2; 12–22: +3; then +1 per 15 min) or after a 5-minute queue wait, and scale down after 15 consecutive low-load minutes; queue limit 1,000; startup ≈4 min Classic/Pro, 2–6 s Serverless; idle warehouses accrue DBU and cloud charges; queries fetching results keep a warehouse active; local result cache and disk cache are cleared on stop, the Serverless remote result cache survives; auto-stop minimum 10 (Classic/Pro), 5 in the UI or 1 via API (Serverless); `min_num_clusters` ≤ min(max, 30), max ≤ 40; spill → increase size, queueing → more clusters. Not documented first-party: Pro/Classic DBU per size, Serverless per-cluster concurrency, billing during STARTING/STOPPING.

Per-knob target rules (common order: hard limits, evidence sufficiency, reliability guards, maximum annual savings, smallest change among results within 2% of the best; observable knobs may jump to the optimum, extrapolated knobs move one step with a canary):

- **K1 auto-stop:** evaluate every allowed value (Serverless 5/10/15/20/30; Pro/Classic 10/15/20/30/45/60); cost(T) = idle running cluster-minutes kept + restarts(T) × restart cost (Classic/Pro ≈4 min plus cache loss; Serverless ≈0); constraint restarts per active day ≤ budget and cold-start wait within guard. New evidence: per-T removable idle on RUNNING time (M1) and per-T restart count. Example "BI - Sales": removable idle 5,500 / 7,920 / 10,120 / 11,000 min at T = 45 / 30 / 15 / 10; table picks 30 (11,501/yr); T = 10 ≈ 15,974/yr before restart cost.
- **K2 max clusters:** Classic/Pro required clusters per minute = ceil((running + queued) / 10); target = clamp(ceil(P99.5) + 1, min, 40), reached in one move up or down. Serverless keeps the queue rule at LOW confidence. New evidence: per-minute concurrency histogram (with M3b).
- **K3 min clusters:** smallest m ≥ 1 covering P95 of required clusters at the start of busy periods (scale-up lag); Serverless usually 1; may jump with a canary. Subsumes G15.
- **K4 size:** stays one step with a canary; direction-correct, validated pricing (G29, G6), value gate (G30), clean population (G9/S4), scale-out widening (G25).
- **K5 migration:** price Serverless at its own K1 optimum and concurrency, scaled to billing (G20).

G31 proposal: Reporter 1.2.0 → 1.3.0 additive required-nullable `StepProjection.step_savings_365d` (ComponentRanges; per-track change on the step's own chain, ordered low ≤ base ≤ high, computed by the bundle estimator); Workbook 1.3.0 → 1.4.0: plan text appends "step saves N/yr (base)", Recommendation Registry adds `plan_step`, `plan_step_savings_base_365d`, `plan_step_pricing_basis` for the recommended scenario. Four sheets unchanged.

### Golden-scenario pass

56 cost-optimization scenarios (44 seeded, 12 added from Databricks documentation and community sources) plus constructed runs of publisher 2.9.0 (now folded into scenario rows C01–C51). Every scenario maps to a tracker item, works today, or is out of scope. The catalog (63 rows: scenario, signature, expected behavior, scope, code coverage, pass/fail, tracker IDs) is in `docs/proposals/golden-scenarios.md`. New items:

| ID | Band | Finding | Golden case |
|---|---|---|---|
| G32 | P1 | Cold-start guard measures current exposure (p95 compute wait ≥ 60 s and starts/day ≥ 0.5), not the restarts the change adds: any nightly-sleeping Pro warehouse is BLOCKED, an always-on 0→30 passes with HIGH confidence. Replaced by K1's marginal-restart guard. | C01, C05 |
| G33 | P1 | No growth or seasonality guard on capacity cuts: quiet 30 days after a quarter-end peak gives max 8→3 ACTIONABLE; doubling volume still gets a downsize canary; forecast trend capped at +6.25%. A prior-epoch veto needs a CLAUDE.md exception. | C38, C37 |
| G34 | P1 | Failure/timeout guard applies to size only; at 12% failed queries, max reduce, min reduce and auto-stop proceed. | C26 |
| G35 | P2 | Max-cluster increase cost bound capped at +50%: max 1→2 at 100% time at max shows worst −32.4k on a 64.7k baseline (true worst −64.7k). | C50 |
| G36 | P1 | Migration range collapses to a point when `max_observed_clusters = 1` and is wrong-signed for idle warehouses (−8,176 where about +24k is expected). D4's `MIGRATION_NOT_COST_EFFECTIVE` must ship after K5. | C51, C01, C05 |
| G37 | P2 | Latency and sample gates ignore workload type: nightly ETL can never be downsized. | C05 |
| G38 | P2 | Tags collected but never used; dev/sandbox warehouses get production thresholds. | C39 |
| G39 | P3 | Silent NO_CHANGE with empty blockers for queueing below the ceiling and for 2X_SMALL. | C46, C47 |
| G40 | P2 | Non-work activity (BI polling, keep-alive and metadata queries, open sessions, result fetching, dashboard refresh) keeps warehouses running and is not flagged. | C03, C04, C44, A10 |
| G41 | P2 | Serverless region availability check passes on any region label (no verified list). | C15, A05 |
| OOS | — | Classic→Pro, workload isolation, consolidation, statement timeout, networking, budget policies: flag only. | C17, C23, C24, C27, C40, C43 |

Notes folded into existing items: K1 never targets below 10 min on Pro/Classic and evaluates raising auto-stop (C49); K3 must derive the floor from concurrency because steady warehouses show 100% time at min (C45); K4 adds a spill-concentration check (known limitation 12, C22). Negative billing corrections (C30) carry the original usage date and net out on that day, so they are covered by G18/G21.

### Worked example: always-on warehouse (golden scenario C01)

Pro MEDIUM, 1 cluster, auto-stop 0, 2,000 queries in 30 days on 22 weekdays, always running: 43,200 cluster-minutes per 30 days. Forward baseline 111,848/yr (DBU 46,253 + AWS 65,595; demo prices).

| | 2.9.0 today | Optimum (K1 with M1, D10, D11, D13) |
|---|---|---|
| Step position | 2nd, PENDING behind the size canary | 1st, READY (auto-stop is independent of size, D1/D13) |
| Target | 30 (fixed table) | Lowest-cost T passing the 2% cold-start share |
| Removable share of running time | capped at 60% | measured: T=10 75.2%, 15 74.1%, 30 72.0%, 45 70.6% |
| Annual savings, before restart cost | 33.6k / 50.3k / 67.1k (contingent); authoritative 0 | T=10 84.1k · T=15 82.9k · T=30 80.5k · T=45 79.0k |
| Restart cost | not counted | ≈$0.85 per Pro restart (4 min × $0.213 per cluster-minute) plus cache warm-up |
| Cold-start guard | passes (starts/day 0.05) | budget 2% × 2,000 = 40 restarts per 30 days; the ~22 morning restarts use about half of it |

Result: going from 0 to any finite target captures about 94% of the attainable saving (79.0k at T=45 vs 84.1k at T=10). The choice of T is second order and is set by the cold-start budget: with a p90 gap of 12 minutes, T=10–15 would add roughly 200 intra-day restarts a month (about 10% of queries waiting ~4 minutes), so the optimizer settles at the lowest T whose gaps longer than T stay within ~18 a month (likely 30–45). Expected ≈79–80k/yr authoritative, versus 0 authoritative today. The exact T needs `restarts(T) = COUNT_IF(gap > T)` per grid value, which K1 adds to the collector. Figures before M1 assume the query-gap idle equals RUNNING idle.


## Decisions (user, 2026-10-03)

- **D1 (G11):** a max-cluster ceiling cut above the observed peak invalidates nothing; auto-stop is evaluated independently of size and min-clusters. Requires a PRD FR-PLAN-001 revision.
- **D2 (G7):** tighten the migration bound (cluster-minutes × documented efficiency range, minus Classic/Pro startup ≈4 min and idle tail) and split the Executive Summary into "Recommended plan savings" (Plan A low/base/high) and "Experiment upside" (Plan B high). Workbook additive rows; JSON envelope unchanged.
- **D3 (G8):** price sparse auto-stop from event-derived idle tails (RUNNING after the last query until STOPPED); ACTIONABLE when measured; zero queries with uptime → REVIEW. Delivered with M1. Cold-start guard unchanged.
- **D4 (G10):** `overall_status` derives from the recommended scenario; a cost-objective migration with every bound negative becomes NO_CHANGE with blocker `MIGRATION_NOT_COST_EFFECTIVE`; unpriced migration stays TEST_REQUIRED; empty Plan A with a priced positive Plan B → REVIEW_REQUIRED.
- **D5 (G12):** in live mode, refuse to start when Pro/Classic warehouses exist and the region is missing from the registry, unless `--allow-missing-aws-price` is passed; cross-check the label against the region token of billed Serverless SKUs.
- **D6 (G24, G16):** upsize becomes TEST_REQUIRED with a latency/cost canary; max-cluster +1 stays ACTIONABLE but gains the epoch-age and query-sample gates; the size runtime multiplier applies to busy time only.
- **D7 (S6):** do not read `spot_instance_policy` now; keep on-demand AWS as the labeled upper bound and solve with CUR/effective rates later.
- **D8 (S1):** collect task-time utilization now as evidence only (findings, no decision effect); use it in decisions after the first real-data run calibrates it.
- **D9 (G21):** `billing_settle_hours = 24`: a UTC day counts as complete only 24 h after it ends and once the workspace's own watermark has passed it.
- **D10 (K1–K3):** observable knobs (auto-stop, max, min) jump straight to the evidence-derived optimum; min keeps its canary; size and migration stay one step with a canary.
- **D11 (K1), refined 2026-10-04:** cost decides (each restart's start-up and cache warm-up is priced in the objective); the user guard is the *added* cold-start query share = (restarts(T) − restarts(current)) / queries ≤ 2% for Pro/Classic (the G32 fix), no guard for Serverless. Counting all restarts would block every nightly-ETL warehouse, whose nightly start happens at any T.
- **D12 (G31):** add `step_savings_365d` (Reporter 1.3.0) and the per-step amount in the workbook plan text plus three Registry columns (Workbook 1.4.0).
- **D13:** order plan steps by base savings × confidence / risk, respecting only true invalidations (after D1); a channel REVIEW stays first. Revises PRD FR-PLAN-001 together with D1.
- **D14 (2026-10-04):** retire `warehouse_channel` as a recommendation; observe only, like Photon. No candidate, plan step or invalidation; reported in `current_config`, kept in epoch identity; informational note for PREVIEW; knob enum value reserved; `WH-CHANNEL-*` techniques retired. Candidates per package 6 → 5. Product-scope change: PRD 6.4, FR-OPT-001, §23; HLD 3.3; LLD 15.1; context §14; CLAUDE.md supported-scope line.
- **G5:** a real `list_prices` / billed-SKU sample is not available yet; G5 stays fail-closed until one is.

## Batch plan

1. **Collector correctness (P0):** G1, G2, G4, then G3.
2. **Fail-closed evidence (P0):** G17, G18, G16.
3. **Billing and windows (P1):** G21 with S2, G22; together with M2.
4. **Pricing (P1):** G6 with M4, G9 with S4, G19, G20; G5 with S3 once a SKU sample exists.
5. **Policy (D1–D6):** G11, G7, G10, G12, G15, G24; PRD/HLD/LLD text updated in the same batch.
6. **Sparse auto-stop with M1/M2:** G8 (D3), G26, G25, S1.
7. **Hygiene and seal:** G23, G27, G28, G13, G14, S5, stale-reference scan, release seal.

Each batch follows the CLAUDE.md change loop with a per-item summary and worked example at an approval gate.

## Coordination with M9

`docs/proposals/m9-contract-design.md` (signed off) claims publisher 2.9.0, Reporter contract 1.2.0 and Workbook contract 1.3.0. The G1 exclusion row and the D2 Executive Summary rows should ride the same Workbook 1.3.0 bump. G3 must land before or with M9 Stage 1: otherwise M9's `observed_change` would report a rename or tag edit as an applied configuration change.

## Verification still needed on first real data

- SKU naming and tier/region tokens in `system.billing.list_prices` and billed `sku_name` (G5).
- Pro/Classic DBU/hour parity with the Serverless table from a first-party page (G6).
- Whether a tag or rename creates a `system.compute.warehouses` row (G3) and whether 365-day retention drops the only snapshot of a long-unchanged warehouse (G4).
- `cluster_count` reported on STARTING/STOPPING events (affects uptime and D3).
