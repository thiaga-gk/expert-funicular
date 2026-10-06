# Realizer Source & Evidence Contract v1.0.0

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

## 16. Fail-closed examples

- unresolved configuration state;
- no defensible counterfactual;
- unresolved billing correction chain;
- mixed currency;
- missing material cost component;
- hard performance/reliability failure;
- unresolved overlap;
- stale/insufficient mandatory evidence;
- missing representative workload.
