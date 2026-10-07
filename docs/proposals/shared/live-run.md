# Live run sequence (Publisher and Realizer)

The order of the first run of both products on a real account, from setup to the review that closes it. It is the one home of the sequence. What each step answers stays with its product's checklist: [`../publisher/real-data-run.md`](../publisher/real-data-run.md) (snapshot retention, SKUs, statement types, the Publisher's assumptions, cost-profile calibration) and [`../realizer/real-data-run.md`](../realizer/real-data-run.md) (restart billing, the 0.75 realization factor, the size runtime ratio, floor cuts). Commands and flags are in the [README](../../../README.md) and the LLDs ([Publisher](../../architecture/publisher/lld.md), [Realizer](../../architecture/realizer/lld.md) section 14).

Every command is read-only. The only change to the account is the one an owner applies in step 2.1, by hand, on a warehouse they agreed to test. Neither product applies anything.

Placeholders: `<P>` the OAuth CLI profile, `<C>` the collector SQL warehouse (it runs the read-only statements and is never the test warehouse), `<T>` the test warehouse, `<R>` the account's AWS region. Folders below are under one working folder `./live`.

## Phase 0 · Prepare (once)

| Step | Do | Check |
|---|---|---|
| 0.1 | Check out master, then `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .` | `publisher --version` and `realizer --version` print the release under test |
| 0.2 | Install the Databricks CLI and log in: `databricks auth login --profile <P>` (OAuth U2M; static tokens are rejected) | `databricks --profile <P> current-user me` answers |
| 0.3 | Choose `<C>`, `<T>` and `<R>` (without `--source-region` the AWS cost of Pro and Classic is not modeled and every cost basis is DBU only). `<T>` is a Pro or Classic warehouse with steady weekday use, an auto-stop of 20 minutes or more and an owner who agrees to one auto-stop change for four weeks | Agreed with the owner of `<T>` |
| 0.4 | Preflight: `python .claude/skills/databricks-golden-miner/scripts/preflight.py --profile <P> --warehouse-id <C>` | It passes (it fails closed) |
| 0.5 | Optional: the live integration tests (`PUBLISHER_LIVE_PROFILE`, `PUBLISHER_LIVE_SQL_WAREHOUSE_ID`, then `python -m unittest discover -s tests/integration -t .`) | OK or skipped with a reason |

## Phase 1 · Day 0: observe, change nothing

| Step | Do | Check |
|---|---|---|
| 1.1 | The aggregate checklist queries: `python .claude/skills/databricks-golden-miner/scripts/real_data_queries.py --profile <P> --warehouse-id <C> --out ./live/real_data_answers.json` | Exit 0; aggregates only |
| 1.2 | Publisher, full scope: `publisher --profile <P> --sql-warehouse-id <C> --source-region <R> --flat-output --output-dir ./live/pub_full` | `manifest.json`: package count, `unseen_billed_warehouse_count`; `publisher.log` warnings. Keep `evidence.json` locally for 1.4 (owner emails and tags: never send it) |
| 1.3 | Publisher, auto-stop scope: the same with `--scope current:auto-stop --output-dir ./live/pub_autostop` | `<T>` has a package with an auto-stop step; note its recommended value |
| 1.4 | Cost-profile replays of 1.2's evidence: `publisher --from-evidence ./live/pub_full/evidence.json --cost-profile <profile> --flat-output --output-dir ./live/pub_<profile>` for `conservative`, `balanced`, `aggressive` | Per warehouse, which cuts each profile ships or holds, and on which limit |
| 1.5 | Realizer backtest of past auto-stop changes: `realizer --backtest --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir ./live/rz` | Per past change: predicted, realized, billed and the cross-check outcome. The Publisher's backtest of the other knobs is not built yet (its checklist, tracker G56) |
| 1.6 | Realizer baseline fleet run over the auto-stop packages: `realizer --publisher-run ./live/pub_autostop --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir ./live/rz` | Exit 0; every warehouse `NOT_APPLIED` (nothing was changed); the state under `./live/rz/state` now holds one lineage per package |
| 1.7 | Offline replay of 1.6: `realizer --publisher-run ./live/pub_autostop --from-evidence ./live/rz/state/realization/measurements --output-dir ./live/rz_replay` | The same states as 1.6; a difference is a defect to report |

**Checkpoint A (send back, then review together before anything is changed):**
- `real_data_answers.json`;
- the counts in 1.2's `manifest.json` and its `publisher.log` warnings;
- the three cost-profile outcomes;
- from `./live/rz`: `realization/backtest/` and the run folders of 1.6 and 1.7 (`manifest.json`, `realization_workbook.xlsx`, `realizer.log`, `realization/`);
- the stdout and exit status of each command.

Packages, workbooks and manifests name warehouses: send them as agreed (or with the identifiers removed). The review answers the snapshot, SKU and statement-type items, gives a first answer on restart billing and the 0.75 factor from the backtest, and confirms the change to apply on `<T>`.

## Phase 2 · Day 1: the owner applies one change

| Step | Do | Check |
|---|---|---|
| 2.1 | The owner of `<T>` sets its auto-stop to the value of `<T>`'s package from 1.3, exactly, in the Databricks UI or API | Note the UTC time of the change |
| 2.2 | Optional: an approvals file naming the change, passed as `--approvals` in phase 3, records the origin as confirmed instead of inferred ([`source-evidence-contract.md`](../../contracts/realizer/source-evidence-contract.md) section 18, origin) | |
| 2.3 | Leave every other setting of `<T>` alone for four weeks | Another knob changed in the window is reported as an independent change and stops the billed cross-check from isolating auto-stop |

Auto-stop comes first because its counterfactual is a replay with no guard window. A size change's canary guard keeps its claim observable only for 30 days, so it comes after this run (phase 4).

## Phase 3 · Weeks 1 to 4: measure weekly

Once a week, on the same weekday, with the same `--output-dir` (one state for the whole run):

| Step | Do | Check |
|---|---|---|
| 3.1 | `realizer --publisher-run ./live/pub_autostop --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir ./live/rz [--approvals <file>]` (add `--warehouse <T>` to measure `<T>` alone when the fleet run is slow) | Exit 0. `<T>`: `APPLIED` and not settled until one whole settled week follows the change day; then a measurement each week over up to 28 settled days |
| 3.2 | Replay the week's evidence as in 1.7, into a new folder | The same states and numbers |
| 3.3 | Optional: a new Publisher run (1.2 into a new folder) | `<T>` gets a successor package on its new configuration; the Realizer keeps realizing the original lineage from the state |

**Checkpoint B (after week 1)** and **checkpoint C (after week 4):** send `state/realization/measurements/*.json` (no SQL text, owner or tag), the week's run folder files and the stdout and exit status. For `<T>`: predicted (the package's range), realized (the replay), billed (the cross-check outcome and its reason), and the state, classification and claim status.

## Phase 4 · Review and next changes

1. Review together, per change: predicted against realized against billed, in real-number tables, and every question of both checklists the run answered. Findings go to the gap reviews ([Publisher](../publisher/tracker.md), [Realizer](../realizer/gap-review.md)); a recalibrated value goes through its own approval, a version bump and regenerated goldens.
2. Next changes, one at a time on agreed warehouses, each measured to a stable number before the next: a size cut (claimable only after the 30-day canary guard, so about five weeks), then a minimum-cluster cut. A plan's later steps are not measured once an earlier step is applied ([Realizer known limitations](../../reference/realizer/known-limitations.md)), so apply one step and measure it before the next.
3. The Publisher's backtest of size, floor and maximum-cluster changes (tracker G56) and the golden miner's proposals (`.claude/skills/databricks-golden-miner/SKILL.md`) follow from the same evidence.
4. State retention between runs: [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) section 24.
