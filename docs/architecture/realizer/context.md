# Domain Context — Databricks SQL Warehouse Realizer v1.0.0

> The Realizer v1.0.0 specification. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED

## 1. Mental model

Publisher describes **what should happen**. Realizer records/evaluates **what actually happened**.

## 2. Vocabulary

- `PKG` — immutable recommendation version.
- `RPKG` — stable realization lineage for exactly one PKG.
- `RLE` — immutable realization fact/event.
- `RSNAP` — deterministic current view of one RPKG.

```text
Recommendation changes -> NEW PKG
Reality changes        -> NEW RLE
Interpretation changes -> NEW RSNAP
```

## 3. Recommendation vs implementation

`EXACT` means observed value matches the authoritative target. `MODIFIED` means the recommended knob changed to another target consistent with the intervention direction/intent. `NOT_APPLIED`, `CONTRARY` and `UNRELATED` have their literal meanings. Modified does not mean failed: measure/validate/re-estimate the actual state.

## 4. Intent vs application vs causality

Accepted != applied. Applied != causal attribution. Exact configuration match can exist with `intent=UNKNOWN` and `origin=INDEPENDENT`.

## 5. Two value KPIs

Validated observed economic realization and confirmed recommendation-attributed realization are separate.

## 6. Counterfactual realization

Realized value uses the old state under actual post-change workload minus corrected actual new-state cost. Naive before/after spend is prohibited when workload changes.

## 7. Safety and under-realization

Unsafe monetary savings -> `VALIDATION_FAILED`. Safe but low financial realization -> `UNDER_REALIZED`. These are different.

## 8. Same-scope denominator

The realization ratio always compares against expected economics for the exact applied scope. Modified targets are re-estimated. Pending/stale targets never supply the denominator.

## 9. Plan A

A partially completed plan may contain fully realized completed steps. Downstream `PENDING_REEVALUATION` is not authoritative until regenerated. Realizer re-evaluates from the actual state and Publisher emits a new package.

## 10. Supersession and economic handoff

Newer recommendation package != immediate economic ownership. Handoff occurs when the new physical/configuration state becomes effective. Replacement packages receive no retroactive value.

## 11. Maturity

`NOT_APPLIED|COLLECTING|PROVISIONAL|REPRESENTATIVE|MATURE`. Savings-to-date, annualized run-rate and mature TTM are distinct views.

## 12. Corrections/drift/rollback

Corrections append and reproject. Drift closes only the affected valid interval while preserving prior history. Rollback requires observed authoritative state.

## 13. Null vs zero

Zero means known zero. Null means unavailable/not defensible. Never coerce unknown to zero.

## 14. Phase-1 boundary

Phase 1 is the v1.0.0 Realizer release: the Publisher and the Realizer, SQL source execution, local JSON/JSONL, and proof of the complete closed loop. Phase-2 terminology and runtime design are not part of this repository.

## 15. Engineering mantra

> Prove the closed loop first; split code after behavior is proven.
