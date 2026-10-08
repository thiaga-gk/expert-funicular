# Live run sequence (Publisher and Realizer)

The order of the first run of both products on a real account: mine the golden scenarios from live evidence, fix what the mining finds, run the Publisher's flows on the fixed code, then the Realizer's (its backtest, a baseline, one applied change measured for four weeks), and review. It is the one home of the sequence; the [starter prompts](#starter-prompts) at the end drive it. What each step answers stays with its owner: the Publisher's checklist [`../publisher/real-data-run.md`](../publisher/real-data-run.md) (snapshot retention, SKUs, statement types, the Publisher's assumptions, cost-profile calibration), the Realizer's [`../realizer/real-data-run.md`](../realizer/real-data-run.md) (restart billing, the 0.75 realization factor, the size runtime ratio, floor cuts), the golden miner's workflow [`SKILL.md`](../../../.claude/skills/databricks-golden-miner/SKILL.md) (mining, proposals, the approval gate, fix briefs) and the fix loop of [`../publisher/remediation-runbook.md`](../publisher/remediation-runbook.md). Commands and flags are in the [README](../../../README.md) and the LLDs ([Publisher](../../architecture/publisher/lld.md), [Realizer](../../architecture/realizer/lld.md) section 14).

Every command against the account is read-only. The only change to the account is the one an owner applies in step 4.4, by hand, on a warehouse they agreed to test. Neither product applies anything.

Placeholders: `<P>` the OAuth CLI profile, `<C>` the collector SQL warehouse (it runs the read-only statements and is never the test warehouse), `<T>` the test warehouse, `<R>` the account's AWS region, `<D>` the date of a run. Everything a run writes goes under `mining_out/<D>/`, which git ignores: the evidence holds real identities, owner emails and tags, and enters git only as an approved live golden case.

## Phase 0 · Prepare (once)

| Step | Do | Check |
|---|---|---|
| 0.1 | Check out master, then `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .` | `publisher --version` and `realizer --version` print the release under test; the full test suite passes |
| 0.2 | Install the Databricks CLI and log in: `databricks auth login --host https://<workspace> --profile <P>` (OAuth U2M; static tokens are rejected) | `databricks --profile <P> current-user me` answers |
| 0.3 | Choose `<C>` and `<R>`. `<R>` must be a region in `pricing/aws_ec2_on_demand.json` (today `us-east-1`): with Pro or Classic warehouses in any other region the Publisher stops after collection with only `evidence.json` and `publisher.log` written (replay with `--from-evidence`), unless every Publisher command adds `--allow-missing-aws-price` (their AWS cost is then unavailable). The Realizer falls back to DBU only | `<R>` is in the pricing registry |
| 0.4 | Preflight: `python .claude/skills/databricks-golden-miner/scripts/preflight.py --profile <P> --warehouse-id <C>` (it starts `<C>` if stopped) | It passes; on a failure it prints the fix and nothing else runs |
| 0.5 | Optional: the live integration tests (`PUBLISHER_LIVE_PROFILE`, `PUBLISHER_LIVE_SQL_WAREHOUSE_ID`, `PUBLISHER_LIVE_SOURCE_REGION=<R>`, then `python -m unittest discover -s tests/integration -t .`) | OK or skipped with a reason |

## Phase 1 · Mine the golden scenarios

| Step | Do | Check |
|---|---|---|
| 1.1 | Collect through the Publisher, every warehouse, full scope: `publisher --profile <P> --sql-warehouse-id <C> --source-region <R> --warehouse all --flat-output --output-dir mining_out/<D>/pub_full` | `manifest.json`: package count and `unseen_billed_warehouse_count` (it answers snapshot retention on its own); `publisher.log` warnings; `evidence.json` kept locally |
| 1.2 | The aggregate checklist queries: `python .claude/skills/databricks-golden-miner/scripts/real_data_queries.py --profile <P> --warehouse-id <C> --out mining_out/<D>/real_data_answers.json` | Exit 0; aggregates only (no warehouse id, owner or SQL text) |
| 1.3 | Mine: `python .claude/skills/databricks-golden-miner/scripts/mine_patterns.py --run-dir mining_out/<D>/pub_full --real-data mining_out/<D>/real_data_answers.json` | `pattern_report.md` and `.json`: registry coverage (which golden rows real warehouses show), gaps (real patterns no row covers), behaviour mismatches (the Publisher's decision against a row's expected behaviour), premise contradictions, demo-fleet drift and the real-data answers. Deterministic |
| 1.4 | Proposals, ranked: mismatches on `fail` rows, then premise contradictions, then gaps by warehouse count and cost, then drift. Each is a new row, a live case for an existing row, a mismatch triaged (Publisher bug, signature false positive or row premise contradicted), a premise correction, a demo-fleet parameter change or a real-data answer | One proposal at a time in chat with its real numbers; approve, reject or adjust each (never in a batch). A live case commits that warehouse's evidence as collected: its identity and annual cost are shown before approval |
| 1.5 | Apply the approved proposals that pass today on a branch `golden-mining-<D>`: live cases (`make_case.py`, then `python scripts/update_goldens.py --case <name> --approved-change "<approval>"`), new rows and signatures, text-only premise corrections, real-data answers in the Publisher checklist | The change loop passes; merged on approval |
| 1.6 | Fix briefs: each approved Publisher bug, and each premise that needs a decision change, becomes one tracker row (`L<NN>`) in [`../publisher/tracker.md`](../publisher/tracker.md) | `Needs input` where a policy value must be chosen first |

## Phase 2 · Fix what the mining found

| Step | Do | Check |
|---|---|---|
| 2.1 | Each `L<NN>` row through the remediation runbook's per-issue loop with its golden-miner differences: a red case built from the brief's warehouse, the fix on its own branch, the change loop, the version bump of the first decision-affecting merge, and the fix verified on the same evidence (replay `mining_out/<D>/pub_full/evidence.json` with the fixed code and mine the replay: the brief's mismatch is gone and no new one appears) | A premise's new value is chosen by the owner of the decision before any code; each merge after its summary and approval |
| 2.2 | A demo-fleet parameter change (drift), or any fix that moves the demo, is a Publisher change that the Realizer's simulated worlds read too: rebuild the Realizer's fixtures (`python scripts/build_realizer_fixtures.py --approved-change "<why>"`) and end-to-end goldens (`python scripts/update_realizer_e2e_goldens.py --stage all --approved-change "<why>"`), run the Realizer's decision-equivalence check over its stored scenarios and explain every decision that moved | The Realizer's stored outputs regenerate only with the approval |
| 2.3 | A Realizer defect found on the way starts a new Realizer review (the last one is closed: [`../../archive/realizer/gap-review.md`](../../archive/realizer/gap-review.md)) | Recorded, then fixed the same way |

With no approved fix, phase 3 reuses step 1.1's run instead of collecting again.

## Phase 3 · Publisher flows on the fixed code

| Step | Do | Check |
|---|---|---|
| 3.1 | Publisher, full scope, on master after the fixes (a new date `<D>`): step 1.1's command into `mining_out/<D>/pub_full` | Mine it (step 1.3): the fixed mismatches stay fixed and no new one appears |
| 3.2 | Publisher, auto-stop scope: `publisher --profile <P> --sql-warehouse-id <C> --source-region <R> --warehouse all --scope current:auto-stop --flat-output --output-dir mining_out/<D>/pub_autostop` | Every warehouse with an auto-stop change has a one-step package |
| 3.3 | Cost-profile replays of 3.1's evidence: `publisher --from-evidence mining_out/<D>/pub_full/evidence.json --cost-profile <profile> --flat-output --output-dir mining_out/<D>/pub_<profile>` for `conservative`, `balanced`, `aggressive` | Per warehouse, which cuts each profile ships or holds, and on which limit |
| 3.4 | Choose `<T>`: a Pro or Classic warehouse with steady weekday use, an auto-stop package from 3.2 and an owner who agrees to one auto-stop change for four weeks | Agreed with the owner of `<T>`; note the package's recommended value |

## Phase 4 · Realizer flows

| Step | Do | Check |
|---|---|---|
| 4.1 | Backtest of past auto-stop changes: `realizer --backtest --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir mining_out/<D>/rz` | Per past change: predicted, realized, billed and the cross-check outcome. The Publisher's backtest of the other knobs is not built yet (its checklist, tracker G56) |
| 4.2 | Baseline fleet run over the auto-stop packages: `realizer --publisher-run mining_out/<D>/pub_autostop --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir mining_out/<D>/rz` | Exit 0; every warehouse `NOT_APPLIED`; the state under `mining_out/<D>/rz/state` holds one lineage per package |
| 4.3 | Offline replay of 4.2: `realizer --publisher-run mining_out/<D>/pub_autostop --from-evidence mining_out/<D>/rz/state/realization/measurements --output-dir mining_out/<D>/rz_replay` | The same states as 4.2; a difference is a defect to report |
| 4.4 | The owner of `<T>` sets its auto-stop to the value of `<T>`'s package from 3.2, exactly, in the Databricks UI or API, and leaves every other setting alone for four weeks. Optional: an approvals file naming the change, passed as `--approvals` in 4.5, records the origin as confirmed ([`source-evidence-contract.md`](../../contracts/realizer/source-evidence-contract.md) section 18, origin) | Note the UTC time of the change. Another knob changed in the window is reported as an independent change and stops the billed cross-check from isolating auto-stop |
| 4.5 | Once a week, on the same weekday, with the same `--output-dir`: `realizer --publisher-run mining_out/<D>/pub_autostop --profile <P> --sql-warehouse-id <C> --source-region <R> --output-dir mining_out/<D>/rz [--approvals <file>]` (add `--warehouse <T>` to measure `<T>` alone when the fleet run is slow) | Exit 0. `<T>`: `APPLIED` and not settled until one whole settled week follows the change day; then a measurement each week over the settled weeks since the change (28 days at week 4) |
| 4.6 | Replay the week's evidence as in 4.3, into a new folder | The same states and numbers |

Auto-stop comes first because its counterfactual is a replay with no guard window. A size change's canary guard keeps its claim observable only for 30 days, so it comes after this run (phase 5).

## Checkpoints

| Checkpoint | When | Send back |
|---|---|---|
| A | After phase 1 | `real_data_answers.json`, `pattern_report.md` (aggregates and pattern names), the counts in 1.1's `manifest.json` and its `publisher.log` warnings |
| B | Before step 4.4 | The fixes merged in phase 2 with their summaries; the three cost-profile outcomes; `realization/backtest/` and the run folder of 4.2 from `mining_out/<D>/rz`, and of 4.3 from `mining_out/<D>/rz_replay` (`manifest.json`, `realization_workbook.xlsx`, `realizer.log`, `realization/`); the stdout and exit status of each command |
| C, D | After week 1 and week 4 of step 4.5 | `state/realization/measurements/*.json` (no SQL text, owner or tag), the week's run folder files, stdout and exit status. For `<T>`: predicted (the package's range), realized (the replay), billed (the cross-check outcome and its reason), state, classification and claim status |

Packages, workbooks, manifests and the JSON pattern report name warehouses: send them as agreed (or with the identifiers removed), never `evidence.json`.

The miner grounds the Publisher's scenarios. The Realizer has no miner yet: its real-data evidence is the backtest (4.1) and the live measurements (4.5), whose evidence files replay offline (`--from-evidence`). Turning a real measurement into a Realizer golden case is not built; this run's measurement files are its first input if it is.

## Phase 5 · Review and next changes

1. Review together, per change: predicted against realized against billed, in real-number tables, and every question of both checklists the run answered. A recalibrated value goes through its own approval, a version bump and regenerated goldens.
2. Mine again after week 4 (phase 1 on a new date): `<T>` then shows its new epoch, so the miner checks the Publisher's decision on the changed warehouse, and drift is compared with the first mining.
3. Next changes, one at a time on agreed warehouses, each measured to a stable number before the next: a size cut (claimable only after the 30-day canary guard, so about five weeks), then a minimum-cluster cut. A plan's later steps are not measured once an earlier step is applied ([Realizer known limitations](../../reference/realizer/known-limitations.md)), so apply one step and measure it before the next.
4. The Publisher's backtest of size, floor and maximum-cluster changes (tracker G56) follows from the same evidence.
5. State retention between runs: [`ledger-contract.md`](../../contracts/realizer/ledger-contract.md) section 24.

## Starter prompts

Run them on the machine that can reach the account, from the root of a checkout of master after steps 0.1 and 0.2. The first prompt drives phases 0 to 4.3; the second resumes the weekly measurement.

**How to invoke.** Each command reads the prompt from this page, so the session always gets the committed text. The values shown (`prod-sso`, the warehouse ids, the dates) are examples to replace.

```bash
# The run (phases 0 to 4.3). The session first asks for the profile, collector warehouse and region.
claude --model claude-opus-5-5 --effort high \
  "$(awk '/^\*\*Mine, fix, Publisher flows, Realizer flows:\*\*/{f=1;next} f&&/^```text/{p=1;next} p&&/^```/{exit} p' docs/proposals/shared/live-run.md)"

# The same, with the inputs given up front.
claude --model claude-opus-5-5 --effort high \
  "$(awk '/^\*\*Mine, fix, Publisher flows, Realizer flows:\*\*/{f=1;next} f&&/^```text/{p=1;next} p&&/^```/{exit} p' docs/proposals/shared/live-run.md)

Inputs: profile prod-sso, collector SQL warehouse 4f2a9c1b7e3d5a60, AWS region us-east-1."

# Each week after step 4.4 (N = 1 to 4, on the same weekday): the run's date, the test warehouse, the change time,
# and the same profile, collector warehouse and region as the first run.
claude --model claude-opus-5-5 --effort high \
  "$(awk '/^\*\*Each week/{f=1;next} f&&/^```text/{p=1;next} p&&/^```/{exit} p' docs/proposals/shared/live-run.md | \
     sed -e 's|<date>|2026-10-12|g' -e 's|<test warehouse>|8c1d2e3f4a5b6c7d|g' \
         -e 's|<UTC time>|2026-10-14T15:05Z|g' -e 's|<N>|1|g' \
         -e 's|<profile>|prod-sso|g' -e 's|<collector warehouse>|4f2a9c1b7e3d5a60|g' -e 's|<region>|us-east-1|g')"
```

Or start `claude --model claude-opus-5-5 --effort high` and paste the prompt. The session stops for you at checkpoint A (then the miner's proposals one at a time), after each fix's summary, at step 3.4 and at checkpoint B; answer in chat ("approve", "all recommended" or per number). When it hands off at 70 to 80 percent context, continue in the session it names. Send back only what each checkpoint lists.

**Mine, fix, Publisher flows, Realizer flows:**

```text
Run the live run of the Publisher and the Realizer on this account, end to end: mine the golden
scenarios, fix what the mining finds, run the Publisher's flows on the fixed code, then the Realizer's.

Read first: CLAUDE.md; docs/proposals/shared/live-run.md (the sequence you follow, phase by phase, with
each step's "Check" column); .claude/skills/databricks-golden-miner/SKILL.md (phase 1);
docs/proposals/publisher/remediation-runbook.md, its per-issue loop and "Fixing a golden-miner finding"
(phase 2); and the two checklists, docs/proposals/publisher/real-data-run.md and
docs/proposals/realizer/real-data-run.md (the questions the run answers).

Ask me once, up front: the OAuth CLI profile, the collector SQL warehouse and the AWS region. Ask for the
test warehouse only at step 3.4, with a recommendation from the auto-stop packages.

Rules:
- Every command against the account is read-only; only a warehouse owner changes anything, by hand, at
  step 4.4. Refuse a profile with a static token.
- Every output goes under mining_out/<date>/. Never put evidence.json, a token or a CLI config into chat,
  git or a message.
- On a failed step stop and show me the command, its exit status and the error.
- Gates in chat: the miner's proposals one at a time with their real numbers; every decision (a policy
  value, a premise's new value, a behaviour a user sees) as a numbered decision with a recommendation and
  a worked example from the live evidence, ending with a short numbered list of the recommended options
  for me to answer ("all recommended" or per number).
- Fixes: one branch per fix (or tightly coupled group), red test first, the CLAUDE.md change loop,
  python scripts/compare_with_ref.py --ref master for every Publisher change, the Realizer's fixture and
  goldens rebuild with its decision-equivalence check when the demo moves, /code-review low on the branch
  before each merge, the full suite again after review fixes, then a summary per fix (problem, fix,
  worked example with before/after numbers, tests, commits) and my approval before merging to master. No
  push. Update the trackers and the docs with each fix.
- Use parallel sub-agents for independent fixes, each in its own worktree created from your branch.
- At 70-80 percent context, hand off to a fresh session with a handoff prompt that carries the state.

Stop and wait for me at checkpoint A (after phase 1), after each fix's summary, at step 3.4 (choosing
the test warehouse) and at checkpoint B (before the owner applies the change). At checkpoint B report,
per phase, what ran and what it showed in real-number tables, list the files to send back, and give the
first answers to the checklist questions the data settles so far (restart billing and the 0.75 factor
from the Realizer's backtest).
```

**Each week (step 4.5, checkpoints C and D):**

```text
Resume the live run: read CLAUDE.md and docs/proposals/shared/live-run.md. The run's folder is
mining_out/<date>/; the owner applied the change on <test warehouse> at <UTC time> (step 4.4). Run steps
4.5 and 4.6 for week <N> with profile <profile>, collector SQL warehouse <collector warehouse> and AWS
region <region> (the first run's), the same --output-dir (mining_out/<date>/rz) and nothing else changed.
Earlier weeks are in mining_out/<date>/rz/state/ and the earlier run folders under mining_out/<date>/rz. Report for the test warehouse: state,
classification, claim status, predicted (the package's range) against realized (the replay) against
billed (cross-check outcome and reason), in a table next to the earlier weeks, and whether the replay
matched. At week 1 and week 4 list the checkpoint files to send back; after week 4 go on to phase 5
(review, second mining, next change) and propose before doing.
```
