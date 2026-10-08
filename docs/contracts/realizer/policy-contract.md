# Realizer Policy Contract

> Policy version `realizer-policy-v1.5.0` (`POLICY_VERSION` in `src/realizer/config/settings.py`). The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

**Status:** LOCKED

## 1. Principle

Algorithms live in code. Business thresholds, evidence requirements, guardrails, maturity, attribution and materiality rules live in an immutable versioned `PSNAP-*` PolicySnapshot.

## 2. Initial defaults

```yaml
validation:
  minimum_days: 7
  minimum_queries: 100
  require_representative_regime: true
  max_normalized_p95_runtime_regression_pct: null   # the package's cost-profile limit; a value only tightens it

realization:
  realized_ratio_min: 0.80
  under_realized_ratio_min: 0.50
  review_above_realized_ratio: 2.0   # without a published range: realized and flagged for review above this ratio

maturity:
  annualization_requires: REPRESENTATIVE
  mature_ttm_days: 365

economics:
  attributable_spend_pct_min: 95.0
  mandatory_price_coverage_pct: 100.0
  currency_consistency_pct: 100.0
  portfolio_overlap_count_max: 0   # the caller's overlap count; ownership across lineages is decided from the ledgers

attribution:
  confirmed_origin_required_for_hard_kpi: true   # CONFIRMED or INFERRED origin

regeneration:
  remeasure_days_after_invalidating_change: 7

supersession:
  minimum_material_savings_delta_pct: 5.0
```

### Policy file settings

`realizer --policy <file>` takes a JSON object whose keys are the settings below (the names of `RealizerPolicy` in `src/realizer/governor/policy.py`); a key not listed is refused, and any setting left out keeps its default. The YAML above groups the same values under contract names; the last column says where each setting sits there. `tests/test_docs.py` fails when a setting or a default here differs from the code.

| Setting | Default | Valid values | Contract name above |
|---|---|---|---|
| `minimum_days` | `7` | integer >= 0 | `validation.minimum_days` |
| `minimum_queries` | `100` | integer >= 0 | `validation.minimum_queries` |
| `require_representative_regime` | `True` | true or false | `validation.require_representative_regime` |
| `max_normalized_p95_runtime_regression_pct` | `None` | number >= 0; left out, the package's cost-profile limit governs, and a value only tightens it | `validation.max_normalized_p95_runtime_regression_pct` |
| `realized_ratio_min` | `0.80` | number >= 0 and >= `under_realized_ratio_min` | `realization.realized_ratio_min` |
| `under_realized_ratio_min` | `0.50` | number >= 0 and <= `realized_ratio_min` | `realization.under_realized_ratio_min` |
| `review_above_realized_ratio` | `2.0` | number >= `realized_ratio_min` | `realization.review_above_realized_ratio` |
| `remeasure_days_after_invalidating_change` | `7` | integer >= 0 | `regeneration.remeasure_days_after_invalidating_change` |
| `minimum_attributable_spend_pct` | `95` | number from 0 to 100 | `economics.attributable_spend_pct_min` |
| `minimum_price_coverage_pct` | `100` | number from 0 to 100 | `economics.mandatory_price_coverage_pct` |
| `required_currency_consistency_pct` | `100` | number from 0 to 100 | `economics.currency_consistency_pct` |
| `minimum_material_savings_delta_pct` | `5` | number >= 0 | `supersession.minimum_material_savings_delta_pct` |
| `require_confirmed_origin_for_hard_kpi` | `True` | true or false | `attribution.confirmed_origin_required_for_hard_kpi` |

`policy_version` is the label of the policy in force (`POLICY_VERSION`), not a threshold; it is not listed.

## 3. Namespace separation

Publisher pre-change realization assumptions such as conservative/base/optimistic factors are not Realizer outcome thresholds. They must never be conflated.

## 4. Historical policy reproducibility

Historical closed episodes retain the policy snapshot under which they were evaluated. Later policy changes create explicit PolicyDiff/re-evaluation events; they never silently rewrite history.

When a run evaluates an active application episode under a policy whose snapshot differs from the one the episode was last evaluated under (its last validation, or its last PolicyDiff), the run records `POLICY_DIFF_RECORDED` on that episode: `previous_policy_snapshot_id`, `policy_snapshot_id`, the run's `observed_at_utc` and `changed_settings`, one `{field, previous, current}` per setting that changed (`null` when the previous policy's settings were not recorded in the output directory: the change is never guessed). The settings of every policy a validation or a PolicyDiff was recorded under are kept as `realization/policies/<PSNAP>.json`. The validations before the diff keep their snapshot; the validation that follows is evaluated under the new policy. Example: a run validated under a p95 limit of 5 percent and the next under 4 records the diff `max_normalized_p95_runtime_regression_pct` 5 to 4, and the next validation fails a 4.5 percent regression that had passed.

## 5. Readiness

All required readiness gates must pass: elapsed period, sample sufficiency, representative regime and sufficient evidence quality. Low-frequency workloads wait for representative execution rather than qualifying from calendar time alone. A regime that is not representative because the measured query volume moved outside the comparable band (section 7, workload shift) states the volume ratio and the reason `WORKLOAD_SHIFT` in its readiness check; evidence that is not ready is recorded nowhere in the ledger, so a run logs the failing checks and their reasons (`realizer.not_ready`).

## 6. Guardrails

A hard performance or reliability failure overrides monetary savings. The limits are the package's own: its cost profile's applied thresholds (`cost_profile.applied_thresholds`), with the Publisher's defaults for the values a profile does not set; the Realizer policy can only tighten the p95 limit. Each rule reads `PASS`, `FAIL` or `UNKNOWN`; the validation fails when any rule fails, and a rule without enough evidence is `UNKNOWN`, which keeps the measurement not ready (readiness check `guardrail_evidence`), never failed.

- Measured guard metrics (a live measurement's `performance.guards`, the canary guard metrics before and after the change) follow the Publisher canary guard's two-part rule: a metric fails only when it is above its safe level AND above the value before the change times (1 + the profile's `canary_p95_regression_pct`). The failure rate applies to every change (`UNKNOWN` below the Publisher's minimum query sample). An auto-stop change adds the cold-start rule: the share of queries that gained a cold start (restarts per query after minus before) must stay within the profile's `cold_start_added_share_max_pct` (Serverless has no budget). Any other change adds the queued share and, for a size change, the spill share (each with a minimum of affected queries; fewer pass), and the p95 execution time on the statement sources seen before and after (`UNKNOWN` below the Publisher's canary p95 sample).
- A caller-supplied observation states a p95 regression percentage (limit: the profile's `canary_p95_regression_pct`), a reliability verdict (`null` is `UNKNOWN`) and a correctness verdict (failed only by evidence of a failure).

A canary-type step (execution state `READY_FOR_CANARY`) stays provisional for the Publisher's 30-day guard window: its measurement is recorded with recognition `PROVISIONAL` and the hard disqualifier `CANARY_GUARD_WINDOW_OPEN`, so it is not claimable until the window ends.

## 7. Realization classification

When the denominator is a Publisher range (the applied step's candidate range, or the scenario's range for a scenario-scope application) whose low is positive, the annualized realized savings are classified against the range: a live counterfactual (the auto-stop replay, the size counterfactual) measures the full mechanical effect of the change, the track of the published high, so comparing it with the base would always read high.

- at or above the range's low -> `REALIZED`; above 1.25 x the high -> `REALIZED`, detail `REALIZED_ABOVE_RANGE_REVIEW`, review required;
- below the low -> `UNDER_REALIZED`; below half the low -> `UNDER_REALIZED + REOPTIMIZATION_RECOMMENDED` when guardrails still pass;
- the ratio against the base is reported in every case.

The comparison is like for like: a live measurement with a modeled AWS price is compared with the total range, a DBU-only measurement (or a caller-supplied observation that gives a DBU component and no AWS component) with the DBU range. A denominator without a range (a re-estimate for the applied scope) keeps the ratio policy:

- `ratio > 2.0` (`review_above_realized_ratio`) -> `REALIZED`, detail `REALIZED_ABOVE_RANGE_REVIEW`, review required: a realization more than twice the expectation is implausible enough for a person to look at, as one far above a published range is. A policy file may set it, never below `realized_ratio_min`.
- `ratio >= 0.80` -> `REALIZED`.
- `0.50 <= ratio < 0.80` -> `UNDER_REALIZED`.
- `< 0.50` -> `UNDER_REALIZED + REOPTIMIZATION_RECOMMENDED` when guardrails still pass.

A hard validation failure is `VALIDATION_FAILED` regardless of savings. Negative realization is preserved. A measurement billed in another currency than the package's is reported in its own currency and never compared with the package's range or base (no exchange rate is guessed): `MONITORING`, detail `CURRENCY_MISMATCH_UNCLASSIFIED`, no ratio, and the currency gate keeps it from being claimed (`CURRENCY_MISMATCH`).

**Workload shift.** When the measurement's query volume per day after the change is outside 0.75 to 1.25 of the pre-change window's (`measurement.volume_ratio`), the package's expectation, priced at the old workload, is not a fair yardstick, and savings are not proportional to volume (more traffic leaves fewer idle gaps for a shorter auto-stop to cut). The realization is reported, never classified: state `MONITORING`, detail `WORKLOAD_SHIFT`, `classification_basis` `INFORMATIONAL`, the realized saving, its run-rate and the ratio against the package's base reported, never `UNDER_REALIZED` and never a re-optimization; the denominator stays the package's and the claim follows the usual gates. Example: an auto-stop change from 30 to 10 minutes on a Pro warehouse (published 6,242.34 / 9,363.51 / 12,484.69 a year) whose query volume halves from the change day saves 479.53 in 28 days, 6,251.02 a year: `MONITORING`, `WORKLOAD_SHIFT`, claimable. At 1.93 x the volume it saves 7,277.97 a year; an expectation scaled by the volume (18,058.47) would have called that under-realized.

A performance fix (step role `PERFORMANCE`: a capacity add whose objectives are performance, priced as a cost increase) or a capacity guardrail (step role `CAPACITY`: a cut priced at zero) is not a saving. It is judged on the guard metrics before and after the change, with the package's cost-profile safe queue level and p95 regression percentage (`classification_basis` `PERFORMANCE_GUARDS`):

- `PERFORMANCE_VALIDATED`: the queued share fell to at most half its pre-change value or to the safe level, and p95 on the shared sources did not rise past previous x (1 + the regression percentage); detail `PERFORMANCE_VALIDATED_COST_ABOVE_MODEL`, review required, when the annualized cost increase exceeds 1.25 x the modeled increase (minus the candidate's savings base);
- `CAPACITY_VALIDATED`: the queued share after the change is at or below the safe level, or not above its value before; the verdict's evidence also states, from the running minutes by cluster count, the minutes at the new cap and the upper bound of what the old cap could have added (`old_cap_upper_bound`: those minutes times the clusters removed, priced and annualized), never a saving;
- otherwise `MONITORING` (`PERFORMANCE_NOT_IMPROVED`, `CAPACITY_QUEUEING_REGRESSED`) with re-optimization recommended; without guard rows `MONITORING` (`PERFORMANCE_UNVERIFIED`, `CAPACITY_UNVERIFIED`).

The cost change (`cost_change`) is reported signed (positive = cost went up): for a capacity add measured with the running minutes by cluster count, the cluster-minutes above the old cap priced at the post-change window's DBU cost per cluster-minute plus modeled AWS (basis `CLUSTER_LEVELS_ABOVE_OLD_CAP`, the total with an AWS price, else DBU; [source evidence contract](source-evidence-contract.md) section 18); else the billed change at the pre-change query mix of a live measurement (total, else DBU); else the caller's counterfactual minus observed cost. The billed change of the same component is reported next to it (`billed_annualized_cost_increase`) as its cross-check. It is never counted as realized savings, and attribution claims nothing (`NOT_A_SAVINGS_STEP`). Example: max clusters 2 to 5 priced at -15,628.48/yr; queueing 9.72 to 0.76 percent, p95 2,500 ms before and after; 3,795 cluster-minutes above the old cap in 28 days (832.83 DBU + 473.62 modeled AWS), +17,030.48/yr, billed +12,887.78/yr: `PERFORMANCE_VALIDATED` (17,030.48 is within 19,535.60).

A safety rollback (step role `SAFETY_ROLLBACK`: the Publisher restores a value whose canary guard tripped) is a safety action, not a saving: its validation runs, but no counterfactual or measurement is recorded and nothing is claimed, whatever costs the observation carries (`SAFETY_ACTION_APPLIED`).

## 8. Modified application

`MODIFIED` is eligible for realization. Validate and measure the actual state, re-estimate expected economics for the actual applied target, and regenerate downstream recommendations from that actual state when dependencies require it.

For auto-stop the re-estimate is the Publisher's own option grid in the package (`auto_stop_annual_cluster_hours_by_value`, the annual net cluster-hours of every value it evaluated): the step's published range per component, scaled by the applied value's net cluster-hours over the recommended value's (`denominator_method` `PACKAGE_OPTION_GRID`); a value between two grid values is interpolated linearly and a value outside the grid has no denominator (`MONITORING`). The realization is then classified against that range like an exact step's. Example: the package recommends 30 to 10 minutes (6,242.34 / 9,363.51 / 12,484.69 for 1,212.6 cluster-hours); the owner sets 15 (909.4 cluster-hours): 4,681.50 / 7,022.25 / 9,363.00. A caller's re-estimate (`expected_annual_savings_actual_scope`) takes precedence; other knobs have no re-estimate yet.

## 9. Attribution

Strict product-attributed KPIs require a qualified causal origin: `CONFIRMED` (an approvals file names the package, the knob and the applied value) or `INFERRED` (a measured change applied after the package was generated, to the knob its step recommends). `UNKNOWN`, `INDEPENDENT` and `NOT_ATTRIBUTABLE` origins may still support validated observed-value reporting. The policy setting `require_confirmed_origin_for_hard_kpi` set to false lifts the origin requirement.

A warehouse-type migration is never claimable, whatever its origin: its measurement carries the hard disqualifier `NO_REVERSE_MIGRATION_MODEL` (no model prices the query mix on the type the warehouse left), so it is reported as an observed delta, `OBSERVABLE_ONLY`, with nothing attributed ([`ledger-contract.md`](ledger-contract.md) section 17).

## 10. Counterfactual

Allowed claim-eligible methods: paired canary, calibrated prior state, matched historical, controlled pre/post. `CORRELATED_ONLY` is diagnostic, not strict claimable attribution; it is the method of a warehouse-type migration's observed delta, which is reported and never claimed.

## 11. Financial quality

Financial-grade publication requires at least 95% attributable spend, 100% mandatory material price coverage, 100% currency consistency, reconciled corrections and zero portfolio overlap: one owner per change across the lineages of a warehouse ([ledger contract](ledger-contract.md) section 18), and no overlap the caller states.

## 12. Corrections and drift

Corrections always append and reproject. Configuration target-field changes are material to the active interval. Workload/regime statistical thresholds remain Analyzer-owned.

## 13. Supersession

Savings-only package churn requires >=5% material improvement unless another trigger applies (invalid package, material risk/confidence/config/workload/policy/capability change, explicit reoptimization). New package generation alone does not stop valid existing realization.

On the run path ([ledger contract](ledger-contract.md) section 19): a newer package of the same epoch replaces a lineage (nothing was applied in between); a successor whose configuration change applied none of the lineage's steps replaces it (a configuration change); a successor whose change applied the lineage's own step replaces it only when its expected savings are at least `minimum_material_savings_delta_pct` above the lineage's (a lineage expecting 10,476.47 a year is replaced by a successor expecting 11,000.29 or more, such as 11,105.06, 6 percent more, never by one expecting 10,895.53, 4 percent more; an unpriced successor never replaces it). A replacement the caller names (`replacement_package_id`) is an explicit trigger. Supersession changes the source package's disposition only: a superseded lineage that owns an applied change keeps measuring it.

## 14. Determinism

Identical ledger, source snapshots, PolicySnapshot and component versions produce identical semantic outputs and hashes.
