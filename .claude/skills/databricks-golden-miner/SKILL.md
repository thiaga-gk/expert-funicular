---
name: databricks-golden-miner
description: Mine a live Databricks workspace's system tables (through the publisher's own read-only live run and the Databricks CLI) for real warehouse patterns, and turn them into gated proposals that refresh the golden scenarios. Coverage of the registry, gaps no row covers, behaviour mismatches against expected behaviour, demo-fleet parameter drift, and the pending real-data-run answers. Use this skill whenever the user wants to refresh, extend, ground or sanity-check golden scenarios, golden cases or the demo fleet against real data, mine system.query.history/billing/warehouse events for patterns, do the "real-data run", find which golden rows occur in production, or turn a live warehouse into a regression case, even if they only say "check the goldens against prod" or "what patterns do our warehouses show".
---

# Databricks Golden Miner

Turn a live, read-only publisher run into evidence about the golden scenario set: which registry rows real warehouses
exhibit, which real patterns no row covers, where the publisher's decision contradicts a row's expected behaviour, where
live evidence contradicts an assumption a row rests on, and how far the simulated demo fleet sits from reality. You then **propose** additions and changes, and the user approves each
one before anything is written. Golden baselines are regression oracles. A silent refresh would bless whatever the code does today,
so the skill never rewrites an existing `expected/` folder and never commits without an explicit approval.

Frame every proposal with the sibling orchestrator's `references/optimization-thinking-framework.md`. A signature is a
causal hypothesis (F2: observation, then mechanism), an expected behaviour is a constraint or counterfactual claim (F3/F4),
and the whole loop is F6 validation and learning: real data calibrates the simulated scenarios.

Read `references/mining-method.md` before interpreting a report. It defines the features, the catalog semantics and
how to read gaps, mismatches and drift. The row signatures live in `references/signature-catalog.yaml`.

## Ground rules

- **Read-only.** Only SELECT statements and GET-style CLI calls. No `apply`, `edit`, `start` or `stop` calls, and no mutation API.
  The table probes and queries start a stopped execution warehouse; tell the user before step 0 runs them.
- **The Databricks CLI is a hard prerequisite.** Nothing live runs until `scripts/preflight.py` passes.
- **OAuth profiles only.** A profile with a static PAT is refused, the same rule as the publisher. Never print, log or
  paste tokens.
- **Raw output stays out of git.** Write runs to `mining_out/`, which is gitignored.
  `evidence.json` holds owner emails, tags and real identities.
- **Live golden cases commit the evidence exactly as collected.** This was the repo owner's decision: no anonymizer. Say so at the gate
  for each proposed case so the user sees which warehouse's identity and costs will enter git.
- **One home per fact.** The collector SQL stays in `src/publisher/analyzer/collector/queries/`, the real-data queries in
  `docs/proposals/real-data-run.md` and the scenarios in `docs/proposals/golden-scenarios.md`. The scripts read
  those; they never carry copies.

## Inputs to ask for (once, up front)

1. The OAuth CLI profile (`--profile`).
2. The SQL warehouse that runs the collector SQL (`--sql-warehouse-id`).
3. The AWS region (`--source-region`).
4. Optionally, a run scope (`--warehouse` / `--scope`). The default is all warehouses.

## Workflow

### 0. Preflight (fails closed)
```bash
python .claude/skills/databricks-golden-miner/scripts/preflight.py --profile <P> --warehouse-id <W>
```
The script checks, in order: the CLI is installed and recent enough; the profile is OAuth (no `token`); `databricks auth token`
works; `current-user me` answers; `warehouses get <W>` works; and a `SELECT 1 ... LIMIT 1` on each system table the collector reads
(`system.compute.warehouses`, `system.compute.warehouse_events`, `system.query.history`, `system.billing.usage`,
`system.billing.list_prices`). On FAIL, relay the printed `fix:` line to the user and stop. Don't work around a failed step.

### 1. Collect through the publisher (single source of the SQL semantics)
```bash
python -m publisher --profile <P> --sql-warehouse-id <W> --source-region <R> --warehouse all \
  --output-dir mining_out/<YYYY-MM-DD> --flat-output --no-workbook
```
Keep `evidence.json`, `packages/`, `manifest.json` and `publisher.log`. Report `unseen_billed_warehouse_count` from
`manifest.json`. That count answers G4b on its own: billed warehouses that `system.compute.warehouses` does not show.

### 2. Real-data aggregates (G4b, G5, S4)
```bash
python .claude/skills/databricks-golden-miner/scripts/real_data_queries.py --profile <P> --warehouse-id <W> \
  --out mining_out/<date>/real_data_answers.json
```
These are aggregate-only queries. The script reads them from `docs/proposals/real-data-run.md`; `--list` shows them without running.

### 3. Mine
```bash
python .claude/skills/databricks-golden-miner/scripts/mine_patterns.py --run-dir mining_out/<date> \
  --real-data mining_out/<date>/real_data_answers.json
```
This writes `pattern_report.json` and `.md` under the run folder: coverage, gaps, behaviour mismatches, premise
contradictions, drift and real-data answers. The output is deterministic: the same run gives identical reports. Read the
markdown first, then use the JSON for exact feature values.

### 4. Build proposals
Turn the report into a short, ranked list of proposals. A report finding alone is not yet a proposal. Each proposal is one of:

| Kind | From | Proposed change |
|---|---|---|
| **New row** | a gap group: real warehouses no primary row explains | Registry row text (Scenario, Signature, Expected behaviour, In scope, Coverage, Status) + catalog signature + a live case |
| **Live case for an existing row** | a matched row with no case folder, or a row marked `fail` | A live case pinning the behaviour the row expects |
| **Behaviour mismatch** | `mismatches` | Triage into one of three causes (below) and say which one it is and why |
| **Row premise contradicted** | `premise_contradictions`, a mismatch triaged as premise, or a real-data answer that refutes an assumption a row cites | The current row text, the premise and the setting it rests on (`assumes`), the live evidence against it, and the suggested new row text. When the publisher's behaviour or a setting must change too, a separate decision-change item (see step 6) |
| **Drift** | `drift` with `demo_outside_live_range` | A `DEMO_FLEET` parameter change in `src/publisher/analyzer/collector/demo.py` with before/after values; expected behaviour unchanged |
| **Real-data answer** | `real_data` | The text that closes G4b/G5/S4 in `docs/proposals/real-data-run.md` and the tracker |

**Mismatch triage.** Read the package `rationale` and `blockers`, the row and the warehouse's features, then pick one of these:
1. **Publisher bug.** The row and the signature are right, but the decision is wrong. Propose a live case asserting the expected behaviour.
2. **Signature false positive.** The warehouse isn't that pattern, for example a canary rollback that also looks over-sized. Propose a tighter signature.
3. **Row premise contradicted.** The warehouse is the pattern and the publisher follows the row, but live evidence shows the row's
   expected behaviour rests on something untrue for real warehouses. Examples: starts take 12 minutes, not 4; billed size rates
   are off the published ratio; clusters queue at 4 concurrent queries, not 10. A mismatch flagged
   `premise contradicted: P-...` points here first.

Never relax a row's expected behaviour just to make a mismatch disappear. Changing what a row expects needs evidence that the
premise is wrong across warehouses, not on a single outlier.

Rank them: `fail`-row mismatches first, then premise contradictions by affected warehouses and rows, then gaps by
warehouse count and annual cost, then drift. Drop anything that
rests on one ambiguous warehouse unless the user asks for it.

### 5. Gate: one proposal at a time, explained in chat
For each proposal, write in chat:
- a plain-language summary;
- a small table of the real numbers behind it (warehouse, type/size/clusters/auto-stop, the deciding features, today's
  decision and the expected decision);
- for a case, the warehouse identity and annual cost that will enter git;
- the exact text or diff that would be committed.

Then ask approve / reject / adjust with a short question whose options stand on their own. Never batch approvals, and never
treat silence as approval.

### 6. Apply the approved proposals (one branch per mining run)
1. Branch first: `git checkout -b golden-mining-<date>`.
2. **Case:** draft it with
   ```bash
   python .claude/skills/databricks-golden-miner/scripts/make_case.py --run-dir mining_out/<date> \
     --warehouse <ws>/<wh> --row <C59> --slug <kebab-name> --description "<pattern; expected behaviour>"
   ```
   This writes `tests/golden/cases/<row>-<slug>/` with the raw evidence and a draft `expect` block that pins today's decision
   per knob. It refuses to overwrite an existing folder. Cut the draft down to the assertions that state the row's expected
   behaviour, and keep the ones the user approved. Only cases that pass today go on the mining branch. A case for a bug or
   premise item would fail, so it is not added here: it becomes the red test of that item's fix branch (step 5).
   Then generate the baseline:
   `python scripts/update_goldens.py --case <name> --approved-change "<the approval in one line>"`.
3. **New row:** add it to `docs/proposals/golden-scenarios.md` and its signature to
   `references/signature-catalog.yaml`. A row that also gets a `DEMO_FLEET` warehouse needs
   `python scripts/update_goldens.py --demo-only --approved-change "<why>"`.
4. **Row premise contradicted:** this has two parts, approved separately.
   - **Text-only.** Edit the row's Signature or Expected behaviour in `docs/proposals/golden-scenarios.md`, and its `expect`
     and `premises` in the catalog. Do this on the mining branch, as long as no publisher decision changes.
   - **Decision change.** The behaviour the row expects differs from what the code does, or the setting it rests on
     (`assumes`, a `Thresholds` field or product constant) must change. It becomes a fix brief (step 5). Never fold it into
     the mining branch, and never rewrite existing `expected/` outputs to match the new premise.
   Until the decision change lands, keep the row's Status honest (`fail` if the code now contradicts the corrected row).
5. **Fix briefs** (hand-off to the fixing loop). Each approved publisher bug, and each premise item that needs a decision
   change, becomes one tracker row. Add it to the live tracker when it is reachable, and always to
   `docs/proposals/tracker.md`. The row has:
   - ID `L<NN>`, the next free number;
   - Title, Band (P1 for a bug on an in-scope row, P2 for a premise), Batch `mining-<date>`;
   - Status `Open`, or `Needs input` when a policy value must be chosen first, such as the new value of the setting a
     premise rests on;
   - Note, written as the brief:
     - kind (bug or premise) and rows;
     - the live evidence: warehouses, the deciding numbers, today's decision against the expected one;
     - the warehouse to build the red case from (`<ws>/<wh>`) and its run folder;
     - the setting it rests on (`assumes`) and the components likely to need a version bump.

   Put the `L<NN>` ID in each affected row's Tracker cell in `docs/proposals/golden-scenarios.md`. Fixing follows
   "Fixing a golden-miner finding" in `docs/proposals/remediation-runbook.md`. That work is not part of this skill. Out-of-scope rows never get fix briefs.
6. **Real-data answers:** update `docs/proposals/real-data-run.md` and the tracker, then regenerate `docs/proposals/tracker.md`.
7. Run the change loop: `python -m compileall -q src tests scripts examples`, then
   `python -m unittest discover -s tests -v`, the demo with `python scripts/validate_workbook.py`, and
   `python scripts/validate_skill_suite.py`. Report failures with their output. Don't hide them.
8. Commit only when the user asks.

## When the catalog itself changes

The catalog describes what a pattern looks like, not publisher policy. Policy numbers stay in
`src/publisher/governor/thresholds.py`. After you edit a signature, run
`python -m unittest tests.unit.skills.test_golden_miner -v`. It checks that every demo-fleet warehouse still matches its own
row, that the report is deterministic, that a mutated warehouse raises a mismatch, and that each premise's `assumes`
value still equals the publisher setting. When that last test fails because a setting changed, re-read the premise,
update its value and tolerance band, and say so in the change.

## Output to the user at the end

Give a short summary:
- what was mined (warehouse count, date);
- coverage highlights;
- proposals, split into approved, rejected and deferred;
- files changed;
- test results.

Point to `mining_out/<date>/pattern_report.md` for the full report.
