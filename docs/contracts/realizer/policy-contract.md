# Realizer Policy Contract v1.0.0

> The Realizer v1.0.0 specification. The implementation is the `src/realizer/` package (Analyzer, Governor, Reconciler, Estimator, Reviewer, Packager, Reporter) behind the `realizer` command; its layout and the reorganization record are in [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) and [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

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

maturity:
  annualization_requires: REPRESENTATIVE
  mature_ttm_days: 365

economics:
  attributable_spend_pct_min: 95.0
  mandatory_price_coverage_pct: 100.0
  currency_consistency_pct: 100.0
  portfolio_overlap_count_max: 0

attribution:
  confirmed_origin_required_for_hard_kpi: true   # CONFIRMED or INFERRED origin

regeneration:
  remeasure_days_after_invalidating_change: 7

supersession:
  minimum_material_savings_delta_pct: 5.0
```

## 3. Namespace separation

Publisher pre-change realization assumptions such as conservative/base/optimistic factors are not Realizer outcome thresholds. They must never be conflated.

## 4. Historical policy reproducibility

Historical closed episodes retain the policy snapshot under which they were evaluated. Later policy changes create explicit PolicyDiff/re-evaluation events; they never silently rewrite history.

## 5. Readiness

All required readiness gates must pass: elapsed period, sample sufficiency, representative regime and sufficient evidence quality. Low-frequency workloads wait for representative execution rather than qualifying from calendar time alone.

## 6. Guardrails

A hard performance or reliability failure overrides monetary savings. The limits are the package's own: its cost profile's applied thresholds (`cost_profile.applied_thresholds`), with the Publisher's defaults for the values a profile does not set; the Realizer policy can only tighten the p95 limit. Each rule reads `PASS`, `FAIL` or `UNKNOWN`; the validation fails when any rule fails, and a rule without enough evidence is `UNKNOWN`, which keeps the measurement not ready (readiness check `guardrail_evidence`), never failed.

- Measured guard metrics (a live measurement's `performance.guards`, the canary guard metrics before and after the change) follow the Publisher canary guard's two-part rule: a metric fails only when it is above its safe level AND above the value before the change times (1 + the profile's `canary_p95_regression_pct`). The failure rate applies to every change (`UNKNOWN` below the Publisher's minimum query sample). An auto-stop change adds the cold-start rule: the share of queries that gained a cold start (restarts per query after minus before) must stay within the profile's `cold_start_added_share_max_pct` (Serverless has no budget). Any other change adds the queued share and, for a size change, the spill share (each with a minimum of affected queries; fewer pass), and the p95 execution time on the statement sources seen before and after (`UNKNOWN` below the Publisher's canary p95 sample).
- A caller-supplied observation states a p95 regression percentage (limit: the profile's `canary_p95_regression_pct`), a reliability verdict (`null` is `UNKNOWN`) and a correctness verdict (failed only by evidence of a failure).

A canary-type step (execution state `READY_FOR_CANARY`) stays provisional for the Publisher's 30-day guard window: its measurement is recorded with recognition `PROVISIONAL` and the hard disqualifier `CANARY_GUARD_WINDOW_OPEN`, so it is not claimable until the window ends.

## 7. Realization classification

When the denominator is a Publisher range (the applied step's candidate range, or the scenario's range for a scenario-scope application) whose low is positive, the annualized realized savings are classified against the range: a replay counterfactual measures the full mechanical effect of the change, the track of the published high, so comparing it with the base would always read high.

- at or above the range's low -> `REALIZED`; above 1.25 x the high -> `REALIZED`, detail `REALIZED_ABOVE_RANGE_REVIEW`, review required;
- below the low -> `UNDER_REALIZED`; below half the low -> `UNDER_REALIZED + REOPTIMIZATION_RECOMMENDED` when guardrails still pass;
- the ratio against the base is reported in every case.

The comparison is like for like: a live measurement with a modeled AWS price is compared with the total range, a DBU-only measurement with the DBU range. A denominator without a range (a re-estimate for the applied scope) keeps the ratio policy:

- `ratio >= 0.80` -> `REALIZED`.
- `0.50 <= ratio < 0.80` -> `UNDER_REALIZED`.
- `< 0.50` -> `UNDER_REALIZED + REOPTIMIZATION_RECOMMENDED` when guardrails still pass.

A hard validation failure is `VALIDATION_FAILED` regardless of savings. Negative realization is preserved.

## 8. Modified application

`MODIFIED` is eligible for realization. Validate and measure the actual state, re-estimate expected economics for the actual applied target, and regenerate downstream recommendations from that actual state when dependencies require it.

## 9. Attribution

Strict product-attributed KPIs require a qualified causal origin: `CONFIRMED` (an approvals file names the package, the knob and the applied value) or `INFERRED` (a measured change applied after the package was generated, to the knob its step recommends). `UNKNOWN`, `INDEPENDENT` and `NOT_ATTRIBUTABLE` origins may still support validated observed-value reporting. The policy setting `require_confirmed_origin_for_hard_kpi` set to false lifts the origin requirement.

## 10. Counterfactual

Allowed claim-eligible methods: paired canary, calibrated prior state, matched historical, controlled pre/post. `CORRELATED_ONLY` is diagnostic, not strict claimable attribution.

## 11. Financial quality

Financial-grade publication requires at least 95% attributable spend, 100% mandatory material price coverage, 100% currency consistency, reconciled corrections and zero portfolio overlap.

## 12. Corrections and drift

Corrections always append and reproject. Configuration target-field changes are material to the active interval. Workload/regime statistical thresholds remain Analyzer-owned.

## 13. Supersession

Savings-only package churn requires >=5% material improvement unless another trigger applies (invalid package, material risk/confidence/config/workload/policy/capability change, explicit reoptimization). New package generation alone does not stop valid existing realization.

## 14. Determinism

Identical ledger, source snapshots, PolicySnapshot and component versions produce identical semantic outputs and hashes.
