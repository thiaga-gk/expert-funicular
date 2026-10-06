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
  max_normalized_p95_runtime_regression_pct: 5.0

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
  confirmed_origin_required_for_hard_kpi: true

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

A hard performance or reliability failure overrides monetary savings. Reliability is required but uses explicitly approved enterprise/technique SLO policy rather than invented universal percentages.

## 7. Realization classification

- `ratio >= 0.80` -> `REALIZED`.
- `0.50 <= ratio < 0.80` -> `UNDER_REALIZED`.
- `< 0.50` -> `UNDER_REALIZED + REOPTIMIZATION_RECOMMENDED` when guardrails still pass.
- hard validation failure -> `VALIDATION_FAILED` regardless of savings.

Negative realization is preserved.

## 8. Modified application

`MODIFIED` is eligible for realization. Validate and measure the actual state, re-estimate expected economics for the actual applied target, and regenerate downstream recommendations from that actual state when dependencies require it.

## 9. Attribution

Strict product-attributed KPIs require confirmed causal origin in v1. Inferred/unknown origin may still support validated observed-value reporting.

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
