# Domain Context — Databricks SQL Warehouse Realizer

> The Realizer specification, kept in line with the code. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED

## 1. Mental model

Publisher describes **what should happen**. Realizer records/evaluates **what actually happened**.

## 2. Vocabulary

- `PKG` — immutable recommendation version.
- `RPKG` — stable realization lineage for exactly one PKG.
- `RLE` — immutable realization fact/event.
- `RSNAP` — deterministic current view of one RPKG.
- `APP` — one application episode: a configuration state observed as applied, with its effective time and its relation to the recommendation.
- `VAL` — one validation episode of an application (guardrails, readiness, representative workload).
- `MEAS` — one measurement of realized value over a window.
- `CF` — a counterfactual: the old state under the actual post-change workload.
- `REVID` — the post-change evidence a snapshot rests on.
- Lineage — one package's realization (one RPKG) among the others of its warehouse. A warehouse can hold several: daily packages of one epoch, profile alternatives, the package generated after a change. One change belongs to one lineage.
- Cross-lineage owner — the lineage that owns a change several lineages realize: the one whose package was generated most recently at or before the change. The others still measure and report it, observable only.
- Claim status — `CLAIMABLE` (the measured value counts toward the recommendation), `OBSERVABLE_ONLY` (measured and reported, not counted: claimed by another lineage, a migration, a cross-check that disagrees) or `NOT_ATTRIBUTABLE`.
- Measurement window — the whole UTC weeks the live measurement reads, before the change (pre-change window) and after it (post-change window). The change day belongs to neither.
- Settled week — a whole UTC week all of whose billing days have settled; nothing is measured before one has.
- Billed cross-check — the same DBU saving measured without the replay's assumptions, from the billed cost before and after the change: `AGREES`, `INCONCLUSIVE` or `DISAGREES`.

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

Realized value uses the old state under actual post-change workload minus corrected actual new-state cost. Naive before/after spend is prohibited when workload changes. The Realizer measures the counterfactual of an auto-stop change (the old value replayed over the actual gaps), a size change (the post-change days priced at the old size, from the measured query runtime ratio) and a minimum-cluster cut (the cluster-minutes the old floor would have kept); a migration is observed and never claimed, a maximum-cluster change has no claimable saving ([source and evidence contract](../../contracts/realizer/source-evidence-contract.md) section 18).

## 7. Safety and under-realization

Unsafe monetary savings -> `VALIDATION_FAILED`. Safe but low financial realization -> `UNDER_REALIZED`. These are different.

## 8. Same-scope denominator

The realization ratio always compares against expected economics for the exact applied scope. Modified targets are re-estimated. Pending/stale targets never supply the denominator.

## 9. Plan A

A partially completed plan may contain fully realized completed steps. Downstream `PENDING_REEVALUATION` is not authoritative until regenerated. Realizer re-evaluates from the actual state and Publisher emits a new package.

## 10. Supersession and economic handoff

Newer recommendation package != immediate economic ownership. Handoff occurs when the new physical/configuration state becomes effective. Replacement packages receive no retroactive value. A warehouse can hold several lineages at once; exclusivity, supersession and the state they share are in [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) sections 18, 19 and 24.

## 11. Maturity

`NOT_APPLIED|COLLECTING|PROVISIONAL|REPRESENTATIVE|MATURE`. Savings-to-date, annualized run-rate and mature TTM are distinct views. Savings to date sum the episode's measurements over the time they cover; `MATURE` needs 365 covered days ([`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) section 15).

## 12. Corrections/drift/rollback

Corrections append and reproject. Drift closes only the affected valid interval while preserving prior history. Rollback requires observed authoritative state.

## 13. Null vs zero

Zero means known zero. Null means unavailable/not defensible. Never coerce unknown to zero.

## 14. Phase-1 boundary

Phase 1 is the Realizer as built: the Publisher and the Realizer, read-only system-table measurement (or replay of its evidence, or a simulated demo world), local JSON/JSONL, and proof of the complete closed loop. Phase-2 terminology and runtime design are not part of this repository.

## 15. Engineering mantra

> Prove the closed loop first; split code after behavior is proven.
