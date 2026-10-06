# Product Requirements Document — Databricks SQL Warehouse Realizer v1.0.0

> The Realizer v1.0.0 specification. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED  
**Implementation baseline:** Publisher v2.8.0 repository (evolved from the Publisher v2.7.0 baseline)

## 1. Product statement

Publisher answers **what should change**. Realizer answers **what actually changed, whether it remained safe, what value was actually produced, how confidently the value belongs to the recommendation, and what should happen next**.

## 2. Scope

Phase 1 supports post-change realization for Publisher v2.8.0 SQL Warehouse configuration recommendations: warehouse channel, Photon, size, max/min clusters, auto-stop and warehouse type, subject to source capability.

It supports exact/modified/contrary/unrelated implementation, staged Plan A, Plan B migration, application detection, validation, normalized counterfactual realization, daily value capture, attribution, corrections, drift, rollback, supersession and regeneration.

## 3. Out of scope

Realizer does not mutate warehouse configuration, auto-approve recommendations, replace Analyzer/Modeler/Estimator algorithms, perform query optimization, fabricate unavailable AWS actuals, rewrite PKGs/ledger history, invent candidate allocations, or use LLM judgment for authoritative realized dollars.

## 4. Principles

- reality over recommendation;
- PKG immutability;
- append-only realization history;
- intent/application independence;
- modified implementation is first-class;
- safety precedes savings;
- counterfactual rather than naive before/after;
- observed value != recommendation-attributed value;
- no double counting;
- reporting is presentation, not authority.

## 5. Product entities

`PKG`, `RPKG`, `RLE`, `RSNAP`, `APP`, `VAL`, `MEAS`, `CF`, `REVID`.

## 6. Functional requirements

### Identity/ledger
- one logical RPKG per PKG;
- append-only idempotent ledger;
- current RSNAP derived from ledger;
- preserve effective vs recorded time.

### Application
- compare source/recommended/observed authoritative state;
- detect application without requiring confirmation;
- never synthesize ACCEPTED;
- classify `EXACT|MODIFIED|NOT_APPLIED|CONTRARY|UNRELATED`;
- preserve source/recommended/actual values;
- allow modified realization;
- use actual state as regeneration baseline.

### Plan
- step realization independent from aggregate plan progress;
- completed steps can realize in a partially completed plan;
- never credit pending re-evaluation scope;
- request regeneration after invalidating changes.

### Validation
- Policy-driven readiness;
- representative workload/regime required;
- Analyzer owns post-change metrics;
- hard guardrails dominate monetary savings;
- multiple validation attempts supported.

### Counterfactual/economics
- M08 old state under actual post-change workload;
- Estimator REALIZED owns money;
- signed negative value retained;
- DBU/AWS economic/cash components distinguished;
- posted-dollar and constant-price bases distinguished;
- savings-to-date, annualized and mature TTM remain distinct.

### Attribution/exclusivity
- application match != causal attribution;
- observed and recommendation-attributed value separate;
- scenario/plan attribution when candidate split is indefensible;
- one economic atom/basis/interval has one authoritative owner.

### Corrections/drift/rollback
- append corrections;
- preserve prior snapshots;
- close only affected intervals on drift;
- rollback confirmed by observed state;
- preserve failed experiments.

### Regeneration/supersession
- regeneration returns to Publisher;
- actual state is new baseline;
- new recommendation creates new PKG/RPKG;
- supersession alone does not transfer economic ownership.

### Reporter/Workbook
- preserve locked Reporter/Workbook contracts;
- canonical reporting source is PKG + RPKG;
- safe deterministic legacy projection only;
- never fabricate unknown actor;
- workbook remains presentation-only.

## 7. Non-functional requirements

Determinism, idempotency, reproducibility, auditability, immutability, fail-closed evidence handling, decimal financial precision, zero overlap, least privilege/read-only source access, resilience by warehouse/RPKG, replayable corrections, contract validation, testability without live Databricks, Phase-1/Phase-2 semantic parity and explainability.

## 8. Phase-1 Definition of Done

All locked contracts validate; GR-001..040 are implemented; non-waivable goldens pass; Publisher regression passes; Publisher→Realizer→Workbook E2E passes; financial reconciliation shows zero overlap; deterministic replay and duplicate-event idempotency pass; `/code-review low` and release evidence complete.
