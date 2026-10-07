# High-Level Design — Databricks SQL Warehouse Realizer

> The Realizer specification, kept in line with the code. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED  
**Input:** the Reporter JSON Contract 3.1.0 `RecommendationPackage` the current Publisher writes

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
- Analyzer: post-change metric/workload/regime computation, and the measurement of the package's warehouse (live from system tables, from evidence, or in the demo world) with its counterfactuals and billed cross-check.
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

Phase 1 is the `src/realizer/` package: one capability package per component above, behind the `realize_request()` facade, which calls each capability once and holds no domain logic. A fleet run (`src/realizer/fleet.py`) only sequences warehouses and calls the facade per lineage. The command-line adapter is `src/realizer/cli/` ([LLD](lld.md) section 14). Persistence is local JSON/JSONL/filesystem artifacts. Source execution and authentication are the neutral read-only access layer in `src/workflow_shared/` (OAuth, REST GET, the UTC-pinned SQL client), reached through one bridge module, `src/realizer/analyzer/collector/bridge.py`, which also runs the Publisher's collector SQL, so a configuration epoch, billed daily cost and idle time mean the same in both products.

## 6. Runtime flows

All flows end in the same capability chain (application matcher, ledger, validation, M08/Estimator, attribution, projection); they differ in where the observation comes from.

- **Record only.** A package without an observation records the lineage (its source package copy and a lineage entry) and projects it.
- **Live measurement.** `--package` with `--profile` and `--sql-warehouse-id`. Two read-only passes on a collector SQL warehouse that is never the measured one: pass 1 reads the configuration epochs, the billed daily cost and the last settled billing day; the measurement plan then names the windows, and pass 2 reads them (running idle time and replay rows, running minutes by cluster count, guard metrics, the size runtime). The Analyzer turns them into one observation with the counterfactual of the changed knob and the billed cross-check. What was read is written first as measurement evidence in the state; the run then realizes the observation ([source and evidence contract](../../contracts/realizer/source-evidence-contract.md) section 18).
- **Replay.** `--from-evidence` reads a measurement evidence file a live or demo run wrote and produces the same observation offline, so a live result is reproducible and testable without Databricks.
- **Demo.** `--demo` measures a Publisher demo-fleet package in its simulated world through the same path (source `DEMO_SIMULATION`); the simulated world is never a measurement of a real warehouse.
- **Backtest.** `--backtest` measures auto-stop changes that already happened (no package, no ledger), each with the Publisher's prediction from the days before the change and the realized saving from the days after it, and writes evidence and a report ([section 19](../../contracts/realizer/source-evidence-contract.md)).
- **Fleet run (daily processing).** `--publisher-run` realizes every warehouse of a Publisher run, one at a time under a per-warehouse lock in the state: every open lineage kept in the state (oldest package first), then the run's package, each with its observation from one source (live, demo or an evidence folder). A failure stays with its warehouse. The run writes a run manifest and the realization workbook. A scheduler runs it after each Publisher run, with one `--output-dir` and one state directory, so the history is never forked; weekly Publisher refresh and daily Realizer refresh keep separate authorities.

Daily Realizer runs observe active RPKGs, append application/drift/correction evidence, capture provisional economics, run validation/realization when ready, and project snapshots.

### Lineages of a warehouse

A warehouse can hold several lineages in one state directory (daily packages of one epoch, cost-profile or scope alternatives, the package generated after a change). Each run reads the others and decides, from all of them together, who owns a change (the most recently generated package before it; the others report it observable only), which package supersedes which (a newer package of the same epoch; a successor after a change; a rollback by a later package), and writes what it learns to the lineages concerned. The decisions do not depend on the order the packages are realized in. The rules are in the [ledger contract](../../contracts/realizer/ledger-contract.md) sections 18, 19 and 24.

## 7. Regeneration

Realizer emits a regeneration request containing actual observed config, affected dependencies, reason and evidence. Publisher re-runs and emits a new immutable PKG.

## 8. Supersession

New package generation does not end the old economic interval. Handoff occurs at the effective physical/configuration boundary. A superseded lineage that owns an applied change keeps measuring and claiming it; the rules (same-epoch supersession, successor link, rollback by a later package) are in the [ledger contract](../../contracts/realizer/ledger-contract.md) section 19.

## 9. Phase-2 boundary

Phase 2 is deliberately specified outside this Phase-1 HLD and is not part of this repository. Phase 1 defines the behavioral reference that Phase 2 must preserve.

## 10. Security/failure

Read-only source posture: OAuth profiles only (a static PAT is refused), read-only SQL statements and REST GET, no credential, token or SQL text in a log or in evidence; the owner applies a change, the products never do. Failures isolate by workspace+warehouse+RPKG (a fleet run's exit status is 1 when a warehouse crashed, else 2 when one was refused, else 0). Missing/conflicting material evidence fails closed. Replay is deterministic.
