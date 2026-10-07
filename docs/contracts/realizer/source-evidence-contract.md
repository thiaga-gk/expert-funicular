# Realizer Source & Evidence Contract v1.0.0

> The Realizer v1.0.0 specification. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED

## 1. Purpose

Define authoritative sources, evidence quality, freshness/coverage requirements, and fail-closed behavior for post-change application, validation, economics, corrections and drift.

## 2. Authority matrix

| Domain | Primary authority | Use |
|---|---|---|
| Core SQL Warehouse configuration | `system.compute.warehouses` | application/config drift |
| Warehouse operational state | `system.compute.warehouse_events` | start/stop/scale reconstruction |
| Query/workload/performance | `system.query.history` through Analyzer | validation/regime/workload |
| DBU usage | `system.billing.usage` | actual consumption |
| Databricks pricing | approved effective-price source / `system.billing.list_prices` fallback | financial valuation |
| Photon observed use | governed signed billing feature evidence | application/validation |
| AWS economics | versioned AWS price/cost registry; CUR/Data Export when available | economic/cash components |
| Policy | immutable PolicySnapshot | gates/thresholds |
| Counterfactual quantities | Modeler M08 | old-state counterfactual |
| Realized dollars | Estimator REALIZED | financial output |
| HITL intent | explicit workflow/user event | intent/causal evidence |
| Recommendation | immutable PKG | recommended source/target/lineage |

Adapters transport evidence; they do not redefine authority.

## 3. Core config authority

The authoritative configuration is

`warehouse_channel, warehouse_size, max_clusters, min_clusters, auto_stop_minutes, warehouse_type`

via `system.compute.warehouses`. The observed configuration records all of them and Photon. The configuration hash, which identifies an application and decides drift, covers the knobs a decision is made on: type, size, minimum and maximum clusters and auto-stop. A channel change has no priced effect: it is recorded as an independent change, never drifts an application and never counts against a claim.

## 4. Application evidence vs causal evidence

Configuration matching answers **whether** a state is present. Causal evidence answers **why** it changed. These remain independent.

## 5. Photon

Phase 1 follows the existing governed Publisher evidence path. Realizer does not silently expand read-only API authority. Insufficient evidence yields unknown/unconfirmed, never invented state. Photon is an observed billing-mix fact, never a configuration change: it is recorded in the observed configuration (`null` when the billing mix is mixed or unknown) and is outside matching, the configuration hash and drift.

## 6. Query/performance

Analyzer owns normalized post-change metrics including runtime, capacity/compute wait, queueing, reliability, workload volume/mix and representative regime. Realizer consumes Analyzer outputs rather than recalculating them.

## 7. Billing corrections

Signed usage semantics are preserved. Retractions/restatements are not discarded before aggregation.

An observation's `correction` object is optional for a restatement. The collector cannot see the ledger, so it does not compare a re-collected window with what was measured before; the Realizer compares it with the ledger when it measures, and records a correction of a window measured earlier with other economics whether or not the observation carries a correction object ([ledger contract](ledger-contract.md) section 20).

## 8. AWS

Until CUR/Data Export is authoritative, modeled AWS economics are explicitly labeled `MODELED` and must not be represented as actual hard-cash realized value. An observation that states an AWS cash amount (`aws_cash_realized`) or freed commitment (`aws_commitment_freed`) without `aws_evidence_type` `CUR_ACTUAL` is refused (section 16).

## 9. Counterfactual evidence

M08 uses old configuration + actual post-change workload/regime and never uses observed post-change cost as a feature for the old-state counterfactual.

The counterfactual's identity (`CF-*`, `make_cf_id` in `src/realizer/estimator/economics.py`) names what the evidence states about it: the application episode, the package's (old) configuration hash, the counterfactual method the record states, the window (its start, or the applied time without one; its end, or the observed time without one: the measurement id's fallbacks), the replay target (`measurement.replay.replayed_target_minutes`, `null` without a replay), the assumptions (the measurement's source and applied value, cost basis and replay restart cost; `null` without a measurement), the evidence fingerprint (the SHA-256 of the observation's `economics` and `measurement`: costs, components, replay inputs and windows), the modeler version and the policy snapshot. Two collections of one window that differ in their costs or replay inputs therefore get different counterfactual ids. The `COUNTERFACTUAL_RECORDED` payload records the same window (`window.start_utc`, `window.end_utc`), `replay_target`, `assumptions` and `evidence_fingerprint` next to the method, claim eligibility, workload basis, counterfactual cost and modeler version.

## 10. Evidence IDs

Each post-change evidence item receives deterministic `REVID-*` identity based on evidence type, workspace/warehouse, source, observation window and canonical payload hash.

## 11. Evidence quality

`SUFFICIENT | PARTIAL | INSUFFICIENT | CONFLICTING | STALE`

Conflicting authoritative evidence fails closed. Unavailable data is `null`, not zero.

## 12. Source conflict rule

Realizer never chooses whichever source supports a favorable recommendation. Conflicting authoritative observations block authoritative claims until resolved.

## 13. Regionality

Workspace, warehouse and region/source context are retained. Regional source coverage gaps are explicit and never silently dropped.

## 14. Drift ownership

- configuration: Realizer observed-state comparison;
- workload/regime: Analyzer;
- financial: Estimator/price evidence;
- policy: PolicyDiff;
- source quality: source-quality evaluation.

## 15. Claimable value evidence

Strict recommendation-attributed realization requires application, economics, counterfactual, validation, source-quality, exclusivity and causal-attribution gates to pass. Broader validated observed value may be reported without confirmed causality when policy allows.

## 16. Observation format

A realize run consumes one normalized observation in observation format 3.0.0 (`src/realizer/contracts/observation.py`, schema `schema/realization-observation.schema.json`). It is checked before the run writes anything (`validate_observation` in `src/realizer/config/loader.py`).

- The economics state one window's costs and components, never realized to date or the trailing twelve months: both are computed from every measurement of the application episode in the ledger ([ledger contract](ledger-contract.md) section 15), so an observation cannot state them and an observation that carries either key is refused.
- Every key is present; a value the source could not establish is `null`, never omitted, and an input that is `null` never passes its gate (readiness, financial quality). The one exception is `performance.correctness_pass`: correctness is failed only by evidence of a failure, so `null` passes.
- The observation names its workspace and warehouse, which must be the package's; its times are ordered (applied at or before observed, window start at or before window end at or before observed); economics come with a window; `applied_scenario_id`, when present, names a scenario of the package; `replacement_package_id`, when present, is a package id in the contract format and not the package itself; the observed warehouse type is a supported type.
- `overlap_count` is the caller's own count of overlapping claims: above zero it adds the hard disqualifier `PORTFOLIO_SCOPE_OVERLAP`. It can only add a restriction; which lineage owns a change several packages of the warehouse applied is decided from the lineages in the output directory ([ledger contract](ledger-contract.md) section 18).
- `workload` (an object) is informational: it enters no identity and no decision.
- The configuration holds exactly the six decision knobs and the observed Photon state. Values are compared in the Publisher's spelling (a size `X-Large` is `X_LARGE`).
- The financial-quality inputs (attributable spend, price coverage, currency consistency, correction chain, material cost components) are one block; the currency gate also requires the economics to be stated in the package's currency.
- The cost components agree with the costs. When the economics state both `counterfactual_old_cost` and `observed_new_cost`, the realized total is their difference, and the components given add up to it within 0.03 (the Reporter JSON Contract's reconciliation tolerance): `dbu_realized` alone is the total, `dbu_realized + aws_economic_realized` is the total; no component given is not checked. Whether or not both costs are stated, an AWS component (`aws_economic_realized`) needs a DBU component and an `aws_evidence_type`, and an AWS cash amount or freed commitment needs `CUR_ACTUAL` evidence, so modeled AWS is never cash. An amount that is not a finite number is refused naming the field. An observation without a measurement cost basis that gives a DBU component and no AWS component states DBU costs, so it is compared with the published DBU range ([ledger contract](ledger-contract.md) section 16).
- The counterfactual method is one of the policy's four claim-eligible methods (`PAIRED_CANARY`, `CALIBRATED_PRIOR_STATE`, `MATCHED_HISTORICAL`, `CONTROLLED_PRE_POST`) or a diagnostic one (`CORRELATED_ONLY`, `NO_DEFENSIBLE_COUNTERFACTUAL`).
- `measurement` states how a live measurement measured the change (section 18): the step's knob, its source and applied values, the change, observed and previous epochs, the post- and pre-change windows, the query volume ratio, other knobs that changed, the cost basis and the replay's quantities, and for a knob without a replay the billed change (`billed_change`); `null` for an observation a caller supplies. `performance.guards` holds the guard metrics before and after the change, `null` without a post-change window.
- `rollback_observed` is checked against the configuration, never taken on trust: a flag set while the configuration is not back at the source value of the previous application (with no application yet, while a knob of the plan's steps differs from the package's configuration) is refused with a one-line message. The check needs the ledger, so it runs once the lineage entry is recorded and before anything for the observation is written. A configuration back at the source value is a rollback whether or not the flag is set ([ledger contract](ledger-contract.md) section 6); `rollback_requested` is recorded as the caller's intent.
- A change applied before the package was generated is recorded as an independent change (`APPLIED_BEFORE_PACKAGE`), never as an application of the package.
- Every changed decision knob that no authority step of the selected plan explains is recorded as an independent change (`INDEPENDENT_CHANGE_OBSERVED`) with its reason: `OUT_OF_RUN_SCOPE` (the package's run scope did not cover the knob), `NOT_RECOMMENDED` (the package recommends no change to it), `PENDING_REEVALUATION` or `BLOCKED_STEP` (its step carries no authority), `UNRELATED_VALUE` (a value the step cannot relate to). An independent change alone requests no regeneration. The applied step's claim stays claimable only when the counterfactual holds the other changes at their new values (`measurement.holds_other_knobs`, the live replay); otherwise it is observable only (disqualifier `INDEPENDENT_CHANGE_NOT_HELD`).
- An application's remeasurement window is the selected scenario's REMEASURE step (`validation.remeasure_days`; none when the scenario has no such step). An invalidating or modified application requests regeneration once that many days have passed, else after the policy's `remeasure_days_after_invalidating_change`.

## 17. Fail-closed examples

- unresolved configuration state;
- no defensible counterfactual;
- unresolved billing correction chain;
- mixed currency;
- missing material cost component;
- hard performance/reliability failure;
- unresolved overlap;
- stale/insufficient mandatory evidence;
- missing representative workload.

## 18. Live measurement

`realizer --profile <profile> --sql-warehouse-id <id>` measures the package's warehouse from system tables. Access is read-only OAuth through a Databricks CLI profile (a static PAT is refused); the collector statements run on another SQL warehouse, never on the one measured, whose idle time and cost they would otherwise change. The Realizer runs the Publisher's own collector SQL through one bridge module (`src/realizer/analyzer/collector/bridge.py`), so a configuration epoch, billed daily cost, running idle time and the canary guard mean the same in both products ([Publisher LLD](../../architecture/publisher/lld.md), collector SQL). Two passes:

1. The warehouse's configuration epochs (`system.compute.warehouses`), its billed daily cost per epoch (signed usage at list prices; unpriced usage leaves the cost unknown) and the last settled billing day: the Publisher's settle buffer bounded by the workspace's billing watermark.
2. The windows the measurement plan names (`src/realizer/analyzer/measurement.py`): running idle time over a named epoch up to the as-of time, with the replay of auto-stop values when the step is an auto-stop change, and the canary guard metrics over the same days.

**Plan.** The measured step is the first authority-eligible step, the recommended plan's first and then the other scenarios' (an alternative such as a migration), whose knob differs from the package's value at the as-of time; the change is the start of the run of epochs that hold the observed value, and the observation's `applied_at_utc` is its start. When no step's knob differs (nothing applied, a rollback to the package's configuration, or a change no step makes), `applied_at_utc` is the start of the run of latest epochs with the observed configuration if that began after the package was generated, else `null`. The post-change window is the most recent whole UTC weeks of the epoch in effect, from its first whole day to the last settled billing day (or the day before the epoch ended), at most 12 weeks. The pre-change window is as many whole weeks (fewer when the epoch before the change is shorter) ending the day before the change day; the change day is in neither. Nothing is measured before one whole settled week (`NOT_SETTLED`). A knob without a replay counterfactual (`KNOB_NOT_REPLAYED`; today every knob but auto-stop) gets the same windows without replay values: it is observed, never measured, so it has no counterfactual method, no economics and holds no other knob, and its reason stays `KNOB_NOT_REPLAYED` before a whole week has settled too (then without windows).

**Auto-stop replay counterfactual** (`src/realizer/analyzer/projector.py`, method `CALIBRATED_PRIOR_STATE`). The post-change window is replayed with the package's source auto-stop value: the idle cluster-minutes the warehouse would have kept running, less the restarts the old value would have avoided at the package's restart assumption (the Publisher's cost of one restart per floor cluster). An auto-stop of 0 (never stop) is replayed as a value longer than any gap. Every other knob stays at its observed value. The removed cluster-minutes are priced at the window's billed DBU cost per running cluster-minute and, for Pro and Classic, at the modeled AWS price of a cluster-hour (the Publisher's topology and regional EC2 price registry; the package's source region and the registry committed with the Publisher unless `--source-region` and `--aws-price-registry` say otherwise). Serverless has no customer AWS cost. Without an AWS price the realization is DBU only and the material cost component is unavailable. Realized = counterfactual - observed; the AWS component is labeled `MODELED` and never reported as cash.

**Billed change of a knob without a replay** (`measurement.billed_change`, basis `BILLED_PRE_POST_AT_PRE_MIX`). What the bill did after a change the Realizer cannot replay (a capacity add, a size or cluster change), at the pre-change window's query mix, per component and signed post minus expected without the change, so a positive amount is a cost increase: DBU = the post-change billed cost minus the pre-change billed cost per day times the query volume ratio times the post-change days; modeled AWS = the post-change running cluster-minutes at the post-change epoch's modeled cluster-hour price minus the pre-change running cluster-minutes at the pre-change epoch's price, scaled the same way (each window priced at its own size; `null` without both prices or for Serverless); the total only with both components; each annualized by 365 over the post-change days, with the billed rows' currency. It is `null` without both windows' running time, a volume ratio, or billing priced in one currency, and `null` for a replayed knob, whose saving is the replay's. The bill also moves with anything else that changed, so the billed change is reported next to the guards for the Realizer's judgment, never claimed as a saving.

**Readiness inputs.** Elapsed days are the settled whole days since the change; the query count is the post-change window's; the regime is representative when the post-change queries per day are within 0.75 to 1.25 of the pre-change window's, and for an auto-stop replay whenever that volume ratio is known (unknown without a pre-change window): the replay prices the actual post-change days, so a shifted workload is measured and reported as a workload shift ([`policy-contract.md`](policy-contract.md) section 7), while a change judged on queueing and p95 waits for a comparable workload; the evidence is `SUFFICIENT` when warehouse events cover the whole post-change window, `PARTIAL` when they cover part of it, `INSUFFICIENT` without them.

**Billed cross-check** (`src/realizer/analyzer/crosscheck.py`). The replay repeats the Publisher's own arithmetic and assumptions, so an error in a shared assumption would cancel out; the billed comparison measures the same DBU saving without them: the pre-change window's billed cost per day, scaled to the post-change query volume and length, minus the post-change window's billed cost. `AGREES` when it has the replay's sign and is within 25 percent of it; `INCONCLUSIVE` when it cannot resolve the saving (no pre-change window, fewer than two pre-change weeks, another decision knob changed too, query volume outside 0.75 to 1.25 of the pre-change window's, billing not priced in one currency, or a replay saving below twice the noise of the post-change billed total, the pre-change weekly cost's standard deviation times the square root of the post-change weeks), which does not block a claim; `DISAGREES` otherwise, which keeps the measurement out of `CLAIMABLE` (disqualifier `BILLED_CROSS_CHECK_DISAGREES`) until a person reviews it. Both numbers, the difference and the outcome are in the observation's `measurement.cross_check` and the realization package's `economics.measurement`. The pre- and post-change running cluster-minutes are recorded next to them: the quantity the modeled AWS component is priced on, which billing cannot check.

**Origin.** A measured change applied after the package was generated, to the knob its step recommends, has origin `INFERRED`; one an approvals file names (`--approvals`, approvals format 1.0.0, schema `schema/realization-approvals.schema.json`: per approval the package id, the knob, the approved value and a reference such as a change ticket, no person or free text) is `CONFIRMED` with intent `ACCEPTED` and the reference recorded. A change applied before the package is neither. Both qualify for the strict KPI ([policy contract](policy-contract.md) section 9).

**Guard metrics.** The observation's `performance.guards` holds the canary guard metrics over the pre- and post-change windows (queries, failed or cancelled share, queued share, spill share, p95 execution time on the statement sources seen in both) and the restarts per query of both windows; the Realizer's guardrails judge them ([policy contract](policy-contract.md) section 6).

**Measurement evidence.** What a live run read is written first, to `realization/measurements/<workspace>__<warehouse>__<PKG>__<as-of>.json` under the state directory (`--state-dir`, by default `<output-dir>/state`; `--output-dir` itself with `--flat-output`), in measurement evidence format 1.0.0 (`src/realizer/contracts/measurement.py`, schema `schema/realization-measurement-evidence.schema.json`): epochs, billed daily cost, the settled day, the windows' running-idle totals and replay rows, the guard metrics, the AWS price model and the Publisher policy values relied on (restart cost, settle buffer and the guard thresholds, with the package's cost-profile values). It holds no SQL text, owner or tag. `realizer --from-evidence <file>` replays it to the same observation, so a live result is reproducible offline.

**Demo** (`src/realizer/analyzer/collector/demo_world.py`). `realizer --demo` runs this measurement on a simulated world instead of system tables: the package's warehouse must be a Publisher demo-fleet warehouse (`wh-demo-<NNN>` of the fleet, in the fleet warehouse's workspace; any other package is refused with a one-line message before anything is written), and its world is the Publisher demo simulator's for that warehouse, the fleet's own configuration epochs followed by the package's first authority-eligible step applied at 14:32 UTC seven days after the package was generated (`--demo-modified`: a size step to MEDIUM applied as SMALL; no eligible step, or an `--as-of` before that time, leaves the world unchanged). The collector statements are answered from the simulated rows, the measurement is taken 28 settled days after the change day (at 12:00 UTC thirty days after it) or at `--as-of`, and the evidence is written as a live run's with source `DEMO_SIMULATION`, so `--from-evidence` replays it. The demo applies the package's own plan, so it honors the run scope and cost profile the package was built with, and it is the world the end-to-end golden cases measure. A simulated world is never a measurement of a real warehouse; its simulator noise depends only on the scenario and the day.

**Fleet run.** `realizer --publisher-run <dir>` reads every package of a Publisher run (`<dir>` is a run folder, or a root whose newest `run_date=*/run_ts=*` folder is read; its `manifest.json` lists the packages, filtered by repeatable `--warehouse <warehouse_id>` or `<workspace_id>/<warehouse_id>`, a name the run does not hold being refused before anything runs) and observes each lineage it realizes from one source: `--profile` with `--sql-warehouse-id` (a live measurement per package, as above), `--demo` (the package's simulated world) or `--from-evidence <folder>` (for each package the newest measurement evidence file of that package in the folder, by the as-of time in its name; a package with none is refused for its warehouse). `--as-of`, `--approvals` and `--policy` apply to every warehouse. Measurement evidence names the package, so the lineages of one warehouse measured in one run keep one file each.

## 19. Backtest

`realizer --backtest` measures auto-stop changes that already happened, so the prediction model can be checked on real changes before a recommended change has 28 settled days. It uses no package, classifies nothing and writes no ledger. Sources: `--profile <profile> --sql-warehouse-id <id>` (live, every warehouse of the workspace or those named by repeatable `--warehouse <warehouse_id>` or `<workspace_id>/<warehouse_id>`; optional `--source-region`, `--aws-price-registry`, `--as-of`, `--billing-ingested-through`, `--databricks-config-file`), `--from-evidence <file>` (replay) or `--demo` (a deterministic simulated fleet of past changes). Access and the collector SQL are the live measurement's (section 18): the collector warehouse is refused when `--warehouse` names it, before any credential is read, and is not measured otherwise (`EXECUTION_WAREHOUSE`).

**Past changes** (`src/realizer/analyzer/backtest.py`). A past change is two consecutive configuration epochs of one warehouse whose auto-stop differs while every other decision knob (type, channel, size, minimum and maximum clusters) is equal. A pair where another knob changed too is skipped (`OTHER_KNOB_CHANGED`: billed cost and the replay cannot separate the knobs), as is an unsupported warehouse type (`UNSUPPORTED_WAREHOUSE_TYPE`). The post-change window is the most recent whole UTC weeks of the changed epoch, from its first whole day to the earlier of the last settled billing day and the day before the epoch ended, at most 12 weeks; without one the change is skipped (`NOT_SETTLED`), and whole weeks that lie before the first day the history holds (system tables keep a year: the run day less 365 days) are skipped as `BEFORE_HISTORY`. The pre-change window is as many whole weeks (fewer when the epoch before is shorter or starts before the history) ending the day before the change day; a change without one is still measured, without a prediction. The change day is in neither window.

**Collection.** Pass 1 reads the configuration epochs, the billed daily cost and the settled billing day of every selected warehouse at once. Pass 2 reads the planned windows in rounds, each round one statement for the pre-change windows, one for the post-change windows and one canary guard statement, so that a statement reads at most one epoch (and one pair of guard periods) per warehouse.

**Prediction and realization.** The restart cost is the Publisher's cost of one restart for the warehouse type times the floor clusters (as in section 18). The realization replays the old value on the post-change days run at the new one: the idle the old value would have kept (or, for a raise, the idle the old value would have removed) less the restarts it would have avoided (added). The prediction is the saving the Publisher's replay would have predicted for moving old to new on the pre-change days run at the old value: the new value's removable idle less the restarts it adds for a lowered value (or a warehouse that never stopped), minus the added idle less the avoided restarts for a raised one. Each is priced at its own window's billed DBU cost per running cluster-minute and, for Pro and Classic, at the modeled AWS price of a cluster-hour for the epoch's size in `--source-region` (without a region the cost basis is DBU only and the AWS component null; Serverless has no customer AWS cost), and annualized by 365 over the window's days. The ratio of realized to predicted is null when the prediction is not a saving. The billed cross-check of section 18 sits next to each realization, with no other knob changed by construction.

**Evidence and report.** What a backtest read is written first, to `<output-dir>/realization/backtest/evidence-<as-of>.json` in backtest evidence format 1.0.0 (`src/realizer/contracts/backtest.py`, schema `schema/realization-backtest-evidence.schema.json`): per warehouse with an auto-stop change its epochs, billed daily cost and settled day, and per measured change its two windows (running-idle totals and the replay rows of the value each replays), the guard rows and the AWS price model, with the Publisher policy values relied on. It holds no SQL text, owner, tag or email. The report follows at `<output-dir>/realization/backtest/backtest-<as-of>.json` (report format 1.0.0): per change the windows' days, queries, running cluster-minutes, restarts and billed DBU and cost, the predicted and realized saving per window and per year, the ratio, the cross-check, the cold-start share (restarts per query) before and after and the guard metrics (failure, queued share, shared-source p95) before and after; per skipped change its reason; and the assumptions used. Money is a two-decimal string and measured quantities are canonical decimal strings; identical evidence gives an identical report, and `--backtest --from-evidence <file>` replays it. stdout has one line per change (warehouse, change time, old and new value, post-change days, predicted and realized annual saving, cross-check outcome) and the report path.
