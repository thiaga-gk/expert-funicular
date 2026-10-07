# Live run sequence (Publisher and Realizer)

The order of the first run of both products on a real account, from setup to the review that closes it: the live runs, the mining of the golden scenarios from the same evidence, one applied change measured for four weeks, and the review. It is the one home of the sequence. What each step answers stays with its owner: the Publisher's checklist [`../publisher/real-data-run.md`](../publisher/real-data-run.md) (snapshot retention, SKUs, statement types, the Publisher's assumptions, cost-profile calibration), the Realizer's [`../realizer/real-data-run.md`](../realizer/real-data-run.md) (restart billing, the 0.75 realization factor, the size runtime ratio, floor cuts) and the golden miner's workflow [`SKILL.md`](../../../.claude/skills/databricks-golden-miner/SKILL.md) (mining, proposals, the approval gate, fix briefs). Commands and flags are in the [README](../../../README.md) and the LLDs ([Publisher](../../architecture/publisher/lld.md), [Realizer](../../architecture/realizer/lld.md) section 14).

Every command is read-only. The only change to the account is the one an owner applies in step 3.1, by hand, on a warehouse they agreed to test. Neither product applies anything.

Placeholders: `<P>` the OAuth CLI profile, `<C>` the collector SQL warehouse (it runs the read-only statements and is never the test warehouse), `<T>` the test warehouse, `<R>` the account's AWS region, `<D>` the day-0 date. Everything a run writes goes under `mining_out/<D>/`, which git ignores: the evidence holds real identities, owner emails and tags, and enters git only as an approved live golden case.

## Phase 0 · Prepare (once)

| Step | Do | Check |
|---|---|---|
| 0.1 | Check out master, then `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .` | `publisher --version` and `realizer --version` print the release under test |
| 0.2 | Install the Databricks CLI and log in: `databricks auth login --profile <P>` (OAuth U2M; static tokens are rejected) | `databricks --profile <P> current-user me` answers |
| 0.3 | Choose `<C>`, `<T>` and `<R>` (without `--source-region` the AWS cost of Pro and Classic is not modeled and every cost basis is DBU only). `<T>` is a Pro or Classic warehouse with steady weekday use, an auto-stop of 20 minutes or more and an owner who agrees to one auto-stop change for four weeks | Agreed with the owner of `<T>` |
| 0.4 | Preflight: `python .claude/skills/databricks-golden-miner/scripts/preflight.py --profile <P> --warehouse-id <C>` (it starts `<C>` if stopped) | It passes; on a failure it prints the fix and nothing else runs |
| 0.5 | Optional: the live integration tests (`PUBLISHER_LIVE_PROFILE`, `PUBLISHER_LIVE_SQL_WAREHOUSE_ID`, then `python -m unittest discover -s tests/integration -t .`) | OK or skipped with a reason |

## Phase 1 · Day 0: collect, change nothing

| Step | Do | Check |
|---|---|---|
| 1.1 | Publisher, every warehouse, full scope (the mining input): `publisher --profile <P> --sql-warehouse-id <C> --source-region <R> --warehouse all --flat-output --output-dir mining_out/<D>/pub_full` | `manifest.json`: package count and `unseen_billed_warehouse_count` (it answers snapshot retention on its own); `publisher.log` warnings; `evidence.json` kept for steps 1.4 and 2.1 |
| 1.2 | The aggregate checklist queries: `python .claude/skills/databricks-golden-miner/scripts/real_data_queries.py --profile <P> --warehouse-id <C> --out mining_out/<D>/real_data_answers.json` | Exit 0; aggregates only (no warehouse id, owner or SQL text) |
| 1.3 | Publisher, auto-stop scope: the same as 1.1 with `--scope current:auto-stop --output-dir mining_out/<D>/pub_autostop` | `<T>` has a package with an auto-stop step; note its recommended value |
| 1.4 | Cost-profile replays of 1.1's evidence: `publisher --from-evidence mining_out/<D>/pub_full/evidence.json --cost-profile <profile> --flat-output --output-dir mining_out/<D>/pub_<profile>` for `conservative`, `balanced`, `aggressive` | Per warehouse, which cuts each profile ships or holds, and on which limit |
| 1.5 | Realizer backtest of past auto-stop changes: `realizer --backtest --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir mining_out/<D>/rz` | Per past change: predicted, realized, billed and the cross-check outcome. The Publisher's backtest of the other knobs is not built yet (its checklist, tracker G56) |
| 1.6 | Realizer baseline fleet run over the auto-stop packages: `realizer --publisher-run mining_out/<D>/pub_autostop --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir mining_out/<D>/rz` | Exit 0; every warehouse `NOT_APPLIED` (nothing was changed); the state under `mining_out/<D>/rz/state` now holds one lineage per package |
| 1.7 | Offline replay of 1.6: `realizer --publisher-run mining_out/<D>/pub_autostop --from-evidence mining_out/<D>/rz/state/realization/measurements --output-dir mining_out/<D>/rz_replay` | The same states as 1.6; a difference is a defect to report |

## Phase 2 · Days 0 to 2: mine the golden scenarios

The golden miner reads the day-0 evidence; nothing is collected again. Ask Claude to "mine the golden scenarios from `mining_out/<D>/pub_full`" (the `databricks-golden-miner` skill), or run the scripts by hand. This phase runs alongside phases 3 and 4 and does not wait for the measurement.

| Step | Do | Check |
|---|---|---|
| 2.1 | Mine: `python .claude/skills/databricks-golden-miner/scripts/mine_patterns.py --run-dir mining_out/<D>/pub_full --real-data mining_out/<D>/real_data_answers.json` | `pattern_report.md` and `.json` under the run folder: registry coverage (which golden rows real warehouses show), gaps (real patterns no row covers), behaviour mismatches (the Publisher's decision against a row's expected behaviour), premise contradictions, demo-fleet drift and the real-data answers. Deterministic: the same run gives the same report |
| 2.2 | Proposals, ranked: mismatches on `fail` rows, then premise contradictions, then gaps by warehouse count and cost, then drift. Each is a new row, a live case for an existing row, a mismatch triaged (Publisher bug, signature false positive or row premise contradicted), a premise correction, a demo-fleet parameter change or a real-data answer | One proposal at a time in chat with its real numbers; approve, reject or adjust each (never in a batch). A live case commits that warehouse's evidence as collected: its identity and annual cost are shown before approval |
| 2.3 | Apply the approved ones on a branch `golden-mining-<D>`: live cases (`make_case.py`, then `python scripts/update_goldens.py --case <name> --approved-change "<approval>"`), new rows and signatures, text-only premise corrections, real-data answers in the Publisher checklist | The change loop passes; a case that would fail today is not added here |
| 2.4 | Fix briefs: each approved Publisher bug, and each premise that needs a decision change, becomes one tracker row (`L<NN>`) in [`../publisher/tracker.md`](../publisher/tracker.md), fixed through the remediation runbook's loop on its own branch | `Needs input` where a policy value must be chosen first |
| 2.5 | A demo-fleet parameter change (drift) is a Publisher change that the Realizer's simulated worlds read too: after it, rebuild the Realizer's fixtures (`python scripts/build_realizer_fixtures.py --approved-change "<why>"`) and end-to-end goldens (`python scripts/update_realizer_e2e_goldens.py --stage all --approved-change "<why>"`) and check that no Realizer decision moved unexplained | The Realizer's stored outputs regenerate only with the approval |

The miner grounds the Publisher's scenarios. The Realizer has no miner yet: its real-data evidence is the backtest (1.5) and the live measurements (phase 4), whose evidence files replay offline (`--from-evidence`). Turning a real measurement into a Realizer golden case (a replayed live case next to the simulated end-to-end cases) is not built; the measurement files of this run are its first input if it is.

**Checkpoint A (send back, then review together before anything is changed):**
- `real_data_answers.json` and `pattern_report.md` (aggregates and pattern names; the JSON report names warehouses);
- the counts in 1.1's `manifest.json` and its `publisher.log` warnings;
- the three cost-profile outcomes;
- from `mining_out/<D>/rz`: `realization/backtest/` and the run folders of 1.6 and 1.7 (`manifest.json`, `realization_workbook.xlsx`, `realizer.log`, `realization/`);
- the stdout and exit status of each command.

Packages, workbooks, manifests and the JSON report name warehouses: send them as agreed (or with the identifiers removed), never `evidence.json`. The review closes the snapshot, SKU and statement-type items, takes the miner's proposals one at a time, gives a first answer on restart billing and the 0.75 factor from the backtest, and confirms the change to apply on `<T>`.

## Phase 3 · Day 1: the owner applies one change

| Step | Do | Check |
|---|---|---|
| 3.1 | The owner of `<T>` sets its auto-stop to the value of `<T>`'s package from 1.3, exactly, in the Databricks UI or API | Note the UTC time of the change |
| 3.2 | Optional: an approvals file naming the change, passed as `--approvals` in phase 4, records the origin as confirmed instead of inferred ([`source-evidence-contract.md`](../../contracts/realizer/source-evidence-contract.md) section 18, origin) | |
| 3.3 | Leave every other setting of `<T>` alone for four weeks | Another knob changed in the window is reported as an independent change and stops the billed cross-check from isolating auto-stop |

Auto-stop comes first because its counterfactual is a replay with no guard window. A size change's canary guard keeps its claim observable only for 30 days, so it comes after this run (phase 5).

## Phase 4 · Weeks 1 to 4: measure weekly

Once a week, on the same weekday, with the same `--output-dir` (one state for the whole run):

| Step | Do | Check |
|---|---|---|
| 4.1 | `realizer --publisher-run mining_out/<D>/pub_autostop --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir mining_out/<D>/rz [--approvals <file>]` (add `--warehouse <T>` to measure `<T>` alone when the fleet run is slow) | Exit 0. `<T>`: `APPLIED` and not settled until one whole settled week follows the change day; then a measurement each week over up to 28 settled days |
| 4.2 | Replay the week's evidence as in 1.7, into a new folder | The same states and numbers |
| 4.3 | Optional: a new Publisher run (1.1 into a new dated folder) | `<T>` gets a successor package on its new configuration; the Realizer keeps realizing the original lineage from the state |

**Checkpoint B (after week 1)** and **checkpoint C (after week 4):** send `state/realization/measurements/*.json` (no SQL text, owner or tag), the week's run folder files and the stdout and exit status. For `<T>`: predicted (the package's range), realized (the replay), billed (the cross-check outcome and its reason), and the state, classification and claim status.

## Phase 5 · Review and next changes

1. Review together, per change: predicted against realized against billed, in real-number tables, and every question of both checklists the run answered. Findings go to the Publisher tracker ([`tracker.md`](../publisher/tracker.md)) or, for the Realizer, whose gap review is closed ([archive](../../archive/realizer/gap-review.md)), to a new review; a recalibrated value goes through its own approval, a version bump and regenerated goldens.
2. Mine again after week 4 (step 1.1 and phase 2 on a new date): `<T>` then shows its new epoch, so the miner checks the Publisher's decision on the changed warehouse, and drift is compared with day 0.
3. Next changes, one at a time on agreed warehouses, each measured to a stable number before the next: a size cut (claimable only after the 30-day canary guard, so about five weeks), then a minimum-cluster cut. A plan's later steps are not measured once an earlier step is applied ([Realizer known limitations](../../reference/realizer/known-limitations.md)), so apply one step and measure it before the next.
4. The Publisher's backtest of size, floor and maximum-cluster changes (tracker G56) follows from the same evidence.
5. State retention between runs: [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) section 24.

## Starter prompts

Paste one into a fresh Claude Code session started at the root of a checkout of master on the machine that can reach the account (`claude --model claude-opus-5-5 --effort high`). The first starts the run; the second resumes it each week.

**Day 0 (phases 0 to 2, up to checkpoint A):**

```text
Start the live run of the Publisher and the Realizer on this account. Read CLAUDE.md, then
docs/proposals/shared/live-run.md (the sequence you follow, step by step), the two checklists it links
(docs/proposals/publisher/real-data-run.md, docs/proposals/realizer/real-data-run.md) and
.claude/skills/databricks-golden-miner/SKILL.md (phase 2 runs that skill).

Ask me once, up front: the OAuth CLI profile <P>, the collector SQL warehouse <C>, the AWS region <R>, and
the test warehouse <T> with its owner's agreement (or help me choose one after step 1.1). Use today's UTC
date as <D>.

Rules: every command is read-only, and only the owner of <T> changes anything (step 3.1, by hand). Refuse
a static token profile. Write every output under mining_out/<D>/ and never put evidence.json, a token or a
CLI config into chat, git or a message. Run phase 0 to step 1.7 in order, check each step's "Check"
column, and on a failure stop and show me the command, its exit status and the error. Then run phase 2:
give me the pattern report and take the miner's proposals one at a time in chat (real numbers, then
approve/reject/adjust); apply approved ones on a branch golden-mining-<D> and commit only when I say so.

Stop at checkpoint A: summarize, per step, what ran and what it showed (real numbers in tables), list
the checkpoint A files to send back, give the first answers to the checklist questions the day-0 data
settles (restart billing and the 0.75 factor from the backtest), and recommend the change for <T> with
its package's value. Then wait for me.
```

**Each week (phase 4, checkpoints B and C):**

```text
Resume the live run: read CLAUDE.md and docs/proposals/shared/live-run.md. The run's folder is
mining_out/<D>/; the owner applied the change on <T> at <UTC time> (step 3.1). Run steps 4.1 and 4.2 for
week <N> with the same profile, collector warehouse and region as before, the same --output-dir
(mining_out/<D>/rz) and nothing else changed. Report for <T>: state, classification, claim status,
predicted (the package's range) against realized (the replay) against billed (cross-check outcome and
reason), in a table next to the earlier weeks, and whether the replay matched. At week 1 and week 4 list
the checkpoint files to send back; after week 4 go on to phase 5 (review, second mining, next change)
and propose before doing.
```
