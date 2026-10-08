# Data model

## Inputs

| Input | Where | Required |
|---|---|---|
| Publisher runs | `<publisher-root>/run_date=YYYY-MM-DD/run_ts=.../` (or one flat run at the root) | yes |
| Packages | `packages/<workspace>__<warehouse>.json` listed in the run's `manifest.json` | yes |
| Replay evidence | `evidence.json` in the run (daily DBU cost, epochs, billing history length) | no |
| Workbook | `workbook.xlsx` in the run (only for `--verify` reconciliation) | no |
| Realizer snapshots | `<realizer-root>/run_date=*/run_ts=*/realization/packages/RPKG-*.json` (or flat `realization/packages/`) | no |
| Realizer ledgers | `<realizer-root>/state/realization/ledger/RPKG-*.jsonl` (or flat `realization/ledger/`) | no |

A Publisher run is read only when `manifest.json` exists (it is written last) and every package it lists is
present. A folder whose manifest has `run_manifest_version` is a Realizer run and is skipped. Each package goes
through `realizer.config.loader.validate_package` (supported contract version, committed schema, ID and full hash);
each RPKG through `schema/realization-package.schema.json`. A failing run or snapshot is listed under Audit with
its reason.

## Embedded dataset (`<script id="dashboard-data">`)

| Key | Content |
|---|---|
| `meta` | generator version, demo flag, ECharts version, raw mode, detail-run count, roots (local builds only), whether Realizer output was found |
| `runs[]` | every run, oldest first: `key` (= `generated_at`), label, versions, `kpis.by_currency`, `insights`, `detail` flag |
| `details[key]` | for the last `--detail-runs` runs: `warehouses[]` (identity, config, costs, plan, scenarios with their execution steps, the recommended steps, candidates, evidence rows, realization lineages with timeline, IDs, versions) and `recommendations[]` (one row per CANDIDATE step with status and aging) |
| `strings`, `evidence_fields` | the string table and field order of the compact evidence rows (`[id, metric, value, unit, ...]`, text fields as indexes) |
| `daily` | newest run only: per warehouse, daily DBU list cost and the configuration epochs |
| `raw` | newest run only: the package JSON per warehouse (`lean` drops the evidence map) |
| `blockers` | meaning of each blocker code used, from `docs/reference/publisher/blocker-codes.md` |
| `skipped[]` | inputs not shown and why |

The dataset is serialized with sorted keys and compact separators, `</` escaped. Only data clocks enter it, so the
same inputs give the same bytes.

## URL hash

`#view=executive|portfolio|recommendations|warehouse|audit&lens=...&run=<generated_at>&wh=<workspace>/<warehouse>&cur=USD&top=spend|savings&pf=...&f.<facet>=a|b&theme=light|dark`
- any view, run, warehouse and filter set is linkable.
