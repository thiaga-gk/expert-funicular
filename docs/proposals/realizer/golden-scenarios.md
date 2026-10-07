# Golden Realization Scenario Specification v1.0.0

> The scenarios run as `tests/golden/realizer/test_golden_realization.py`; the registry is `tests/fixtures/realizer/golden-registry.json`, and each scenario realized from a hand-built observation has its canonical output under `tests/fixtures/realizer/expected/`.
>
> These scenarios are the contract layer: most run a frozen package and a hand-built observation. The rows about a modified application, several lineages and plan economics (GR-004, GR-022, GR-023, GR-030, GR-039) call `realize()` on real Publisher packages instead (`tests/fixtures/realizer/packages/` and the end-to-end cases' stored packages), with observations measured in the demo fleet's simulated world where one exists, and realize several packages into one state where the row is about more than one lineage: a later package of the same epoch, a cost-profile alternative, a canary rollback after its canary. The stored scenarios of GR-022, GR-023 and GR-030 stay as the regression of their single-lineage forms: a replacement package the caller names, an interval a drift closes, an overlap count the caller supplies.
>
> The end-to-end layer, cases `GE-NN` under `tests/golden/realizer/cases/` (`tests/golden/realizer/test_golden_e2e.py`), runs the whole path on demo-fleet warehouses: a real Publisher package under a given run scope and cost profile, a simulated owner change after it, the live measurement on that world and `realize()`, checked against an oracle from the same simulator with the old value; `tests/golden/README.md` describes it.

**Status:** LOCKED

## 1. Purpose

Executable behavioral truth for the Realizer. Implementation behavior that conflicts with these scenarios is incorrect until the scenario or governing contract is deliberately revised.

## 2. Scenario catalog

| ID | Scenario | Core expectation |
|---|---|---|
| GR-001 | No implementation | no realization |
| GR-002 | Exact applied, intent unknown | APPLIED without synthetic ACCEPTED |
| GR-003 | Exact + confirmed origin | claimable when all gates pass |
| GR-004 | Modified conservative value | actual-state economics, can realize |
| GR-005 | Modified aggressive value | validate actual state; fail if unsafe |
| GR-006 | Contrary implementation | no original recommendation attribution |
| GR-007 | Unrelated change | no recommendation realization |
| GR-008 | Applied but immature | provisional only |
| GR-009 | Rare/monthly workload | wait for representative execution |
| GR-010 | Exact realized success | REALIZED |
| GR-011 | Safe 60% realization | UNDER_REALIZED |
| GR-012 | Safe <50% | UNDER_REALIZED + reopt |
| GR-013 | Positive savings + P95 failure | VALIDATION_FAILED, successful value 0 |
| GR-014 | Reliability failure | VALIDATION_FAILED |
| GR-015 | No defensible counterfactual | block claimable realization |
| GR-016 | Missing same-scope denominator | retain value, ratio null |
| GR-017 | First Plan A step applied | realize step; regenerate downstream |
| GR-018 | Modified first Plan A step | regenerate from actual state |
| GR-019 | Compatible changes together | scenario-level realization if inseparable |
| GR-020 | Pending downstream changed anyway | record reality; never inherit stale economics |
| GR-021 | Plan B migration | scenario-wide branch realization |
| GR-022 | New package while old state remains | old lineage continues |
| GR-023 | Replacement package applied | handoff at effective boundary, no overlap |
| GR-024 | Post-realization config drift | close interval, preserve history, regenerate |
| GR-025 | Rollback | preserve history, stop successful accrual |
| GR-026 | Billing restatement | append correction, reproject |
| GR-027 | Provider price change | separate posted dollars from efficiency |
| GR-028 | Workload grows | actual workload in old-state CF |
| GR-029 | Negative realized value | preserve signed negative |
| GR-030 | Overlapping packages | one authoritative lineage only |
| GR-031 | Independent same target | observed value yes, product attribution no |
| GR-032 | Duplicate ledger event | idempotent |
| GR-033 | Deterministic replay | identical snapshot/hash |
| GR-034 | Policy change during monitoring | explicit PolicyDiff, history intact |
| GR-035 | Churn below 5% | suppress supersession |
| GR-036 | Material improvement >=5% | eligible to supersede |
| GR-037 | Conflicting authority | block claim |
| GR-038 | AWS modeled only | economic value allowed, cash claim suppressed |
| GR-039 | Multi-step cumulative realization | authoritative final-state value; no annualized summation |
| GR-040 | Mature TTM | distinct mature TTM metric |

## 3. Non-waivable scenarios

GR-002, GR-004, GR-010, GR-013, GR-015, GR-017, GR-018, GR-022, GR-023, GR-024, GR-025, GR-026, GR-028, GR-030, GR-032, GR-033 and GR-039.

## 4. Ledger assertions

Every applicable golden test proves append-only history, idempotency, PKG/RPKG lineage, correction references, non-overlapping intervals, separate effective/recorded times, deterministic state replay and no snapshot-only business truth.

## 5. Economic assertions

```text
RealizedSavings = CounterfactualOldCost - ActualObservedCost
```

Realized savings may be negative. Ratio always uses the exact applied scope denominator, never full stale plan savings.

## 6. Validation assertions

No `REALIZED` state is possible when a hard performance/reliability guardrail fails. Savings never override safety.

## 7. Attribution assertions

Tests separately assert implementation conformance, origin attribution, attribution quality and claimability.

## 8. Regeneration assertions

Any invalidating or modified upstream change proves the new Publisher input state equals the actual observed state, not the original target. Reanalysis produces a new immutable PKG/RPKG.

## 9. Workbook assertions

Future workbook projections show recommendation, actual applied value, conformance, validation, same-scope economics, realized value, attribution, re-evaluation and replacement package without recomputing business logic.
