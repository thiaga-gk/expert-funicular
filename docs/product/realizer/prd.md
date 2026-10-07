# Product Requirements Document — Databricks SQL Warehouse Realizer

> The Realizer specification, kept in line with the code. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED  
**Input:** the `RecommendationPackage` of the Reporter JSON Contract 3.1.0 (the only version accepted), as the current Publisher writes it

## 1. Product statement

Publisher answers **what should change**. Realizer answers **what actually changed, whether it remained safe, what value was actually produced, how confidently the value belongs to the recommendation, and what should happen next**.

## 2. Scope

Phase 1 supports post-change realization for the SQL Warehouse configuration recommendations of the current Publisher: size, max/min clusters, auto-stop and warehouse type. The warehouse channel and Photon are observed facts, never changes: they are recorded, never matched to a step, claimed or counted as drift. A warehouse-type migration is observe-only: the bill's delta is reported and nothing is claimed. A maximum-cluster change has no claimable saving: a cut is judged on queueing and priced at zero, an increase is a measured cost.

It supports exact/modified/contrary/unrelated implementation, staged Plan A, Plan B migration, application detection, validation, normalized counterfactual realization, daily value capture, attribution, corrections, drift, rollback, supersession and regeneration. Value is measured for three knobs: an auto-stop change by replaying the old value over the actual post-change days, a size change by pricing the post-change days at the old size from the measured query runtime ratio, and a minimum-cluster cut by the cluster-minutes the old floor would have kept. Every measurement carries a billed cross-check.

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

`PKG`, `RPKG`, `RLE`, `RSNAP`, `APP`, `VAL`, `MEAS`, `CF`, `REVID` (identities: [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) section 2).

A lineage is one package's realization (one RPKG) in the history of its warehouse; a warehouse can hold several (daily packages, profile alternatives, the package after a change). A counterfactual is the old state under the actual post-change workload, measured from system tables or replayed from evidence. A claim status (`CLAIMABLE`, `OBSERVABLE_ONLY`, `NOT_ATTRIBUTABLE`) says whether a lineage's measured value counts toward the recommendation or is only reported.

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

### Live measurement and replay
- measure the package's warehouse from system tables, read-only (OAuth profile; the collector statements run on a SQL warehouse that is never the measured one);
- write what was read as measurement evidence, and replay it (`--from-evidence`) to the same observation;
- measure only whole settled weeks after the change; a counterfactual the evidence cannot establish is named, never guessed;
- cross-check every measured saving against the billed change;
- simulate the measurement in a deterministic demo world (`--demo`) for demos and end-to-end tests.

### Backtest
- measure auto-stop changes that already happened, with the prediction the Publisher would have made and the realized saving, without a package or a ledger;
- write the evidence and a report, and replay them.

### Fleet run
- realize every warehouse of a Publisher run in one command, one warehouse at a time under a lock;
- keep every open lineage of a warehouse in the state and realize it on each run;
- isolate a failure to its warehouse; write a run manifest and the realization workbook;
- exit status: 1 when any warehouse crashed, else 2 when any was refused, else 0.

### Lineages and ownership
- several packages of one warehouse share one state directory;
- one change belongs to one lineage (the most recently generated package before it whose step it realizes); the others report it as observable only;
- a newer package of the same epoch supersedes the older one; a package after a change is linked as its successor; supersession never stops a valid realization;
- the same decisions whatever the order the packages are realized in.

### Reporter/Workbook
- preserve locked Reporter/Workbook contracts: the Publisher's workbook is never changed by the Realizer;
- canonical reporting source is PKG + RPKG;
- safe deterministic legacy projection only;
- never fabricate unknown actor;
- the Realizer writes its own three-sheet realization workbook (Warehouses, Lineages, Timeline) per fleet run, presentation-only.

## 7. Non-functional requirements

Determinism, idempotency, reproducibility, auditability, immutability, fail-closed evidence handling, decimal financial precision, zero overlap, least privilege/read-only source access, resilience by warehouse/RPKG, replayable corrections, contract validation, testability without live Databricks (a simulated demo world and evidence replay), Phase-1/Phase-2 semantic parity and explainability.

## 8. Phase-1 Definition of Done

All locked contracts validate; GR-001..040 are implemented; non-waivable goldens pass; Publisher regression passes; Publisher→Realizer→Workbook E2E passes; financial reconciliation shows zero overlap; deterministic replay and duplicate-event idempotency pass; `/code-review low` and release evidence complete.
