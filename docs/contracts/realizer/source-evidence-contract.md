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

For the v2.8.0 integrated release Realizer, authoritative configuration includes:

`warehouse_channel, warehouse_size, max_clusters, min_clusters, auto_stop_minutes, warehouse_type`

via `system.compute.warehouses`.

## 4. Application evidence vs causal evidence

Configuration matching answers **whether** a state is present. Causal evidence answers **why** it changed. These remain independent.

## 5. Photon

Phase 1 follows the existing governed Publisher evidence path. Realizer does not silently expand read-only API authority. Insufficient evidence yields unknown/unconfirmed, never invented state.

## 6. Query/performance

Analyzer owns normalized post-change metrics including runtime, capacity/compute wait, queueing, reliability, workload volume/mix and representative regime. Realizer consumes Analyzer outputs rather than recalculating them.

## 7. Billing corrections

Signed usage semantics are preserved. Retractions/restatements are not discarded before aggregation.

## 8. AWS

Until CUR/Data Export is authoritative, modeled AWS economics are explicitly labeled `MODELED` and must not be represented as actual hard-cash realized value.

## 9. Counterfactual evidence

M08 uses old configuration + actual post-change workload/regime and never uses observed post-change cost as a feature for the old-state counterfactual.

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

A realize run consumes one normalized observation in observation format 2.1.0 (`src/realizer/contracts/observation.py`, schema `schema/realization-observation.schema.json`). It is checked before the run writes anything (`validate_observation` in `src/realizer/config/loader.py`).

- Every key is present; a value the source could not establish is `null`, never omitted, and an input that is `null` never passes its gate (readiness, financial quality). The one exception is `performance.correctness_pass`: correctness is failed only by evidence of a failure, so `null` passes.
- The observation names its workspace and warehouse, which must be the package's; its times are ordered (applied at or before observed, window start at or before window end at or before observed); economics come with a window; `applied_scenario_id`, when present, names a scenario of the package; the observed warehouse type is a supported type.
- The configuration holds exactly the six decision knobs and the observed Photon state. Values are compared in the Publisher's spelling (a size `X-Large` is `X_LARGE`).
- The financial-quality inputs (attributable spend, price coverage, currency consistency, correction chain, material cost components) are one block; the currency gate also requires the economics to be stated in the package's currency.
- The counterfactual method is one of the policy's four claim-eligible methods (`PAIRED_CANARY`, `CALIBRATED_PRIOR_STATE`, `MATCHED_HISTORICAL`, `CONTROLLED_PRE_POST`) or a diagnostic one (`CORRELATED_ONLY`, `NO_DEFENSIBLE_COUNTERFACTUAL`).
- `measurement` states how a live measurement measured the change (section 18): the step's knob, its source and applied values, the change, observed and previous epochs, the post- and pre-change windows, the query volume ratio, other knobs that changed, the cost basis and the replay's quantities; `null` for an observation a caller supplies. `performance.guards` holds the guard metrics before and after the change, `null` when not measured.
- A change applied before the package was generated is recorded as an independent change (`APPLIED_BEFORE_PACKAGE`), never as an application of the package.

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
2. The windows the measurement plan names (`src/realizer/analyzer/measurement.py`): running idle time and the replay of auto-stop values over a named epoch up to the as-of time, and the canary guard metrics over the same days.

**Plan.** The measured step is the first authority-eligible step of the recommended plan whose knob differs from the package's value at the as-of time; the change is the start of the run of epochs that hold the observed value. The post-change window is the most recent whole UTC weeks of the epoch in effect, from its first whole day to the last settled billing day (or the day before the epoch ended), at most 12 weeks. The pre-change window is as many whole weeks (fewer when the epoch before the change is shorter) ending the day before the change day; the change day is in neither. Nothing is measured before one whole settled week (`NOT_SETTLED`) or for a knob without a replay counterfactual (`KNOB_NOT_REPLAYED`; today every knob but auto-stop).

**Auto-stop replay counterfactual** (`src/realizer/analyzer/projector.py`, method `CALIBRATED_PRIOR_STATE`). The post-change window is replayed with the package's source auto-stop value: the idle cluster-minutes the warehouse would have kept running, less the restarts the old value would have avoided at the package's restart assumption (the Publisher's cost of one restart per floor cluster). An auto-stop of 0 (never stop) is replayed as a value longer than any gap. Every other knob stays at its observed value. The removed cluster-minutes are priced at the window's billed DBU cost per running cluster-minute and, for Pro and Classic, at the modeled AWS price of a cluster-hour (the Publisher's topology and regional EC2 price registry; the package's source region and the registry committed with the Publisher unless `--source-region` and `--aws-price-registry` say otherwise). Serverless has no customer AWS cost. Without an AWS price the realization is DBU only and the material cost component is unavailable. Realized = counterfactual - observed; the AWS component is labeled `MODELED` and never reported as cash.

**Readiness inputs.** Elapsed days are the settled whole days since the change; the query count is the post-change window's; the regime is representative when the post-change queries per day are within 0.75 to 1.25 of the pre-change window's (unknown without a pre-change window); the evidence is `SUFFICIENT` when warehouse events cover the whole post-change window, `PARTIAL` when they cover part of it, `INSUFFICIENT` without them.

**Measurement evidence.** What a live run read is written first, to `<output-dir>/realization/measurements/<workspace>__<warehouse>__<as-of>.json` in measurement evidence format 1.0.0 (`src/realizer/contracts/measurement.py`, schema `schema/realization-measurement-evidence.schema.json`): epochs, billed daily cost, the settled day, the windows' running-idle totals and replay rows, the guard metrics, the AWS price model and the Publisher policy values relied on (restart cost, settle buffer and the guard thresholds, with the package's cost-profile values). It holds no SQL text, owner or tag. `realizer --from-evidence <file>` replays it to the same observation, so a live result is reproducible offline.
