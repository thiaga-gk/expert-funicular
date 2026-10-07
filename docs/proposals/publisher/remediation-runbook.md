# Remediation runbook: tracker findings

Purpose: resolve every tracker finding, one at a time, until every **in-scope** golden scenario in `docs/proposals/publisher/golden-scenarios.md` is **covered** and **pass**, except scenarios blocked only by items marked *Needs input*. The loop was written for the gap review of publisher 2.8.0/2.9.0 and applies unchanged to the `L<NN>` findings from a golden-miner run, with the differences in [Fixing a golden-miner finding](#fixing-a-golden-miner-finding).

## Sources of truth

| What | Where |
|---|---|
| Findings, design, decisions D1–D14 | `docs/proposals/publisher/gap-review-2.8.0.md` |
| Golden scenarios (one table; finding IDs only in the Tracker column) | `docs/proposals/publisher/golden-scenarios.md` |
| Live status, priority order | Tracker artifact https://claude.ai/artifact/TMmDwx8SBk2ZuzCQbrzr1w, collection `findings`, doc id = finding id |
| Repo copy of the tracker (for environments without claude.ai) | `docs/proposals/publisher/tracker.md`, regenerated from the artifact at every batch merge |
| Earlier proposals M1–M4, M9 | `docs/archive/publisher/audit-m-group-b.md`, `docs/archive/publisher/m9-contract-design.md` |
| Execution rules | `CLAUDE.md` (change loop, contract rules, security) |

Tracker fields: `rank` (sort key; may be fractional), `batch`, `status` (Open, In progress, Needs input, Done, Deferred), `decision`, `note`, `updated`. Read with `ArtifactData` `list`/`get`; write with `update` and `if_version` set to the version you last read. Never delete rows.

## Order of work

Take the lowest-rank tracker row whose status is **Open**. Items whose notes say they ship together (for example G21 with S2 and M2; G9 with S4; G6 with M4; K1 with G32, G8, G26) form one issue: one branch, one review, one merge. Skip *Needs input* and *Deferred* rows.

## Per-issue loop (red → green → review → merge)

0. **Once, before the first issue:** create the worktree from a clean `master`: `git worktree add .worktrees/gap-fixes -b fix/<first-id> master`, then create a venv inside it and `pip install -r requirements.txt`.
1. **Start:** in the worktree, `git switch -c fix/<id> master` (after the first issue). Set the tracker row to *In progress*.
2. **Red:** write the failing tests first.
   - Unit tests for the defect in the capability test folder it belongs to (`tests/unit/<capability>/`, or `tests/unit/contracts/`, `tests/unit/reporter/` for the contract/workbook test files).
   - For every golden scenario row whose Tracker cell lists this finding: a case folder `tests/golden/cases/<cXX>-<slug>/` (`case.yaml` with the `expect` block and `source/` normalized evidence, same shape as the existing cases; `expected/` comes from `scripts/update_goldens.py`) and a test asserting the row's Expected behavior. Reuse an existing fixture when it already represents the scenario.
   - Run the tests and confirm they fail **for the reason the finding describes**. Paste the failing assertion into the issue summary.
3. **Green:** make the smallest layer-local change that passes (analyzer observes, optimizer decides, candidate estimator prices one change, bundle estimator prices scenarios). Unknown stays null, never zero. Keep determinism.
4. **Refactor**, then run the full CLAUDE.md change loop:
   1. `python -m compileall -q src tests scripts examples`
   2. `python -m unittest discover -s tests -v`
   3. regenerate the demo fleet (one simulated warehouse per golden scenario: `python -m publisher --demo --output-dir ./demo_out`)
   4. validate every package (semantic + JSON Schema)
   5. `python scripts/validate_workbook.py ./demo_out`
   6. stale version/legacy reference scan before any release step
5. **Versions:** any decision-affecting change bumps the affected component versions (`POLICY_VERSION`, `ANALYZER_VERSION`, `OPTIMIZER_VERSION`, `ESTIMATOR_VERSION`, `BUNDLE_MODEL_VERSION`, `MODEL_VERSION`, `REVIEWER_VERSION`, `DECISION_VERSION`). The first decision-affecting merge of this cycle moves `PUBLISHER_VERSION` to its next minor and the affected components to that version; later merges in the cycle update any component newly affected. Contract versions change only through the contract gate below. Regenerate goldens and the contract example in the same commit.
6. **Golden table:** for each scenario row this issue touches, set **Code coverage** to *covered* once code handles it, and **Status** to *pass* only when its golden test passes **and** every finding in its Tracker cell is *Done* (this one included after merge). Never add finding IDs to any column but Tracker.
7. **Review:** run `/code-review low` on the branch. If it returns nothing suspiciously fast, run an independent review subagent (Opus) on `git diff master...fix/<id>` instead. Fix confirmed findings and repeat steps 4–7.
8. **Merge:** from the main checkout, `git merge --no-ff fix/<id>` into `master` with a message naming the finding IDs; delete the branch. Do not push.
9. **Close:** set the tracker row(s) to *Done* with a one-line note (what changed, commit hash). Give the user the per-issue summary (below). Then take the next issue.

### Per-issue summary (shown at every merge)

In chat prose, with only a short approval question in `AskUserQuestion` (dialog previews do not render reliably): the problem in one sentence, the fix, a worked example with before/after numbers from the real code, test results (counts), the golden rows that moved to pass, and the commit hash. Merge without waiting for a reply **unless** the issue hits a stop condition. In a live run ([`../shared/live-run.md`](../shared/live-run.md)) every merge waits for approval.

## Fixing a golden-miner finding

The `databricks-golden-miner` skill (`.claude/skills/databricks-golden-miner/SKILL.md`) writes one tracker row per approved bug or premise item, ID `L<NN>`. The row's note is the fix brief: kind (bug or premise), rows, live evidence, the warehouse and run folder to build the red case from, and the setting and components involved. Take those rows through the per-issue loop above, with these differences:

- **Red:** build the case from the brief's warehouse with `.claude/skills/databricks-golden-miner/scripts/make_case.py --run-dir <run folder> --warehouse <ws>/<wh> --row <CNN> --slug <name> --description "..."`. Cut the draft `expect` block down to the row's **expected** behaviour, not today's decision, then confirm it fails for the reason in the brief. The case commits the warehouse's evidence as collected, including real identities and costs. The run folder (`mining_out/`) is local and gitignored. Without it, the brief cannot be reproduced: mark the row *Needs input* and ask for a new mining run.
- **Premise items:** choosing the setting's new value is a stop condition (a policy value not yet decided). Present the live numbers and candidate values, and change nothing until the user picks one. With the change, update the setting's documentation row (`docs/reference/publisher/thresholds.md` or `docs/reference/publisher/product-constants.md`), the row text and the premise's `assumes` value in `.claude/skills/databricks-golden-miner/references/signature-catalog.yaml` (`tests/unit/skills/test_golden_miner.py` checks them against the code).
- **Versions:** as in step 5. The first decision-affecting merge of a mining cycle moves `PUBLISHER_VERSION` to the next minor, and existing `expected/` outputs change only through `scripts/update_goldens.py --approved-change` in the same commit.
- **Verify on the same evidence:** replay the brief's run with the fixed code (`python -m publisher --from-evidence <run folder>/evidence.json --output-dir <dir>`), then mine the replay (`mine_patterns.py --run-dir <dir>`). The brief's mismatch or premise contradiction must be gone, and no new ones may appear. Record both counts in the per-issue summary.
- **Close:** the row's Tracker cell keeps its `L<NN>` ID. The row's Status moves to *pass* under step 6.

## Stop conditions (ask, do not merge)

Set the row to *Needs input*, record why in `note`, and continue with the next Open issue. Batch the questions to the user. An issue needs input when it:

- changes the Reporter JSON contract, the schema or the Workbook contract (G1 exclusion row, G7/D2 executive rows, G31 per-step savings): use the `databricks-contract-designer` skill and get sign-off on the diff **before** contract code;
- changes `CLAUDE.md`, the PRD, HLD, LLD or context docs in scope or policy (D14/G42 channel retirement, G33 prior-epoch veto exception, D1/D13 FR-PLAN-001 wording): show the exact text and get approval;
- needs a policy value not yet decided (G38 tag profile, G30 materiality floor values, K2/K3 percentiles if real data disagrees);
- needs real data (G5 SKU sample; any assumption marked UNVERIFIED that the fix depends on).

## Exit

Stop when no tracker row is *Open* or *In progress* (for a mining cycle: no `L<NN>` row from that run). Then report: rows *Done*, rows *Needs input* with their questions, rows *Deferred*, and the golden table tally (in scope: covered/pass counts; every in-scope row not passing must be blocked only by a *Needs input* item). Remove the worktree only after the user confirms.

## Model and delegation

- Main session: Opus. Most items change decision semantics, economics or determinism, where subtle errors have been found repeatedly.
- Delegate to Sonnet subagents: building golden fixtures from a written spec, mechanical doc updates, running the validation loop and summarizing output, documentation lookups.
- Independent review of decision, estimator and contract changes: a fresh Opus subagent that did not write the code.
