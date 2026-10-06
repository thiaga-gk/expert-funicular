# Realizer gap review

Review of the Realizer (`src/realizer/`) at master 5bcae87 for gaps, correctness and completeness, with the goal of running the Publisher and the Realizer on real system-table data. The question that frames it: after `publisher --scope current:auto-stop` recommends an auto-stop change and the owner applies it, can the Realizer measure the realized savings of that change? Today it cannot.

Method: five independent reviews in parallel (package contract, live collector and counterfactual, correctness with probe scripts, golden scenarios, operations), each finding checked against the code before it entered this list. Findings carry IDs `R<NN>`; decisions `RD<NN>`. Live status is in the tracker; the repo copy is [`tracker.md`](tracker.md). The process is the Publisher's per-issue loop ([`../publisher/remediation-runbook.md`](../publisher/remediation-runbook.md)) applied per batch, in the worktree `.worktrees/realizer-gaps`.

## 1. Where the Realizer stands

The Realizer is a correct-looking shell around numbers it is given. A run takes a PKG and one "observation" mapping, and every number that matters is in the observation: the counterfactual old cost, the new cost, the days, the query count, the p95 change, the reliability verdict, the origin. Nothing measures them.

| Piece a real run needs | Today |
|---|---|
| Read the PKG the Publisher writes (contract 3.1.0) | Rejected: only contract 1.0.0 is accepted |
| Detect that the change was applied, and when | Caller states the observed configuration and times |
| Cost after the change (billed DBU, modeled AWS) | Caller supplies `observed_new_cost` |
| What the old setting would have cost on the post-change workload (M08) | Caller supplies `counterfactual_old_cost`; no model exists |
| Performance and reliability after the change | Caller supplies a p95 percentage and booleans |
| Several packages for one warehouse over weeks | Each package is a separate lineage; both can claim the same savings |
| A daily run over a Publisher run folder | One `--package` per process; the ledger lives under `--output-dir` |
| Something a person reads | No realization workbook; the Publisher workbook shows NOT_APPLIED for every row |

Baseline: 1155 tests pass (3 skipped); the 40 golden rows GR-001..040 pass, all on one frozen 1.0.0 package. With only the version check bypassed in a probe, the engine runs on all 41 demo packages without an error, but the demo observation hard-codes 10,000 / 9,000, so every warehouse "realizes" 1,000.00 and the ratio lands anywhere from 0.13 to 21.7. Several results are also wrong in ways nothing flags (R08, R09, R10, R15).

### The auto-stop example, end to end, once the batches land

The package figures are the demo fleet's `wh-demo-001` auto-stop step (low and high approximate); the post-change numbers are illustrative.


1. `publisher --scope current:auto-stop` writes `PKG-A` for warehouse W: auto-stop 60 to 20, step savings base 93,006/yr (low 62,004, high 124,008; the base applies the 0.75 realization track to the idle a lower value removes).
2. The owner sets auto-stop 20 on 2026-10-08 14:32. `system.compute.warehouses` shows a new configuration epoch from that time.
3. `realizer --publisher-run <run folder> --state-dir <dir>` (daily) reads `PKG-A`, finds the epoch whose auto-stop equals the step's target, and records the application (EXACT, effective time from the epoch start).
4. From the first full settled UTC day after the change, it collects the post-change epoch's evidence with the Publisher's read-only collector, cut at the billing-settled day.
5. Counterfactual: the actual post-change idle gaps replayed with the old value 60 (the Publisher's running-idle replay, one more target), priced at the window's billed DBU per cluster-hour; realized = counterfactual - billed. Example: 28 settled days, replay adds 1,640 idle cluster-minutes and 12 restarts avoided; realized 6,734 for the window, 87,780/yr annualized.
6. Classification against the package's published range (RD15): 87,780 is above the low 62,004, so REALIZED, ratio 0.94 against the base.
7. The RPKG, the ledger and a realization workbook row show it; the next weekly Publisher package for W (its `observed_change` names the auto-stop change) is linked as the successor, and only one lineage owns the savings.

## 2. Findings by batch

Band: P0 blocks a real-data run; P1 wrong results; P2 completeness; P3 hygiene. Batches are in execution order.

### Batch 1 · Package input and fail-closed observation

| ID | Finding | Band | Fix |
|---|---|---|---|
| R01 | Only PKG contract 1.0.0 is accepted; every package the Publisher writes (3.1.0) is rejected | P0 | One package reader producing a frozen `PackageView`; accept 3.1.0 (RD02); schema-validate and recompute the PKG id and full hash on load; require `realization` to be the empty NOT_APPLIED placeholder and `carried_forward_plan` null; record the real `publisher_contract_version`. Retire 1.0.0 (RD01). |
| R02 | The observation has no contract: about 30 optional keys read through `.get`; missing quality inputs default to passing (`correctness_pass`, price coverage, attributable spend, currency consistency, correction chain, `overlap_count`), `bool("false")` is true, and any unknown counterfactual method string is claim-eligible | P0 | Versioned observation format with a JSON Schema, validated on load and on write (the evidence-envelope pattern): required keys, strict types, enumerated `counterfactual_method` (allowlist), absent gate input = gate not passed. RD05. |
| R03 | Observation identity and timeline never checked: an observation for another warehouse, or a change applied two months before the package existed, is claimable | P1 | Observation carries `workspace_id`/`warehouse_id`, mismatch fails; `applied_at` before the package's `generated_at` is an independent change (RD03); `observed_at` before `applied_at` fails. |
| R04 | Currency never compared with the package: a EUR observation against a USD package is claimable; a missing currency becomes USD | P0 | Currency must equal the package's; mismatch or missing = `CURRENCY_MISMATCH` hard disqualifier. |
| R05 | Spelling differs from the Publisher: `X-LARGE`, `2X-SMALL`, `" MEDIUM "` are UNRELATED (false drift too); auto-stop 0 ("never stop", a cost increase) counts as the most aggressive cut and is claimed | P1 | Use the Publisher's normalizer for package and observed values, in matching and in the hash; auto-stop 0 is +infinity in the direction rule (a cut target observed at 0 is CONTRARY). Covers known limitation 9. |
| R06 | Unknown `applied_scenario_id` is accepted as a scenario-scope application of a scenario that does not exist; a dangling `recommended_plan.scenario_id` silently gives NOT_APPLIED | P1 | Fail closed on both. |
| R07 | Unsupported warehouse types pass through (`SERVERLESS_V2` realizes) | P2 | Gate both sides on the Publisher's `SUPPORTED_WAREHOUSE_TYPES`. |
| R08 | An UNRELATED step is recorded as an application and becomes the "previous application" for drift; CONTRARY/UNRELATED surface as `APPLIED` | P1 | UNRELATED = independent change, never an application; CONTRARY maps to a non-applied state with regeneration. Covers known limitation 8. |
| R09 | Errors are tracebacks with exit 1; bad inputs (`--replay-through`) are detected after events were recorded | P1 | `RealizerError` family, CLI exit 2 with a one-line message, inputs validated before the first ledger append. Covers known limitations 15 and 17. |
| R10 | Outputs are not schema-validated on write; `PRODUCT_VERSION` is the stale `2.8.0` in every provenance; no `--version` | P2 | Validate RPKG, RSNAP and ledger entries (Draft 2020-12) before write; tighten the nested schemas; RD04 for the version; `--version` on both CLIs. Covers known limitation 19. |
| R11 | Policy file not validated (unknown keys ignored, negative ratios accepted) | P3 | Reject unknown keys and out-of-range values. |
| R12 | Log time is local but labeled `Z`; no run start/finish lines | P3 | UTC converter (as the Publisher), run events by ID only. Covers known limitation 18. |
| R13 | A failed or blocked validation still publishes an annualized run-rate and a ratio | P2 | Null both unless the state is REALIZED, UNDER_REALIZED or MONITORING. |

### Batch 2 · Plan semantics for 3.x packages (scope, profile, step roles, denominators)

| ID | Finding | Band | Fix |
|---|---|---|---|
| R14 | A Publisher canary rollback step (state READY) is realized like a saving: demo `wh-demo-040` books -800.00 as CLAIMABLE | P1 | Step role `SAFETY_ROLLBACK` (candidate blocker `CANARY_GUARDRAIL_BREACHED`, target equals the `observed_change` previous value): conformance recorded, no savings claim. RD06. |
| R15 | Photon is in the configuration hash: a billing-mix flip (Photon MIXED) alone is DRIFTED with a regeneration request | P1 | Decision knobs only (type, size, min, max, auto-stop, channel) in matching, hash and drift; Photon stays an observed fact. RD07 for the channel. |
| R16 | Changes to knobs with no authority step are invisible: out of run scope, NO_CHANGE candidates, pending or blocked steps. Probe: auto-stop applied plus an owner size cut, 5,000 attributed as CLAIMABLE to auto-stop | P1 | Record every changed decision knob not explained by an authority step as `INDEPENDENT_CHANGE_OBSERVED` with a reason (`OUT_OF_RUN_SCOPE`, `PENDING_REEVALUATION`, `BLOCKED_STEP`, `NOT_RECOMMENDED`); the counterfactual must hold it at its new value or the claim is OBSERVABLE_ONLY. RD08. |
| R17 | The applied scenario is never inferred: a migration is seen only when the caller names Plan B; a migration-only scope (step-less recommended NO_CHANGE_BASELINE) never sees it; the compatibility step is ignored | P1 | Match the observed state against every scenario's steps; `applied_scenario_id` becomes an override; record the scenario's compatibility status (RD11). |
| R18 | Denominators: a scenario-scope application of a plan prefix uses the whole scenario total; `(total or dbu)` makes DBU-only savings the "total"; migration ranges are pre-canary but judged as authoritative | P1 | Prefix = `running_savings_365d` of the last applied step; single step = candidate isolated economics; total only when non-null (else UNAVAILABLE); DBU and AWS expected kept separate; migration denominators informational. |
| R19 | Guardrails contradict the package: Realizer fails any p95 rise above 5 percent, the package's cost profile accepts 15 / 25 / 40 percent and the Publisher prices a +30 percent downsize slowdown; unknown metrics count as failed; canary steps realize at day 7 while the Publisher's guard window is 30 days | P1 | Guardrails from the package's `cost_profile.applied_thresholds` and step `validation.success_metrics`, same two-part rule as the Publisher canary guard; Realizer policy can only be stricter; per-rule PASS / FAIL / UNKNOWN (UNKNOWN = not ready); canary-type steps provisional until 30 days. RD09. |
| R20 | Capacity adds (max-cluster increase, upsize) and zero-priced cuts: expected savings negative or 0, so they stay MONITORING forever or a successful queueing fix reads as UNDER_REALIZED with re-optimization | P1 | Step intent from the candidate `objectives` and the savings sign; PERFORMANCE steps succeed on queue and p95 against the pre window with the cost increase inside the modeled range. RD10. Covers known limitation 14. |
| R21 | `remeasure_days` read from the candidate step (always null in 3.1.0) instead of the REMEASURE step | P2 | Read the scenario's REMEASURE step. |
| R22 | RPKG records no `run_scope`, `cost_profile`, `observed_change`; plan progress ignores the scope (a one-step scoped plan reads PARTIALLY_COMPLETED) | P2 | Record them in `source_recommendation` and the lineage entry; derive progress from the scoped plan. |
| R23 | Regeneration requests carry no evidence reference and the previous step's `invalidates` as "impacted knobs" | P2 | Attach the evidence id, window and the knobs that actually changed. |
| R24 | MODIFIED needs a re-estimated denominator nobody produces | P2 | RD12. |

### Batch 3 · Live collector and the auto-stop realized savings, with end-to-end goldens

| ID | Finding | Band | Fix |
|---|---|---|---|
| R25 | No live collector | P0 | `src/workflow_shared/` for the neutral Databricks access layer (OAuth AuthContext, PAT rejection, read-only SqlClient with the UTC guard, REST GET), behavior-preserving for the Publisher (compare_with_ref 0 diffs); the Realizer calls the Publisher's evidence extraction through one bridge module, then a pure Realizer-owned projector `evidence -> observation`. |
| R26 | Collector SQL is anchored on now and the current epoch (running-idle, daily resources, canary guard): a closed post-change epoch cannot be measured | P0 | Parameterize by `(epoch, as_of)`; the default renders today's SQL byte for byte (RD14). |
| R27 | No counterfactual model | P0 | Auto-stop first: replay the actual post-change idle gaps with the old value (`running_idle_by_target[old]`), restarts at the package's assumption, priced at the window's billed DBU per cluster-hour; old value 0 (never stop) replayed exactly. Other knobs in batch 6. |
| R28 | A replay counterfactual is the optimistic (1.0) track by construction but is compared with the base (0.75) track: ratio inflated about 1.33 | P1 | RD15. |
| R29 | Window and readiness are wall-clock days and caller booleans | P1 | Window = whole settled UTC days from the first full day after the change to the Publisher's `billing_complete_through`; readiness from settled billed days, the package's sample floors and a computed representative regime (whole weeks or billing cycles, volume within 0.75..1.25 of the pre window, no other decision change). |
| R30 | No matched pre/post comparison (unequal windows, weekday mix, query mix) | P1 | Equal whole-week windows before and after; per-source comparison re-weighted by pre counts (the canary guard's source key); volume ratio reported. |
| R31 | The collector's own queries land on the warehouse being measured and keep it awake | P1 | Refuse a collector warehouse equal to the measured one; exclude the product's own statements from busy intervals. |
| R32 | Closed-epoch `_30` fields are false zeros (relative to now) | P1 | Never read `_30` fields for a non-current epoch; window-bound SQL. |
| R33 | Restart billing (4 cluster-minutes per Pro/Classic start) is an assumption that moves the auto-stop result up to 5x | P1 | RD16. |
| R34 | DBU/AWS components are echoed, not computed; modeled AWS can be reported as cash | P2 | Compute per component, reconcile to the total within 0.03, `aws_cash_realized` null until CUR exists, AWS labeled MODELED. |
| R35 | Origin has no source: with `require_confirmed_origin_for_hard_kpi`, a live run is never CLAIMABLE | P1 | RD17. |
| R36 | Counterfactual record hashes the caller's empty `workload` and a raw window start | P3 | Hash the evidence fingerprint, window, method, replay target and assumptions into the CF id. Covers known limitation 11. |
| R37 | No end-to-end goldens: every GR row runs one frozen 1.0.0 package; no Publisher-to-Realizer test (PRD Definition of Done) | P1 | Golden case framework (section 4): real PKG with scope and profile, simulated post-change world, oracle counterfactual from the same simulator, `realize()`, `expect` assertions. RD18. |
| R38 | Workload shift after the change is read as under-realization | P2 | Report a workload-shift detail and suppress re-optimization when the shortfall is volume (RD19). |

### Batch 4 · Lifecycle and ledger for daily runs

| ID | Finding | Band | Fix |
|---|---|---|---|
| R39 | No cross-package exclusivity: two RPKGs for one warehouse both report CLAIMABLE 1,000 (GR-030 passes only because the test sets `overlap_count` by hand) | P0 | One owner per (warehouse, knob, interval). RD20. |
| R40 | No registry of the current package per warehouse; supersession only through a caller-set `replacement_package_id`; NOT_APPLIED lineages accumulate forever | P0 | State index by warehouse; package family link through the later package's `observed_change.previous_epoch_id`; retire never-applied lineages when a newer package for the same epoch arrives. |
| R41 | After ROLLED_BACK or DRIFTED the snapshot still shows CLAIMABLE 1,000 and an annualized 12,166.67 | P1 | RD21. |
| R42 | Re-application after a rollback is folded into the old episode; a second identical drift or rollback is suppressed (event ids exclude time) | P1 | Recurring lifecycle events carry the effective time in their identity; a closed episode never absorbs a new application. |
| R43 | `rollback_observed` is a trusted flag (accepted with the configuration unchanged) | P1 | Rollback only when the observed value returns to the step's source value. |
| R44 | A later authoritative step of the same plan reads as DRIFT and stops realization | P1 | RD22. |
| R45 | Realized to date and mature TTM are labels: "since application" is the latest window, any supplied TTM makes the state MATURE | P1 | RD23. |
| R46 | Measurement identity excludes the costs, and "latest" is decided by hash order at equal recorded time: a re-measured window kept the older value | P1 | Ledger sequence tie-break; a second measurement of an existing window with different economics must be a correction. |
| R47 | Billing restatement: re-collection without a correction object replaces the number silently; reprocessing a restatement adds a second correction | P2 | The collector compares with the prior measurement of the same window and emits the correction; one correction per key. Covers known limitations 7 and 10. |
| R48 | Timestamps compared as strings (microseconds break ordering); drift/rollback intervals can be zero-length | P2 | Compare parsed times; end after start; effective time from the epoch boundary. |
| R49 | Ledger has no lock and a truncated last line breaks the lineage permanently | P1 | Advisory lock per ledger; quarantine one unterminated trailing line. |
| R50 | Supersession is unconditional (any string, even the package itself); the churn rule (GR-035/036) and PolicyDiff (GR-034) are not on the run path | P2 | Validate the id; run the 5 percent rule and PolicyDiff in the lifecycle. |

### Batch 5 · Fleet operations and visibility

| ID | Finding | Band | Fix |
|---|---|---|---|
| R51 | One `--package` per process; no run-folder mode, no per-warehouse failure isolation, no overall exit status | P0 | `realizer --publisher-run <dir or root>` over the manifest (latest run by default), failures isolated per warehouse, exit 2 if any failed, run manifest. RD24. |
| R52 | Ledger lives under `--output-dir`: a fresh dated output folder each day silently forks the history | P1 | `--state-dir` (long-lived ledger, evidence, regeneration) separate from Hive run folders for projections, manifest and log. RD24. |
| R53 | No realization view for a person; the Publisher workbook shows NOT_APPLIED for applied warehouses | P1 | RD25. |
| R54 | `realizer --demo` is one package with constants | P2 | `realizer --demo` over a Publisher demo run through the golden twin (batch 3), honoring scope and profile; refuses packages that are not demo-fleet members. |

### Batch 6 · Counterfactuals for size, clusters and migration

| ID | Finding | Band | Fix |
|---|---|---|---|
| R55 | Size: reusing the Publisher's runtime multiplier makes the counterfactual circular | P1 | Measure the runtime ratio on matched sources and use it; old size's DBU per cluster-hour from the previous epoch of the same warehouse; publish the measured multiplier. |
| R56 | Max and min clusters need minutes by cluster level, which are not collected | P1 | Level-histogram SQL; cut realized against the old cap or floor; performance criteria per R20. |
| R57 | Migration has no reverse (new to old) model | P2 | Observe-only first (RD26), reverse model with the billing reconciliation band later. |

### Batch 7 · Golden hardening and release

| ID | Finding | Band | Fix |
|---|---|---|---|
| R58 | Weak non-waivable goldens: GR-004 and 039 never call `realize()`; GR-022/023/030 use a literal id or a flag instead of a second package; GR-023 repeats one comparison | P1 | Rebuild them as end-to-end cases with real second packages. |
| R59 | Spec section 4 ledger invariants (append-only prefix across runs, non-overlapping intervals, lineage, replay) are not checked generically; the registry checks only that a test method exists | P2 | One invariant checker run over every case; registry gains kind and non-waivable flags. |
| R60 | Realizer docs (PRD, HLD, LLD, contracts, known limitations, README, examples) describe the 1.0.0 shell | P2 | Update with each batch; a real-data runbook section for the Realizer next to the Publisher's. |
| R61 | Small hygiene: over-realization (ratio 30) shows REALIZED with no plausibility flag; `recommended == current` steps are always EXACT; snapshots never pruned; default output dir is the Publisher's | P3 | Plausibility flag above the package's high; default `./realizer_out`; retention note. Covers known limitations 12, 13, 16. |

## 3. Decisions

Each decision has a recommendation; the worked example uses numbers from the probes or the demo fleet.

### Batch 1

**RD01 · Keep contract 1.0.0 input?** The Publisher can no longer write or validate 1.0.0 (its committed schema pins 3.1.0); only test fixtures depend on it (2 packages, 41 stored scenarios, 28 expectation files). *Recommended: retire it.* Convert the two fixture packages to the 3.1.0 shape keeping their PKG ids (RPKG ids hash only the PKG id and the contract major), add fixtures cut from real demo packages, regenerate the stored outputs with an approved change. Alternative: a read-only 1.0.0 reader behind the same view for one release.

**RD02 · Which package versions to accept?** *Recommended: exactly 3.1.0.* A later MINOR adds keys the reader should see on purpose; accepting any 3.x would let a new optional block through unreviewed.

**RD03 · A change applied before the package existed.** Probe: package generated 2026-10-01, observation says applied 2026-09-01 at the recommended value: today REALIZED and CLAIMABLE. The Publisher saw that state (it would be the package's `current_config` or `observed_change`), so the package did not cause it. *Recommended: record it as an independent change with reason `APPLIED_BEFORE_PACKAGE`, no claim*, instead of failing the whole run.

**RD04 · `PRODUCT_VERSION` in provenance.** Every ledger entry and RPKG says `2.8.0`. *Recommended: the release version from one place (`publisher.config.versions.PUBLISHER_VERSION`, today 2.14.0) as `product_version`, the Realizer component versions next to it.* Changes stored provenance, not ids; regenerate goldens once now, before any real ledger exists.

**RD05 · Observation strictness.** *Recommended: a new strict observation format (`observation_format_version` 2.0.0, JSON Schema in `schema/`) with required keys and fail-closed gates; the demo and the live projector both emit it; the stored scenarios are rewritten in it.* Example: today an observation that omits `price_coverage_pct` while 12 percent of DBU is unpriced passes the 100 percent price-coverage gate; under the strict format the gate reads UNKNOWN and the claim waits.

### Batch 2

**RD06 · Publisher canary rollback in a package.** `wh-demo-040`: the size canary MEDIUM failed its guard, the next package recommends LARGE again. *Recommended: (a) the rollback package's step is a safety action, applied or not, never savings; (b) when its `observed_change.previous_epoch_id` matches the earlier package's epoch, the earlier lineage records ROLLED_BACK at the rollback time.*

**RD07 · Warehouse channel change.** The Publisher starts a new epoch on a channel change but never prices it. *Recommended: record it as an independent change with no drift and no claim effect* (no priced effect), Photon never as a change.

**RD08 · An unplanned or out-of-scope change in the measurement window.** Probe: `--scope current:auto-stop` package (auto-stop 60 to 20), the owner also cuts size MEDIUM to SMALL. Today: 5,000 of savings attributed to auto-stop, CLAIMABLE. *Recommended:* record the size change as `OUT_OF_RUN_SCOPE`; the counterfactual changes only the applied knob back to its source value and keeps every other observed value (auto-stop 60 replayed on the SMALL warehouse's actual gaps at the SMALL rate), so the claim measures the auto-stop effect alone and stays claimable; when the counterfactual method cannot hold other knobs fixed (a caller-supplied before/after), the claim is OBSERVABLE_ONLY. An out-of-scope change alone does not trigger regeneration.

**RD09 · Which guardrails govern validation.** Balanced package, size canary LARGE to MEDIUM, p95 +12 percent after the change. Realizer today: VALIDATION_FAILED, successful savings 0. Publisher's next run: no breach (12 < 25), the change stays. *Recommended: the package's cost-profile thresholds with the Publisher's two-part rule (above the safe level and above previous x (1 + pct)); the Realizer policy may only tighten them; a metric without enough sample is UNKNOWN (not ready), never FAILED; canary-type steps stay PROVISIONAL until the 30-day guard window ends.*

**RD10 · Capacity adds.** `wh-demo-008`: max clusters 2 to 5 for queueing, priced at -15,628/yr. Illustrative: after the change queueing falls from 8 to 0.4 percent and cost rises 14,100/yr. Today: negative savings, UNDER_REALIZED with re-optimization. *Recommended: a PERFORMANCE step succeeds when queue share and p95 improve past the step's metrics and the cost increase stays within the modeled range (here 14,100 within 15,628 x 1.25): state PERFORMANCE_VALIDATED, cost reported signed and never counted against savings. A zero-priced max cut is CAPACITY_VALIDATED when queueing does not regress.*

**RD11 · Migration applied with compatibility unresolved.** 31 of 34 demo Plan B scenarios are REQUIRES_VALIDATION. *Recommended: annotate `COMPATIBILITY_NOT_EVIDENCED` on the application and treat the migration range as informational (no REALIZED/UNDER_REALIZED against a pre-canary range).*

**RD12 · Denominator for a MODIFIED value.** Package says 20, owner sets 30. *Recommended: for auto-stop, price the applied value with the same replay on the package-time evidence (the Publisher's option grid already holds every 5-minute value); for other knobs the ratio stays null (state MONITORING) until batch 6.*

### Batch 3

**RD13 · Shared Databricks access code** — decided 2026-10-06: `src/workflow_shared/`, behavior-preserving for the Publisher, importing neither product.

**RD14 · Measuring a closed epoch.** Owner lowers auto-stop on 2026-10-01; someone changes size on 2026-10-12: the auto-stop epoch is now closed and the current SQL cannot read it. *Recommended: parameterize the Publisher's SQL builders by epoch and as-of time, with the default rendering today's SQL byte for byte* (one epoch definition), rather than a second Realizer-owned copy.

**RD15 · What "realized" is compared with.** C01: replay removable 124.1k/yr (optimistic track, 1.0), base 93.1k (0.75), low 62.0k (0.5). A replay counterfactual measures the full mechanical effect, so comparing it with the base always reads high. *Recommended: classify against the published range: annualized realized at or above the step's low = REALIZED, below = UNDER_REALIZED (re-optimization below half the low); the ratio against the base is reported; above the high is flagged for review.* Illustrative: 110k realized = REALIZED, ratio 1.18; 55k = UNDER_REALIZED. Alternative: compare with the optimistic track at 0.80.

**RD16 · Restart billing assumption.** If starting minutes are not billed, the auto-stop counterfactual changes up to 5x (65 vs 360 USD in a 28-day window). *Recommended: use the package's own assumption (4 cluster-minutes per Pro/Classic start, recorded in the counterfactual) so realized and expected are comparable, and settle it in the real-data run*; alternative: publish two realized numbers (with and without restart billing).

**RD17 · Origin and claimability.** Live runs have no confirmed origin, so nothing would ever be CLAIMABLE. *Recommended: INFERRED origin (the change happened after the package was generated and equals the recommended value, or is a MODIFIED value in the same direction) qualifies for the strict KPI; CONFIRMED when an approval file (`--approvals`) names the change; reading `system.access.audit` deferred.*

**RD18 · Golden framework.** *Recommended:* cases under `tests/golden/realizer/cases/<name>/` with ids `GE-NN`, the post-change world and the oracle counterfactual from the Publisher's demo simulator through a small public function (the Realizer already depends on the Publisher), Publisher-stage and Realizer-stage outputs regenerated separately; the GR rows remain the contract layer.

**RD19 · Workload shift.** Post-change volume halves, the auto-stop saving halves with it. *Recommended: report the volume ratio and a `WORKLOAD_SHIFT` detail; no re-optimization flag when the shortfall is explained by volume; the denominator stays the package's.*

### Batch 4

**RD20 · Which lineage owns savings when packages overlap.** PKG-A (Oct 1) recommends auto-stop 20, applied Oct 8; PKG-B (Oct 15) already has auto-stop 20 as `current_config` and recommends a size cut. *Recommended: the lineage whose step the change realizes owns that knob's interval (PKG-A for auto-stop from Oct 8); PKG-B starts from its own current configuration and can only claim the size change; PKG-B is linked as PKG-A's successor through `observed_change`; a later package with the same target never re-claims it.*

**RD21 · Savings after rollback or drift.** Applied Oct 8, realized 6,734 through Nov 4, rolled back Nov 5. *Recommended: savings to date stay at 6,734 (closed interval, still claimable for that interval), annualized and ratio become null, state ROLLED_BACK.*

**RD22 · A later step of the same package applied.** *Recommended: a READY step applied later is plan progress (a new application on the same RPKG; scenario-scope measurement from then on); a PENDING_REEVALUATION step applied early is an independent change (no credit) and asks for regeneration; only changes no step explains are drift.*

**RD23 · Realized to date with daily runs.** Today three daily runs with overlapping 7-day windows each say "since application 1,000". *Recommended: the collector measures each settled day once; realized to date is the signed sum over non-overlapping days in the ledger (a restated day is a correction); annualized is the trailing 28-day run-rate; MATURE when 365 settled days are covered.*

### Batch 5

**RD24 · State and run folders.** *Recommended: one long-lived `--state-dir` (ledger, evidence, regeneration) and Hive `run_date=/run_ts=` folders under `--output-dir` for the projections, a run manifest and the log; `--publisher-run` takes the latest Publisher run under a root by default; a failed warehouse does not stop the others and the run exits 2.*

**RD25 · Where a person sees realization.** *Recommended: a separate realization workbook written by the Realizer run (one row per warehouse: applied, conformance, validation, state, realized to date, annualized vs expected, ratio, attribution, next action, last observed), the Publisher's four-sheet workbook unchanged; joining later through a combined report command.*

### Batch 6

**RD26 · Migration realization.** *Recommended: observe-only first (OBSERVABLE_ONLY, no counterfactual claim), reverse model later.*

## 4. Golden scenarios

Today's 40 GR rows stay as the contract and regression layer, re-based on 3.1.0 fixtures (RD01). The new end-to-end layer (RD18) mirrors the Publisher's golden cases: a case is a real Publisher package for a demo-fleet warehouse with a given `--scope` and `--cost-profile`, a simulated post-change world (owner actions on a timeline, workload changes, billing restatements), the oracle counterfactual from simulating the same workload under the old configuration, and `realize()` runs, with `expect` assertions (`within_pkg` range bands, `near_oracle`, signs, ledger invariants). The demo simulator's noise depends only on scenario and day, so the old/new difference is purely the configuration effect; agreement is a consistency check, not accuracy evidence, which only the real-data run gives.

First cases (batch 3): auto-stop exact with full scope (C05), auto-stop under `--scope current:auto-stop` (C01, one-step plan, COMPLETED), out-of-scope size change alongside (RD08), auto-stop modified, aggressive vs balanced profile on the same evidence (C54: EXACT against one package, MODIFIED against the other), wrong warehouse, applied before the package, currency mismatch. Later batches add the size canary pass/fail with rollback, the canary-rollback package (C55), capacity adds (C08), max and min cuts, migration observe-only, multi-step plans, billing restatement, monthly workloads, replacement packages and overlapping lineages. The full catalog lives in the golden-scenario document when batch 3 starts.

## 5. Process

Per batch: one branch in `.worktrees/realizer-gaps`, red tests first, one commit per finding, the CLAUDE.md change loop after each, then one `/code-review low` and one independent review of the batch diff, one merge to master on approval, the tracker rows set to Done and [`tracker.md`](tracker.md) regenerated. A finding that hits a stop condition (an undecided policy value, a contract change without approved text) is set to Needs input and does not block the rest. Docs describe the current behavior in generic words; finding ids stay in commit messages, the tracker and this document. Realizer contract changes follow its own versioning (ledger and RPKG ids hash the contract major).
