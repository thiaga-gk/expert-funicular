# High-Level Design — Databricks SQL Warehouse Realizer v1.0.0

**Status:** LOCKED  
**Implementation baseline:** Product v2.8.0 repository (evolved from the Publisher v2.7.0 baseline)

## 1. Core invariant

```text
Recommendation truth changes -> NEW PKG
Reality changes              -> NEW RLE
Current realization view     -> NEW RSNAP
Historical facts             -> NEVER rewritten
```

## 2. Bounded responsibilities

- Publisher: recommendation targets, dependencies, predicted economics, immutable PKG.
- Realizer: observed application, ledger, validation coordination, realized-value coordination, attribution, overlap, drift/corrections/rollback/regeneration.
- Analyzer: post-change metric/workload/regime computation.
- Modeler M08: old-state counterfactual quantities under actual post-change workload.
- Estimator: financial valuation.
- Reporter/Workbook: presentation only.

## 3. Logical architecture

```text
PKG + observed evidence
        ↓
Application matcher
        ↓
Append-only ledger
        ↓
Validation readiness -> Analyzer
        ↓
M08 -> Estimator REALIZED
        ↓
Attribution + exclusivity
        ↓
Drift/correction/regeneration evaluation
        ↓
Projection -> RPKG / RSNAP
        ↓
Reporter / Workbook
```

## 4. One PKG -> one RPKG

Replacement recommendation creates a new PKG/RPKG; earlier realization history remains intact.

## 5. Phase 1

Phase 1 is packaged as product v2.8.0, evolved from the existing v2.7.0 Publisher repo. `realizer.py` is a single-file fast-iteration implementation analogous to `publisher.py`. Contracts/schemas/tests remain separate. Persistence is local JSON/JSONL/filesystem artifacts. Source execution and authentication are reused from the existing repo.

## 6. Daily/weekly processing

Daily Realizer runs observe active RPKGs, append application/drift/correction evidence, capture provisional economics, run validation/realization when ready, and project snapshots. Weekly Publisher refresh and Realizer refresh may share orchestration but retain separate authorities.

## 7. Regeneration

Realizer emits a regeneration request containing actual observed config, affected dependencies, reason and evidence. Publisher re-runs and emits a new immutable PKG.

## 8. Supersession

New package generation does not end the old economic interval. Handoff occurs at the effective physical/configuration boundary.

## 9. Phase-2 boundary

Phase 2 is deliberately specified outside this Phase-1 HLD. See `docs/phase2/README.md` and `docs/phase2/hld.md`. Phase 1 defines the behavioral reference that Phase 2 must preserve.

## 10. Security/failure

Read-only source posture in Phase 1; changes remain HITL/external. Failures isolate by workspace+warehouse+RPKG. Missing/conflicting material evidence fails closed. Replay is deterministic.
