# Low-Level Design — Databricks SQL Warehouse Realizer v1.0.0

> The Realizer v1.0.0 specification. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED  
**Implementation baseline:** Publisher v2.8.0 repository (evolved from the Publisher v2.7.0 baseline)

## 1. Phase-1 structure

```text
src/realizer/realizer.py        # facade: realize_request() calls each capability once
src/realizer/<capability>/      # analyzer, governor, reconciler, estimator, reviewer, packager, reporter
src/realizer/contracts/         # typed contracts, report projection, JSON Schema builders
src/realizer/cli/               # command-line adapter (`realizer`, `python -m realizer`)
schema/realization-*.json       # committed JSON Schemas
docs/*/realizer/                # Realizer specifications
tests/                          # unit, component, golden, regression (stored baseline)
```

The single-file core was split into the capability packages only after the proof gate (section 2). Its internal sections were the seams: canonical IDs, collection, application matching, plan authority, ledger, projection, readiness, Analyzer/M08/Estimator integration, denominator, attribution, exclusivity, daily value, corrections, drift, rollback, regeneration, portfolio, compatibility, persistence, CLI.

## 2. Extraction gate

Split only after GR-001..040, Publisher→Realizer E2E, live source validation, modified implementation, ledger replay, billing correction, PKG replacement chain, portfolio reconciliation and Workbook integration all pass.

## 3. Canonical application model

`EXACT | MODIFIED | NOT_APPLIED | CONTRARY | UNRELATED`

Numeric knobs use directional comparison. Warehouse size uses the same shared canonical ordering as Publisher. Warehouse type/channel use explicit transition comparators. Unknown evidence remains unclassified/null.

## 4. Publisher step authority

Realizer references Publisher step by `(scenario_id, step)`; it does not invent a Publisher-issued plan-step ID. `PENDING_REEVALUATION` is visible old context but not authoritative execution economics.

## 5. Deterministic IDs

SHA-256 semantic identities; display prefix + first 24 uppercase hex chars. `RPKG`, `RLE`, `RSNAP`, `APP`, `VAL`, `CF`, `MEAS`, `REVID` use canonical payloads that exclude filenames/log metadata/projection wall clock.

## 6. Time/intervals

Ledger is bitemporal. Effective-time quality: `EXACT|BOUNDED|OBSERVED_AT_ONLY`. Economic/application ownership intervals use `[start,end)`. `RSNAP.as_of_utc` derives from the ledger checkpoint.

## 7. Ledger

One JSON object per line, one file per RPKG in Phase 1. Append-only, idempotent, correction-aware, replayable. Projection is a pure deterministic function of PKG + ordered ledger + projector version.

## 8. Readiness/validation

All policy readiness gates pass before validation. Analyzer computes post-change metrics. Realizer applies Policy rules and hard-guardrail precedence.

## 9. M08/Estimator

M08 receives old PlanState + actual post-change workload/regime. Estimator REALIZED computes money. Realizer classifies/attributes but does not invent financial formulas. The live auto-stop counterfactual replays the actual post-change gaps with the old value ([source and evidence contract](../../contracts/realizer/source-evidence-contract.md) section 18).

## 10. Denominator

`PUBLISHER_SCENARIO | PUBLISHER_CANDIDATE | REESTIMATED_APPLIED_SCOPE | UNAVAILABLE`.
Modified always uses `REESTIMATED_APPLIED_SCOPE`. Pending/contingent/stale economics are forbidden.

## 11. Attribution/exclusivity

Origin `CONFIRMED|INFERRED|UNKNOWN|INDEPENDENT|NOT_ATTRIBUTABLE`; strict product KPI requires Policy-qualified origin. Economic ownership grain is workspace/warehouse-resource/economic atom/[start,end)/basis and allows max one authoritative claimant.

## 12. Corrections/drift/rollback/regeneration

Corrections append. Drift closes affected intervals only. Rollback is observed, not merely requested. Regeneration always uses actual state and returns to Publisher for a new PKG/RPKG. No retroactive credit.

## 13. Phase-1 outputs

```text
<publisher run>/packages/<workspace>__<warehouse>.json                           # the Publisher's PKG-*
<state>/realization/ledger/RPKG-*.jsonl                                          # the history (--state-dir, default <output>/state)
<state>/realization/evidence/REVID-*.json
<state>/realization/regeneration/RERUN-*.json
<state>/realization/measurements/<workspace>__<warehouse>__<PKG>__<as-of>.json  # live or demo measurement evidence
<state>/realization/sources/<workspace>__<warehouse>/PKG-*.json                  # source package copies
<state>/realization/policies/PSNAP-*.json
<state>/realization/state/<workspace>__<warehouse>.json                          # state index
<run>/realization/packages/RPKG-*.json                                           # the projections (<output>/run_date=.../run_ts=.../)
<run>/realization/snapshots/RSNAP-*.json
<run>/realizer.log
<run>/manifest.json                                                              # fleet run: run manifest
<run>/realization_workbook.xlsx                                                  # fleet run: realization workbook
<state>/realization/state/<workspace>__<warehouse>.lock                          # per-warehouse lock, while a run holds it
<output>/realization/backtest/evidence-<as-of>.json                              # backtest evidence
<output>/realization/backtest/backtest-<as-of>.json                              # backtest report
```

## 14. Phase-2 boundary

Phase-2 implementation mechanics are intentionally separated from this Phase-1 LLD. Phase 2 is not part of this repository.
