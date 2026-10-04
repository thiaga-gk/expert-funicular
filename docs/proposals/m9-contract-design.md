# M9 contract design: show the next step after an applied change

Status: **DESIGN SIGNED OFF on 2026-10-03 (decisions D1 to D7 as recommended in section 13). No contract, schema, workbook or publisher code has been written yet.** Implementation of Stage 1 is the next step; Stage 2 ships after M2. The CLAUDE.md exception in section 9 is **not** applied and will be shown again when Stage 2 is built. Method: `databricks-contract-designer` skill, applied to an existing locked contract, which "evolves deliberately by version" rather than being rebuilt in the generic form.

Inputs: `docs/proposals/audit-m-group-b.md` section M9 (options A/B/C and the ExecutionState taxonomy), `contracts/reporter_contract.py` (1.1.0), `contracts/reporter_schema.py`, `contracts/workbook_contract.py`, `docs/reporter-json-contract-v1.1.0.md`, `docs/workbook-contract-v1.2.0.md`.

Not run: `validate_contract_bundle.py`. It checks a YAML bundle (`domain-contract.yaml`, `reporter-contract.yaml`, `workbook-contract.yaml`) that this repo does not have; the locked contract lives in Python + a generated JSON Schema + versioned docs. I did not invent a YAML bundle for one change.

## 1. Goal and constraints

After a user applies a plan step, the new config epoch has little history. Today the next step is hidden for about 7 days: `CURRENT_CONFIG_EPOCH_TOO_NEW` blocks size/min/auto-stop, and the 7-day cost-history floor withholds economics. The user wants the next step shown **with an estimated saving** instead of a blank.

Binding constraints (decided earlier, not relitigated):

| Constraint | Consequence for the design |
|---|---|
| Publisher is stateless between runs | "A step was applied" must be derived from evidence, not remembered. |
| No realization ingestion; Phase 1 emits `NOT_APPLIED` | Nothing may claim a saving was realized. |
| H1: figures priced from pre-change evidence are CONTINGENT, never authoritative | Carried-forward figures never feed package/scenario economics. |
| Unknown cost is null, never zero | Missing or unreplayable figures are null. |
| Current epoch controls active recommendations; historical epochs are calibration only | Stage 2 needs a **documented exception** (section 9). Stage 1 needs none. |
| Identical normalized evidence + versions give identical output and IDs | Every new field is a pure function of evidence and versions. |

## 2. Why the current plan structure cannot carry this

A `PlanStep` of type `CANDIDATE` must reference a candidate of the **current** package (`candidate_id in candidates`, validator in `PlanStep.validate`). After a change, the current epoch has fresh candidates; the prior epoch's remaining steps (max, min, auto-stop) have no current candidate. Stuffing them into `execution_plan` would break cross-reference validation or invent candidates that the current evidence does not support. So carried-forward steps need their own block, outside `candidates`, `scenarios` and `package_economics`.

Also: `ExecutionState.COMPLETED`, `INVALIDATED` and `BLOCKED` exist in the enum but are never emitted and have no validator semantics.

## 3. Staging and versions

| | Stage 1 | Stage 2 |
|---|---|---|
| Delivers | "A change was observed; the next evaluation is due at T because of codes C." No figures. | Adds a labeled, contingent preview of the prior plan's remaining steps with remaining upside. |
| Needs new collector evidence | **No.** The epoch list already holds the prior epoch's config. | Yes: the prior epoch's windows anchored at its `epoch_end` (depends on M2). |
| CLAUDE.md exception | No | **Yes** (section 9) |
| Contract | 1.1.0 to 1.2.0 | Additive on top of 1.2.0 |
| Workbook | 1.2.0 to 1.3.0 | Further additive columns |

Recommendation: **one contract version bump (1.2.0) that defines both stages' fields**, with the Stage 2 fields present but always null until Stage 2 ships. Every contract version bump changes every `EPOCH/EVID/CAND/SCN/REAL/PKG` ID (`contract_version` is in every hash), so one bump is cheaper than two. The cost is a reserved, initially unused block, as `enable_photon` was reserved in 1.1.0. Alternative: two bumps (1.2.0, 1.3.0), no reserved fields, two rounds of ID churn. See decision D2.

## 4. Contract delta (1.1.0 to 1.2.0)

All new keys are **required but nullable**, matching the existing pattern (`projected_economics`, `contingent_*` are `nullable(ref(...))`). Existing fields and enums are unchanged.

### 4.1 `recommended_plan.reevaluation` (Stage 1)

Type `ReevaluationWindow | null`. Null when nothing time-gated blocks the next evaluation.

| Field | Type | Null | Domain / unit | Meaning |
|---|---|---|---|---|
| `status` | enum | no | `WAITING_FOR_EVIDENCE` | Only value in 1.2.0. (`NONE` is represented by the whole object being null.) |
| `earliest_reevaluation_time` | string | no | ISO-8601 UTC `Z` | Earliest instant both time gates can be satisfied. **Not a promise**: ingestion lag and query-sample gates can still delay it. |
| `blocking_codes` | string[] | no | non-empty, sorted, unique, `^[A-Z][A-Z0-9_]*$`; closed set in 1.2.0: `CURRENT_CONFIG_EPOCH_TOO_NEW`, `COST_HISTORY_INSUFFICIENT` | The time-clearable gates currently active. |
| `method` | string | no | non-empty | Fixed description of the arithmetic below. |

Arithmetic (pure function of evidence and thresholds):

- `t_maturity = epoch_start + current_config_stability_days (7) days`.
- `t_cost = 00:00Z of (epoch_start_date + min_economics_cost_history_days (7) days)`, the earliest time that day 6 of history can be fully covered.
- `earliest_reevaluation_time = max(t_maturity, t_cost)` over only the gates currently active.
- Example: `epoch_start = 2026-10-01T15:00Z` gives `t_maturity = 2026-10-08T15:00Z`, `t_cost = 2026-10-08T00:00Z`, result `2026-10-08T15:00Z`.

Invariants:

- R1. `earliest_reevaluation_time >= current epoch start_time`.
- R2. `blocking_codes` is non-empty exactly when the object is non-null.
- R3. `CURRENT_CONFIG_EPOCH_TOO_NEW` in `blocking_codes` implies `warehouse.current_epoch.age_days < current_config_stability_days (7)`; `COST_HISTORY_INSUFFICIENT` implies the package's `cost_history_days` evidence record (already published, `source = system.billing.usage`) has `value < min_economics_cost_history_days (7)`. The validator reads both from content already in the package; the two thresholds become documented contract constants (7 and 7) so the validator does not depend on publisher configuration.
- R4. The object is null once both gates are clear, even if other blockers (query-sample size) remain. Those are shown by candidate blockers, as today.

### 4.2 `recommended_plan.next_action` values (no schema change; `next_action` is a free string)

New value `WAIT_FOR_REEVALUATION` with `selection_reason_code = CURRENT_EPOCH_IMMATURE`, used only when `reevaluation` is non-null **and** the recommended scenario has no step in `READY` / `READY_FOR_CANARY`. A young epoch can still carry a ready step (for example a max-cluster increase for queueing), and then the existing actions apply.

### 4.3 Top-level `observed_change` (Stage 1)

Type `ObservedConfigChange | null`. A purely observational record that the configuration changed at the start of the current epoch. **It is not a realization record and carries no savings fields.**

| Field | Type | Null | Meaning / source |
|---|---|---|---|
| `previous_epoch_id` | `EPOCH-*` | no | The immediately preceding epoch of the same `(workspace_id, warehouse_id)`. |
| `previous_epoch_start` | ISO-8601 | no | `system.compute.warehouses` (AUTHORITATIVE). |
| `changed_at` | ISO-8601 | no | Equals the current epoch's `start_time`. |
| `changes[]` | `{knob, previous_value, current_value}` | no, non-empty | One entry per config field that differs, sorted by `knob`. Fields: warehouse type, channel, size, min clusters, max clusters, auto-stop. |
| `measurement_status` | enum | no | Only `NOT_MEASURED` in 1.2.0. Anything that would say "worked" needs realization ingestion. |

Null when the current epoch is the first in the retained window (365 days of config history) or the diff is empty.

Invariants: O1 `changes[*].previous_value != current_value`; O2 `changed_at == warehouse.current_epoch.start_time`; O3 `previous_epoch_id != warehouse.current_epoch.epoch_id`; O4 `changes` sorted and unique by `knob`; O5 (no-savings rule) the object has no cost fields, enforced structurally by the schema (`additionalProperties: false`).

Why not `Realization.status = APPLIED`: `AppliedChange` requires `applied_by`, which a config diff cannot supply, and `APPLIED` implies a validation lifecycle that does not exist. Answer to the open question: **realization stays `NOT_APPLIED`**. Decision D5.

### 4.4 Top-level `carried_forward_plan` (Stage 2; null until shipped)

Type `CarriedForwardPlan | null`. A replay of the plan the publisher would have produced for the immediately preceding epoch at its `epoch_end`, shown only while the current epoch is younger than the maturity gate.

`CarriedForwardPlan`:

| Field | Type | Null | Meaning |
|---|---|---|---|
| `source_epoch_id` | `EPOCH-*` | no | Equals `observed_change.previous_epoch_id`. |
| `as_of` | ISO-8601 | no | The prior epoch's `epoch_end`; all replay windows are anchored here. |
| `match_policy` | enum | no | `EXACT_SINGLE_KNOB` (only value). |
| `match_outcome` | enum | no | `MATCHED`, `MISMATCH`, `NOT_REPLAYABLE`. |
| `match_reason_code` | string | yes | Required unless `MATCHED`. Examples: `MULTIPLE_KNOBS_CHANGED`, `DIFFERENT_VALUE_APPLIED`, `TYPE_CHANGED`, `PRIOR_EVIDENCE_UNAVAILABLE`. |
| `replay_fingerprint` | string | no | `SHA256:<64 hex>` over the replay evidence and engine versions. |
| `steps[]` | `CarriedForwardStep` | no | Empty only when `match_outcome = NOT_REPLAYABLE`. |
| `remaining_upside_365d` | `ComponentRanges` | yes | See rebasing below. Null when unpriceable. |
| `pricing_basis` | enum | no | Only `CONTINGENT_CARRIED_FORWARD`. |

`CarriedForwardStep`:

| Field | Type | Null | Meaning |
|---|---|---|---|
| `order` | int >= 1 | no | Contiguous from 1. |
| `knob` | `TunableKnob` | no | |
| `technique_id` | string | no | Catalog identity. |
| `previous_value`, `recommended_value` | scalar | no | The prior plan's step. |
| `disposition` | enum | no | `COMPLETED`, `PENDING_REEVALUATION`, `INVALIDATED` (subset of `ExecutionState`, reusing its vocabulary). |
| `reason_code` | string | yes | Required for `INVALIDATED`. |
| `rebased_running_savings_365d` | `ComponentRanges` | yes | Only on `PENDING_REEVALUATION`; null otherwise. |

**Rebasing rule (avoids double counting).** The prior contingent chain's running savings include step 1, which the user has already applied. Publishing "$20,066" after step 1 was applied would present already-achieved (and unmeasured) savings as upside. So each pending step publishes its running savings minus the running savings after the last `COMPLETED` step, track by track (optimistic/base/conservative), then ordered low <= base <= high. `remaining_upside_365d` equals the last pending step's `rebased_running_savings_365d`. Worked example (from M9 section 2): applied step 1 at $11,010; steps 2 to 4 reach $20,066 cumulative, so the preview shows about $9,056 remaining upside, not $20,066.

Invariants:

- C1. `source_epoch_id == observed_change.previous_epoch_id`; `observed_change` must be non-null.
- C2. `COMPLETED` steps must appear in `observed_change.changes` (same knob, `recommended_value == current_value`); `COMPLETED` steps carry no savings.
- C3. `match_outcome = MATCHED` requires `observed_change.changes` to have exactly one entry and the first replayed step to be `COMPLETED`.
- C4. `MISMATCH` requires every step `INVALIDATED` (or `steps` empty) and `match_reason_code`; `NOT_REPLAYABLE` requires empty `steps`, null `remaining_upside_365d`.
- C5. `pricing_basis` is `CONTINGENT_CARRIED_FORWARD`; nothing in `package_economics`, `scenarios[*].portfolio_economics` or `excluded_scenarios` may reference or include these figures.
- C6. `remaining_upside_365d` is null or equals the last pending step's rebased figure within 0.03 (same tolerance as the 1.1.0 contingent rule).
- C7. Unavailable means null: no zero-valued stand-ins.
- C8. Present only while `reevaluation` is non-null (epoch younger than maturity). Once the current epoch is mature, the current plan supersedes it.

### 4.5 `ExecutionState` semantics (no enum change)

`READY`, `READY_FOR_CANARY`, `PENDING_REEVALUATION`, `REQUIRED` keep their 1.1.0 meaning. In 1.2.0 plan steps (`execution_plan`) **still never** use `BLOCKED`, `COMPLETED`, `INVALIDATED`; those names are used only as carried-forward `disposition` vocabulary and are documented once in the contract doc:

| Value | Meaning in 1.2.0 |
|---|---|
| `COMPLETED` | The step's recommended value is the authoritative current config and the prior epoch held its previous value. Config applied, **not savings realized**. |
| `INVALIDATED` | A prior-plan step whose premise no longer holds (another value applied, several knobs changed, type changed). |
| `BLOCKED` | Reserved. Not emitted in 1.2.0. A plan step with a named time-clearable blocker is conveyed by `reevaluation`, not by a step. |

Rationale: putting `BLOCKED` steps into `execution_plan` would require steps without a current candidate or a new step type, which cuts across the existing cross-reference rules, for no figure. Decision D4.

## 5. Identity (`PKG-*` and fingerprints)

- `make_recommendation_package_id_from_parts` payload gains three keys, each `null` when the block is null:
  - `reevaluation`: canonical JSON of the `ReevaluationWindow` (deterministic from epoch start, thresholds and billing coverage).
  - `observed_change_fingerprint`: `SHA256` of the canonical `ObservedConfigChange`.
  - `carried_forward_fingerprint`: `CarriedForwardPlan.replay_fingerprint`.
- Rationale: these blocks change what a consumer sees without changing candidate, scenario or evidence IDs, so they must alter `PKG-*`. The full package hash already covers all content.
- No new ID prefix. Per-step IDs are unnecessary (identity is positional within a fingerprinted block).
- `replay_fingerprint` inputs (Stage 2): contract version, workspace, warehouse, `source_epoch_id`, `as_of`, the replay evidence records' normalized `(metric, value, unit, source, source_field, observation_window)` sorted by metric, and the policy / analyzer / optimizer / estimator / bundle / decision-graph / technique-catalog versions.
- Excluded from every ID: `generated_at`, prose (`method`, `decision_summary`), workbook formatting, logs, file names.
- `contract_version` moves 1.1.0 to 1.2.0, which changes every ID (`EPOCH`, `EVID`, `CAND`, `SCN`, `REAL`, `PKG`). This is the expected, accepted cost of any bump.

## 6. JSON Schema delta (Draft 2020-12, generated by `contracts/reporter_schema.py`)

New `$defs`: `ReevaluationWindow`, `ObservedConfigChange`, `ConfigChange`, `CarriedForwardPlan`, `CarriedForwardStep`. `RecommendedPlan` gains `reevaluation: nullable(ref("ReevaluationWindow"))`. The top-level object gains `observed_change` and `carried_forward_plan`, both `nullable` and added to `required`. All new objects use `additionalProperties: false`. Enums: `ReevaluationStatus {WAITING_FOR_EVIDENCE}`, `MeasurementStatus {NOT_MEASURED}`, `MatchPolicy {EXACT_SINGLE_KNOB}`, `MatchOutcome {MATCHED, MISMATCH, NOT_REPLAYABLE}`, `CarriedForwardDisposition {COMPLETED, PENDING_REEVALUATION, INVALIDATED}`, `PricingBasis` gains `CONTINGENT_CARRIED_FORWARD`. Structural checks only; cross-field rules stay in Python validators (section 7), per the reviewer principle.

## 7. Semantic validators (Python, `reporter_contract.py`)

R1 to R4, O1 to O5, C1 to C8 above, plus:

- X1. `recommended_plan.next_action == WAIT_FOR_REEVALUATION` requires `reevaluation` non-null and no `READY`/`READY_FOR_CANARY` step in the recommended scenario.
- X2. `contract_version == 1.2.0` in the package, `provenance` and every ID payload (existing check, constant changes).
- X3. `PricingBasis.CONTINGENT_CARRIED_FORWARD` appears only inside `carried_forward_plan`; the existing `PlanStep` rule ("pending iff CONTINGENT_PENDING_REEVALUATION") is unchanged.
- X4. Package economics code path must not read `carried_forward_plan` (enforced by a test that mutates the block and asserts `package_economics` and all `SCN-*` IDs are unchanged).

## 8. Workbook projection (Workbook Contract 1.2.0 to 1.3.0)

The workbook remains a projection only: it computes nothing and must not show carried-forward figures in any total.

Stage 1, `Warehouse Portfolio` (one row per package) gains three columns declared in `contracts/workbook_contract.py` (the renderer consumes the declaration):

| Column | Source | Type |
|---|---|---|
| `earliest_reevaluation_time` | `recommended_plan.reevaluation` | text (ISO-8601), blank when null |
| `reevaluation_blockers` | `blocking_codes` joined with `; ` | text |
| `observed_change` | `changes[]` rendered like `warehouse_size LARGE→MEDIUM` | text |

Stage 2 columns (design here, ship later): `carried_forward_outcome` (text), `carried_forward_remaining_upside_low/base/high` (numeric, signed, with the package currency and the existing derived impact type values (`SAVINGS` / `COST_INCREASE` / `MIXED` / `NO_CHANGE` / `UNAVAILABLE`), headers labeled "contingent"; null shown as the existing explicit unavailable state, never 0; no new impact-type value), and a text summary. Rules: Executive Summary aggregations exclude these columns; the numeric columns are labeled contingent in the header; no formulas.

Row grains are unchanged (portfolio one per package; scenario one per scenario; registry one per candidate). Updated acceptance tests: exact ordered columns per sheet, text-typed IDs, numeric economics, no formulas, no decorated strings.

## 9. Policy exception needed only for Stage 2

`CLAUDE.md` currently says: "Current epoch controls active recommendations; historical epochs are calibration/backtest only." Proposed exception text (**not applied; CLAUDE.md is not edited without your approval**):

> A historical epoch may be replayed solely to produce a CONTINGENT, non-authoritative carried-forward preview for the immediately preceding epoch while the current epoch is younger than the maturity gate. It never produces an active recommendation, never feeds candidate, scenario or package economics, and is anchored at that epoch's `epoch_end`.

## 10. Producer plug-in points (no code written)

| Stage | Function (new, pure) | Layer | Inputs |
|---|---|---|---|
| 1 | `derive_observed_change(e)` | Normalizer/analyzer boundary | `e.epochs` (already collected) |
| 1 | `compute_reevaluation(e, thresholds)` | Planner | epoch start, `cost_history_days`, thresholds, billing watermark metadata |
| 1 | adapter additions in `_build_locked_recommendation_package`, `_recommended_plan` | Contract adapter | the two results |
| 2 | `derive_carried_forward_plan(e)` | Planner (replay) | prior-epoch evidence anchored at `epoch_end`; calls the unchanged Analyzer, Optimizer, Estimator and ScenarioPlanner on a view whose current epoch is the prior epoch |
| 2 | collector: prior-epoch daily series and `_30` aggregates when `epoch_age < stability gate` | Collector | the M2 daily-series SQL |

Responsibilities stay separate: the replay reuses the existing layers rather than adding economics logic to the adapter. Old evidence without the Stage 2 inputs yields `match_outcome = NOT_REPLAYABLE` (fail closed), never a guessed preview.

## 11. Acceptance criteria and tests

Stage 1:

1. Golden `newly-changed-config` (epoch 2 days old): `reevaluation.earliest_reevaluation_time` computed per section 4.1; `blocking_codes` sorted; `next_action = WAIT_FOR_REEVALUATION` when no step is ready.
2. Golden with a ready step on a young epoch: `reevaluation` non-null, `next_action` unchanged.
3. `observed_change` for a single-knob change, a two-knob change, a type change; null for a first epoch.
4. Determinism: same evidence gives identical package IDs and bytes; `generated_at` excluded.
5. Negative validator tests for R1 to R4, O1 to O5, X1 to X4.
6. Schema round-trip (`jsonschema` Draft 2020-12) and workbook validation via `tools/validate_workbook.py`; exact column order; no formulas.
7. Demo regenerated once at release; report which decisions and economics changed (none expected: Stage 1 adds fields only).

Stage 2 (additional): `applied-size-step`, `applied-different-value`, `two-knobs-changed` goldens as in M9 section 8; a mutation test proving `package_economics` and `SCN-*` do not depend on the block (X4); rebasing arithmetic test on the $11,010 / $20,066 example giving about $9,056 base remaining upside.

## 12. Compatibility and rollout

- 1.1.0 packages are not valid under 1.2.0 code (exact version equality, as in the 1.0.0 to 1.1.0 bump); the repository keeps one current schema file and one doc per version.
- Consumers branch on `contract_version`. The new keys are additive and nullable, so a 1.1.0-aware consumer that ignores unknown keys keeps working after a version-gate update.
- Publisher version: new minor (2.9.0), because decisions do not change but public output does.
- Evidence format: Stage 1 needs no new evidence fields. Stage 2 needs the prior-epoch fields; absent fields fail closed (see section 10).

## 13. Decisions (all approved as recommended, 2026-10-03)

D2 outcome: one contract bump (1.2.0) that defines the Stage 2 `carried_forward_plan` block now; it stays null until Stage 2 ships. D3 outcome: the exception text is approved in principle but not applied to CLAUDE.md until Stage 2 is implemented after M2.

| # | Decision | Recommendation |
|---|---|---|
| D1 | Approve Stage 1 contract (`reevaluation`, `observed_change`, `WAIT_FOR_REEVALUATION`, workbook 1.3.0 columns)? | Yes. |
| D2 | One bump (1.2.0 with reserved, null Stage 2 fields) or two bumps? | One bump: avoids a second ID churn. |
| D3 | Stage 2 exception to "historical epochs are calibration only" (section 9 text)? | Approve the exception, but implement Stage 2 only after M2 delivers the daily series. |
| D4 | Keep `BLOCKED` out of `execution_plan` and convey waiting via `reevaluation`? | Yes. |
| D5 | Keep realization `NOT_APPLIED`; `observed_change` is separate and unmeasured? | Yes. |
| D6 | Rebased remaining-upside rule (section 4.4) instead of the cumulative figure? | Yes: the cumulative figure would double-count the applied step. |
| D7 | `earliest_reevaluation_time` as a date/time only for the two time-clearable gates (not query-sample gates)? | Yes, labeled "earliest". |

Known limits: `earliest_reevaluation_time` ignores ingestion lag and sample gates; Stage 2 value depends on how often users apply exactly the recommended value (unknown until real data); the replay costs an extra bounded scan; none of the SQL this design depends on (M2 daily series) has been run against a real warehouse.
