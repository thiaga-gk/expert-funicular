# Remediation runbook: gap review of publisher 2.8.0/2.9.0

Purpose: resolve every tracker finding, one at a time, until every **in-scope** golden scenario in `docs/proposals/golden-scenarios.md` is **covered** and **pass**, except scenarios blocked only by items marked *Needs input*.

## Sources of truth

| What | Where |
|---|---|
| Findings, design, decisions D1–D14 | `docs/proposals/gap-review-2.8.0.md` |
| Golden scenarios (one table; finding IDs only in the Tracker column) | `docs/proposals/golden-scenarios.md` |
| Live status, priority order | Tracker artifact https://claude.ai/artifact/TMmDwx8SBk2ZuzCQbrzr1w, collection `findings`, doc id = finding id |
| Earlier proposals M1–M4, M9 | `docs/proposals/audit-m-group-b.md`, `docs/proposals/m9-contract-design.md` |
| Execution rules | `CLAUDE.md` (change loop, contract rules, security) |

Tracker fields: `rank` (sort key; may be fractional), `batch`, `status` (Open, In progress, Needs input, Done, Deferred), `decision`, `note`, `updated`. Read with `ArtifactData` `list`/`get`; write with `update` and `if_version` set to the version you last read. Never delete rows.

## Order of work

Take the lowest-rank tracker row whose status is **Open**. Items whose notes say they ship together (for example G21 with S2 and M2; G9 with S4; G6 with M4; K1 with G32, G8, G26) form one issue: one branch, one review, one merge. Skip *Needs input* and *Deferred* rows.

## Per-issue loop (red → green → review → merge)

0. **Once, before the first issue:** in the main checkout on `master`, commit the untracked `docs/proposals/gap-review-2.8.0.md`, `docs/proposals/golden-scenarios.md` and this runbook in one commit. Create the worktree: `git worktree add .worktrees/gap-fixes -b fix/<first-id> master`, then create a venv inside it and `pip install -r requirements.txt`.
1. **Start:** in the worktree, `git switch -c fix/<id> master` (after the first issue). Set the tracker row to *In progress*.
2. **Red:** write the failing tests first.
   - Unit tests for the defect in `tests/test_publisher.py` (or the contract/workbook test file it belongs to).
   - For every golden scenario row whose Tracker cell lists this finding: a fixture `tests/golden/<cXX>-<slug>.json` (normalized evidence plus an `expected` block, same shape as the existing fixtures) and a test asserting the row's Expected behavior. Reuse an existing fixture when it already represents the scenario.
   - Run the tests and confirm they fail **for the reason the finding describes**. Paste the failing assertion into the issue summary.
3. **Green:** make the smallest layer-local change that passes (analyzer observes, optimizer decides, candidate estimator prices one change, bundle estimator prices scenarios). Unknown stays null, never zero. Keep determinism.
4. **Refactor**, then run the full CLAUDE.md change loop:
   1. `python -m compileall -q publisher.py contracts reporters tests`
   2. `python -m unittest discover -s tests -v`
   3. regenerate the 15-warehouse demo (`python publisher.py --demo --output-dir examples/demo_output`)
   4. validate every package (semantic + JSON Schema)
   5. `python tools/validate_workbook.py examples/demo_output`
   6. stale version/legacy reference scan before any release step
5. **Versions:** any decision-affecting change bumps the affected component versions (`POLICY_VERSION`, `ANALYZER_VERSION`, `OPTIMIZER_VERSION`, `ESTIMATOR_VERSION`, `BUNDLE_MODEL_VERSION`, `MODEL_VERSION`, `REVIEWER_VERSION`, `DECISION_VERSION`). The first decision-affecting merge of this cycle moves `PUBLISHER_VERSION` to the next minor (2.10.0) and the affected components to `v2.10.0`; later merges in the cycle update any component newly affected. Contract versions change only through the contract gate below. Regenerate goldens and examples in the same commit.
6. **Golden table:** for each scenario row this issue touches, set **Code coverage** to *covered* once code handles it, and **Status** to *pass* only when its golden test passes **and** every finding in its Tracker cell is *Done* (this one included after merge). Never add finding IDs to any column but Tracker.
7. **Review:** run `/code-review low` on the branch. If it returns nothing suspiciously fast, run an independent review subagent (Opus) on `git diff master...fix/<id>` instead. Fix confirmed findings and repeat steps 4–7.
8. **Merge:** from the main checkout, `git merge --no-ff fix/<id>` into `master` with a message naming the finding IDs; delete the branch. Do not push.
9. **Close:** set the tracker row(s) to *Done* with a one-line note (what changed, commit hash). Give the user the per-issue summary (below). Then take the next issue.

### Per-issue summary (shown at every merge)

Inside an `AskUserQuestion` preview, because chat prose may not be visible: the problem in one sentence, the fix, a worked example with before/after numbers from the real code, test results (counts), the golden rows that moved to pass, and the commit hash. Merge without waiting for a reply **unless** the issue hits a stop condition.

## Stop conditions (ask, do not merge)

Set the row to *Needs input*, record why in `note`, and continue with the next Open issue. Batch the questions to the user. An issue needs input when it:

- changes the Reporter JSON contract, the schema or the Workbook contract (G1 exclusion row, G7/D2 executive rows, G31 per-step savings): use the `databricks-contract-designer` skill and get sign-off on the diff **before** contract code;
- changes `CLAUDE.md`, the PRD, HLD, LLD or context docs in scope or policy (D14/G42 channel retirement, G33 prior-epoch veto exception, D1/D13 FR-PLAN-001 wording): show the exact text and get approval;
- needs a policy value not yet decided (G38 tag profile, G30 materiality floor values, K2/K3 percentiles if real data disagrees);
- needs real data (G5 SKU sample; any assumption marked UNVERIFIED that the fix depends on).

## Exit

Stop when no tracker row is *Open* or *In progress*. Then report: rows *Done*, rows *Needs input* with their questions, rows *Deferred*, and the golden table tally (in scope: covered/pass counts; every in-scope row not passing must be blocked only by a *Needs input* item). Remove the worktree only after the user confirms.

## Model and delegation

- Main session: Opus. Most items change decision semantics, economics or determinism, where subtle errors have been found repeatedly.
- Delegate to Sonnet subagents: building golden fixtures from a written spec, mechanical doc updates, running the validation loop and summarizing output, documentation lookups.
- Independent review of decision, estimator and contract changes: a fresh Opus subagent that did not write the code.
