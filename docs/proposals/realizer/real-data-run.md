# Realizer real-data run

What cannot be closed from code or simulation alone: the Realizer's measurements on a real account. The demo world is a consistency check (its noise depends only on the scenario and the day), not accuracy evidence; only real warehouses show whether the replay, the size runtime ratio and the floor cut hold. Until each question below is answered the Realizer fails safe: a counterfactual it cannot establish is named and unpriced, never guessed, and a saving whose billed cross-check disagrees is not claimed. The Publisher's own checklist, which this run extends, is [`../publisher/real-data-run.md`](../publisher/real-data-run.md) and its live run supplies the packages the Realizer reads.

This page points to where each command and meaning lives and does not restate them: commands and flags in the [README](../../../README.md#realizer) and [`lld.md`](../../architecture/realizer/lld.md) section 14; measurement, counterfactuals, backtest and evidence formats in [`source-evidence-contract.md`](../../contracts/realizer/source-evidence-contract.md) sections 16 to 19; lineages, the state directory, the fleet run and its exit status in [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) sections 18, 19 and 24; what the Realizer cannot measure yet in [`known-limitations.md`](../../reference/realizer/known-limitations.md).

## Before you start

- Install from a checkout (`python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .`). The Databricks CLI must be on `PATH`.
- An OAuth U2M CLI profile (`databricks auth login`). Static personal access tokens are rejected; never paste a token anywhere.
- A collector SQL warehouse: the Realizer's collector statements run on it. It must never be a warehouse the run measures or backtests (the run refuses it by name for a backtest; for a live measurement it is the operator's rule, because the statements would change the measured warehouse's idle time and cost).
- The AWS region of the account (`--source-region`) for the modeled AWS cost of Pro and Classic; without it the cost basis is DBU only.
- Every command is read-only. The owner applies a change in the Databricks UI or API; neither product does.

## Order of the run

1. **Publisher packages.** One live Publisher run scoped to auto-stop first, then the full scope (the commands and the Publisher's own open items are in its runbook): `publisher --profile <p> --sql-warehouse-id <collector> --source-region <region> --scope current:auto-stop --output-dir ./real_out`, then the same without `--scope`. Keep the run folders; the Realizer reads their `packages/`. Do not keep or send `evidence.json` (it holds owner emails and tags).
2. **Backtest.** `realizer --backtest --profile <p> --sql-warehouse-id <collector> --source-region <region> --output-dir ./rz_real` over every past auto-stop change in the last 365 days (`--warehouse <id>`, repeatable, to focus). Each change is reported with the Publisher-style prediction from the days before it, the realized saving from the days after it and the billed cross-check. No package, no ledger, and no waiting: this is the first real evidence for the questions below.
3. **One live package.** The owner applies one change recommended by a package on a test warehouse (an owner action; note the time). Then `realizer --package <PKG.json> --profile <p> --sql-warehouse-id <collector> --source-region <region> --output-dir ./rz_real`. Nothing is measured before one whole settled week after the change; allow about four weeks for a stable number, and run it again as the weeks settle (each run adds a measurement to the lineage). Pass `--approvals` if the change is to be recorded as confirmed.
4. **Fleet run.** `realizer --publisher-run <Publisher run folder or root> --profile <p> --sql-warehouse-id <collector> --source-region <region> --output-dir ./rz_real` realizes every warehouse of the Publisher run and every open lineage kept in the state; keep one `--output-dir` (so one state) across runs. Check the exit status (1: a warehouse crashed; 2: one was refused) and the per-warehouse lines.
5. **Replay.** The evidence files of steps 3 and 4 replay offline to the same observations: `realizer --package <PKG.json> --from-evidence <file>`, or for a fleet `realizer --publisher-run <folder> --from-evidence ./rz_real/state/realization/measurements --output-dir ./rz_replay`. A replay that differs from the live result is a defect to report.
6. Review together, per change: predicted, realized and billed, the cross-check outcome and its reason, and the questions below.

## What to send back

From `./rz_real`:

- `state/realization/measurements/*.json` (measurement evidence: no SQL text, statement source, owner or tag);
- `realization/backtest/evidence-*.json` and `backtest-*.json`;
- from each run folder: `manifest.json`, `realization_workbook.xlsx`, `realizer.log`, `realization/packages/` and `realization/snapshots/`;
- the Realizer's stdout lines of each run and its exit status.

Never send the Publisher's `evidence.json`, a credential or a CLI config file. Send the Publisher's `packages/*.json` only when asked; they name warehouses.

## Questions the run must settle

Each is answered from the backtest report and the live measurements; a calibration of any value goes through its own approval, a version bump and regenerated goldens.

1. **Restart billing.** The replay charges 4 cluster-minutes per Pro/Classic restart (`restart_cost_cluster_minutes_pro_classic`); the demo simulator bills none, so restart-heavy changes read `DISAGREES` in simulation. Are starting minutes billed on the real account? Compare the cross-check outcome and the restarts per query before and after (in the report) for changes that moved the restart count, and the billed DBU around `STARTING` events (the Publisher runbook's restart-billing item). The answer fixes both products' restart cost.
2. **The 0.75 realization factor.** The Publisher's base case reports 0.75 of the replayed idle removed; the Realizer's replay measures the full mechanical effect and lands at the published high. Compare, per auto-stop change, predicted against realized against the billed change. If realized sits near the prediction at full effect, the factor understates; if the billed change sits near 0.75 of it, it holds.
3. **Size runtime ratio on real workloads.** The ratio is measured only when the matched statement sources hold at least 300 post-change queries (the canary p95 sample) and at least half of the post-change window's executed queries, and the measured rate ratio must be within the 25 percent band of the published DBU-per-cluster-hour ratio. How often are both true (`SIZE_RUNTIME_UNMEASURED`, `SOURCE_RATE_UNAVAILABLE` otherwise)? Is the measured runtime ratio inside the Publisher's published range for the direction? Which bound, elastic or inelastic, does the billed cross-check track on real workloads?
4. **Floor cuts and cluster levels.** Do the running minutes by cluster count (the levels) of a real minimum-cluster cut give a counterfactual the billed cross-check agrees with, and do maximum-cluster changes read plausibly on their guards?
5. **Shared with the Publisher.** Warehouse snapshot retention, SKU names and tiers, statement types, Pro/Classic DBU per cluster-hour by size, list against negotiated prices, `cluster_count` on `STARTING` events, auto-stop replay fidelity and whether a warehouse with no state change emits events are in [the Publisher's runbook](../publisher/real-data-run.md); the same answers apply here.

## Known limitation to keep in mind

In a plan applied step by step, the live measurement measures the first applied step only: a size measurement holds the new auto-stop, so its number is right, but a later step's own saving is never measured. Apply one step at a time and measure it before the next, or expect the later step to be observed and not valued ([`known-limitations.md`](../../reference/realizer/known-limitations.md)).

## After the run

State retention (what can be deleted, what to keep) is in [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) section 24. Findings and decisions that follow from the run go to [`gap-review.md`](gap-review.md) and its tracker; the run's results are presented per change with real-number tables before any fix is proposed.
