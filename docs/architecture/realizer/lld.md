# Low-Level Design — Databricks SQL Warehouse Realizer

> The Realizer specification, kept in line with the code. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED  
**Input:** the Reporter JSON Contract 3.1.0 `RecommendationPackage` the current Publisher writes

## 1. Phase-1 structure

```text
src/realizer/realizer.py        # facade: realize_request() calls each capability once; realize_fleet(), measurement and backtest entry points
src/realizer/fleet.py           # fleet run: sequences the warehouses of a Publisher run, holds the per-warehouse lock, no domain logic
src/realizer/cli/               # command-line adapter (`realizer`, `python -m realizer`): main.py (exit status), realize.py (arguments)
src/realizer/config/            # settings.py (versions), loader.py (checked inputs), output_layout.py (run folders, state directory, lock path)
src/realizer/contracts/         # typed contracts (realization, observation, measurement, backtest, approvals, fleet, lineage, report), JSON Schema builders
src/realizer/analyzer/          # analysis.py, measurement.py (measurement plan), projector.py (auto-stop replay), size_counterfactual.py,
                                #   cluster_counterfactual.py, migration_observation.py, crosscheck.py, backtest.py, lineages.py (other lineages of the warehouse)
src/realizer/analyzer/collector/  # collector.py (live, replay and observation entry points), bridge.py (the one module that reaches workflow_shared
                                #   and the Publisher's collector SQL), demo_world.py (simulated world), backtest_collector.py
src/realizer/governor/          # policy.py (RealizerPolicy), governor.py
src/realizer/reconciler/        # matching.py, lifecycle.py, lineage.py (ownership, supersession, rollback), policy_diff.py
src/realizer/estimator/         # economics, attribution, performance
src/realizer/reviewer/          # candidate and package gates
src/realizer/packager/          # ledger.py, snapshot.py, lineage.py (lineages of a state directory, locks), lineage_projection.py, to_date.py (realized to date)
src/realizer/reporter/          # reporter.py (packages, snapshots), workbook.py (realization workbook), run_manifest.py (fleet run manifest)
schema/realization-*.json       # committed JSON Schemas
docs/*/realizer/                # Realizer specifications
tests/                          # unit, component, golden, regression (stored baseline), end-to-end goldens
```

Each capability package holds one component of the chain; the facade calls each once per realized package. The neutral read-only Databricks access (OAuth, REST GET, the UTC-pinned SQL client) is `src/workflow_shared/`, which imports neither product; only `bridge.py` reaches it and the Publisher's collector SQL, and the CLI never imports a capability package. The single-file core the package grew out of is a historical record ([`repo-reorg-design.md`](repo-reorg-design.md)).

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

M08 receives old PlanState + actual post-change workload/regime. Estimator REALIZED computes money. Realizer classifies/attributes but does not invent financial formulas. The counterfactuals are measured in the Analyzer from system tables (or replayed from evidence), each with the other knobs held at their observed values, and every priced saving is cross-checked against the billed change:

- auto-stop (`projector.py`): the actual post-change gaps replayed with the old value, less the restarts the old value would have avoided;
- size (`size_counterfactual.py`): the post-change days priced at the old size from the measured query runtime ratio, with an elastic and an inelastic bound of the old size's running time, the claimed bound being the one with the smaller saving;
- minimum clusters (`cluster_counterfactual.py`): a floor cut as the cluster-minutes the old floor would have kept over the observed levels; a floor raise is never measured;
- maximum clusters: a raise is a measured cost, a cut a bound on what the old cap could have added, no saving claimed;
- warehouse type (`migration_observation.py`): the billed change, observed and never claimed.

Formulas, thresholds and worked examples are in the [source and evidence contract](../../contracts/realizer/source-evidence-contract.md) section 18; the replay of a backtest is section 19.

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

## 14. Command line

`realizer` and `python -m realizer` (and `python scripts/run_realizer.py` from a checkout) are one adapter, `src/realizer/cli/`: it parses and validates its own arguments, resolves the output layout, configures logging (UTC timestamps on stderr, and the run folder's `realizer.log`), builds the request and calls the facade. It never imports a capability package. Three forms:

```text
realizer --package <PKG.json> [observation source] [options]      # one package
realizer --publisher-run <run folder or root> [source] [options]   # a fleet run
realizer --backtest [source] [options]                             # past auto-stop changes, no package
```

`--package` and `--publisher-run` are mutually exclusive and one is required. Every refusal is a one-line message naming options, never values, and writes nothing.

**Options of a package run and a fleet run**

| Option | Meaning |
|---|---|
| `--package <file>` | The immutable `RecommendationPackage` JSON (Reporter JSON Contract 3.1.0); checked against the schema and its id and full hash recomputed. |
| `--publisher-run <dir>` | Fleet run over a Publisher run folder, or over the newest `run_date=*/run_ts=*` folder under a root (never a folder the Realizer wrote). |
| `--warehouse <id>` | Fleet run only, repeatable: only this warehouse (`<warehouse_id>` or `<workspace_id>/<warehouse_id>`); default every warehouse of the manifest. |
| `--observation <file>` | A normalized post-change observation (format 3.1.0). One observation source per run. |
| `--demo` / `--demo-modified` | Measure a Publisher demo-fleet package in its simulated world; `--demo-modified` applies a size step to MEDIUM as SMALL (package run only). |
| `--profile <p>` with `--sql-warehouse-id <id>` | Live measurement from system tables (OAuth profile, read-only). The SQL warehouse runs the collector statements and is never the measured one. |
| `--from-evidence <path>` | Replay measurement evidence: a file for a package run, a folder (each package's newest file) for a fleet run. |
| `--databricks-config-file`, `--source-region`, `--aws-price-registry`, `--as-of`, `--billing-ingested-through` | Live (and demo for `--as-of`): the CLI config file, the AWS region and EC2 price registry of the modeled AWS cost, the measurement time (default now; demo: 28 settled days after the change), and a billing ingestion cutoff. |
| `--approvals <file>` | Approvals (format 1.0.0) naming approved changes; their origin is then `CONFIRMED`. Needs a live or replayed measurement for a package run; any source for a fleet run. |
| `--policy <file>` | Realization policy override. |
| `--replay-through <UTC time>` | Project only ledger entries recorded through that time (package run). |
| `--print-json` | Print the snapshot instead of the written package path (package run). |
| `--output-dir <dir>` | Root of the run folders; default `./realizer_out`. |
| `--state-dir <dir>` | The long-lived state (ledgers, evidence, regeneration requests, measurements, source package copies, policies, state index); default `<output-dir>/state`. |
| `--flat-output` | Write the state and the projections under `--output-dir` itself: no run folder, no log file. |
| `--log-level` | `DEBUG`, `INFO` (default), `WARNING`, `ERROR`. |
| `--version` | The Realizer, release and contract versions. |

Without an observation source a package run only records the lineage. `--observation`, `--demo`, `--profile` and `--from-evidence` are mutually exclusive. A fleet run needs exactly one of `--profile`, `--demo` and `--from-evidence <folder>` and refuses the per-package options (`--observation`, `--demo-modified`, `--replay-through`, `--print-json`). `--warehouse` belongs to a fleet run (or to a live backtest).

**Options of `--backtest`** (`realizer --backtest --help`): no package (`--package` is refused); one source, `--profile` with `--sql-warehouse-id`, `--from-evidence <backtest evidence file>` or `--demo`; `--warehouse` (repeatable, live only), `--databricks-config-file`, `--source-region` (without it the cost basis is DBU only), `--aws-price-registry`, `--as-of`, `--billing-ingested-through`, `--output-dir`, `--log-level`. It writes `realization/backtest/evidence-<as-of>.json` and `backtest-<as-of>.json` under `--output-dir` (no run folder, no state), prints one line per past change and the report path.

**Output layout.** Each package or fleet run creates a Hive-style run folder `<output-dir>/run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/` (the UTC start of the run, never reused) for its projections (`realization/packages/`, `realization/snapshots/`), `realizer.log` and, for a fleet run, `manifest.json` and `realization_workbook.xlsx`. The history lives in the state directory, which outlives the runs: a scheduler that keeps one `--output-dir` never forks it. A demo or live measurement is written to the state before the run folder is created. `--flat-output` makes the state directory and the run folder one directory. Paths under each root are the same in both layouts (section 13).

**Exit status**

| Run | 0 | 1 | 2 |
|---|---|---|---|
| Package run, backtest | success | an unexpected exception (a defect: logged by type only, then the traceback) | a refused input or run (a `RealizerError`: an input the Realizer does not accept, an unreadable file or ledger) or bad arguments |
| Fleet run | every warehouse `OK` | any warehouse `CRASHED` | no crash and any warehouse `REFUSED`; also a refusal before any warehouse starts (an option, the Publisher run, the approvals or policy file), which writes nothing |

A fleet run's per-warehouse lines (`<workspace>/<warehouse>: <status>`), the manifest path and the workbook path go to stdout.

## 15. Phase-2 boundary

Phase-2 implementation mechanics are intentionally separated from this Phase-1 LLD. Phase 2 is not part of this repository.
