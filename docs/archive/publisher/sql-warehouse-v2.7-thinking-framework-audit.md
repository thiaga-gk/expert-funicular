# SQL Warehouse Publisher v2.7.0 — Optimization Thinking Framework Audit

> **Archived.** Point-in-time audit of publisher 2.7.0. It is not maintained; version numbers and findings reflect that release.

**Status:** Design audit; no runtime behavior changed by this document  
**Skill framework:** Databricks Publisher Skills v1.1.0 / Optimization Thinking Framework v1.0  
**Publisher:** SQL Warehouse Optimizer v2.7.0

## Executive conclusion

SQL Warehouse v2.7.0 is a strong **read-only testing baseline** and already implements much of the formal framework implicitly. It is suitable for the next step of live source validation because it cannot mutate warehouse configuration and it already has deterministic policy, current-epoch evidence, canary states, stateful scenario economics, invalidation, two reviewer gates, and fail-closed supported-type behavior.

It is **not yet fully framework-complete** because optimization constraints are distributed across thresholds/blockers/canary prose rather than represented as a first-class deterministic policy/evaluation model. Before recommendations become an operational approval/execution product or before broad GA, promote constraints and validation criteria into explicit internal contracts.

The locked Reporter JSON Contract v1.0.0 does not need to change for the first live test.

## Framework assessment

| Axis | v2.7.0 assessment | Evidence in current design | Gap / next action |
|---|---|---|---|
| F1 First Principles | Strong | DBU + AWS cost separation; workload/query/event/billing evidence; configuration epochs; workload pressure and idle capacity signals | Make the causal system model an explicit artifact for future maintenance |
| F2 Causal Mechanism | Good / implicit | Each optimizer rule has deterministic evidence + rationale; spill→size-up, low pressure→size-down, queue saturation→max-up, idle gaps→auto-stop | Formalize per-technique observation→mechanism→intervention→falsifier records |
| F3 Constraints First | Partial | Guardrail thresholds, blockers, `TEST_REQUIRED`, supported-type gate, compatibility checks | Main design gap: no first-class `OptimizationPolicy` / `ConstraintEvaluation`; some cost-down changes are `ACTIONABLE` despite post-change performance effects needing empirical proof |
| F4 Counterfactual | Strong | Independent candidate estimates, DBU/AWS separation, migration counterfactuals, low/base/high ranges | Downsize economics use runtime elasticity assumptions; these estimates are not explicitly constraint-qualified |
| F5 Second-Order Effects | Very strong | Dependencies/invalidates, `PENDING_REEVALUATION`, stateful bundle estimator, migration as separate branch, remeasure/regenerate | Add explicit new-bottleneck/constraint re-evaluation to the policy model, not only execution instructions |
| F6 Validation & Learning | Good / incomplete | Canary gates for Photon/size/migration, success-metric names, remeasure 7d, realization contract | Validation contract lacks numeric pass/fail thresholds and predeclared rollback triggers; realization ingestion/calibration remains future work |

## What v2.7.0 already gets right

### 1. It does not optimize by knob inventory alone

The analyzer/optimizer uses causal proxies for real resource behavior:

- spill frequency/severity for compute pressure;
- queue frequency/wait and observed ceiling saturation for concurrency pressure;
- time at minimum/maximum capacity for baseline scaling behavior;
- idle-gap evidence for auto-stop opportunity;
- billing feature evidence for Photon observed state;
- DBU/AWS resource/cost evidence for counterfactual economics.

This is consistent with first-principles design even though the causal model is not stored as a separate artifact.

### 2. Cost-down recommendations are not treated as risk-free

The most obvious behavior-changing interventions already use canary semantics:

- Photon enablement -> `TEST_REQUIRED`;
- size down -> `TEST_REQUIRED`;
- warehouse-type migration -> `TEST_REQUIRED` and compatibility-gated.

### 3. Second-order behavior is one of the strongest parts of the product

Examples:

- Photon change invalidates size/max/min/auto-stop;
- size change invalidates max/min/auto-stop;
- max change invalidates min;
- min change invalidates auto-stop;
- downstream steps become `PENDING_REEVALUATION`;
- scenario economics mutate evolving DBU/AWS/resource state instead of summing candidate savings;
- migration is a mutually exclusive branch and triggers re-analysis.

This is exactly the behavior required by F5.

### 4. The economics model is already counterfactual rather than purely historical

The product distinguishes:

`ACTUAL -> FORWARD BASELINE -> CANDIDATE COUNTERFACTUAL -> STATEFUL SCENARIO -> PACKAGE ENVELOPE -> REALIZATION`.

That should remain unchanged.

## Design changes recommended before operational/GA use

### Change A — Add an explicit deterministic optimization policy

Introduce an internal immutable policy contract, for example:

```python
@dataclass(frozen=True)
class OptimizationPolicy:
    version: str
    objective_mode: str
    primary_objective: str
    constraints: tuple[ConstraintSpec, ...]
    scenario_selection_policy: str
```

The policy should make the current product posture explicit rather than distributing it across constants and prose.

The default objective should be lexicographic:

1. supported type / compatibility / safety;
2. hard performance and reliability constraints;
3. evidence sufficiency;
4. primary objective (cost reduction or remediation of performance pressure);
5. operational simplicity / risk.

**Do not hard-code new numeric constraint tolerances without explicit approval.** In particular, if the product requirement is strict runtime non-regression, encode that exact policy rather than infer a tolerance.

### Change B — Add explicit constraint evaluation before optimizer decisions

Recommended internal contract:

```python
@dataclass(frozen=True)
class ConstraintEvaluation:
    constraint_id: str
    constraint_type: str       # HARD / VALIDATION_REQUIRED / SOFT
    state: str                 # SATISFIED / VIOLATED / UNKNOWN / PENDING_VALIDATION
    evidence_refs: tuple[str, ...]
    baseline_value: float | None
    proposed_value: float | None
    reason_code: str
```

Recommended pipeline:

```text
Analyzer
  -> ConstraintEvaluator
  -> Optimizer
  -> Estimator
  -> Reviewer 1
  -> Scenario Planner
  -> Stateful Bundle Estimator
  -> ScenarioConstraintEvaluator
  -> Reviewer 2
```

This can stay inside the single `publisher.py`; it does not require a module explosion.

### Change C — Revisit status for capacity/idle reductions that can create post-change latency

Current behavior:

- `min_clusters` reduction can be `ACTIONABLE`;
- `auto_stop_minutes` reduction can be `ACTIONABLE`;
- lower `max_clusters` ceiling can be `ACTIONABLE` as a guardrail.

From a constraints-first perspective:

- reducing min clusters can increase warm-capacity/scale-up latency;
- lowering auto-stop can increase start frequency and cold-start latency;
- lowering max clusters can expose a future burst to a lower concurrency ceiling.

Pre-change evidence lowers risk but does not necessarily prove the post-change constraint.

Recommended review:

- **min cluster reduction:** default to `TEST_REQUIRED` when runtime/queue non-regression is a hard constraint;
- **auto-stop reduction:** default to `TEST_REQUIRED` when lowering the threshold materially changes restart frequency;
- **max cluster reduction:** consider `REVIEW` or `TEST_REQUIRED` based on the approved burst-capacity constraint; it has no direct modeled savings today.

This is a product-policy change and should be explicitly approved before code changes.

### Change D — Qualify downsize economics by the performance constraint

The current size estimator uses a downsize runtime multiplier range approximately:

- optimistic: `1.10`;
- base: `1.30`;
- conservative: `1.55`.

That is useful sensitivity analysis, but if **runtime non-regression is a hard constraint**, those assumed outcomes cannot simultaneously be interpreted as feasible accepted outcomes.

Recommendation:

- keep the low/base/high counterfactual as **potential pre-canary economics**;
- add an explicit constraint state of `PENDING_VALIDATION`;
- do not let scenario selection treat the economic range as constraint-satisfied before the canary;
- preserve `TEST_REQUIRED` and measure actual runtime/cost in the canary.

This does not require removing the estimator; it requires qualifying the estimate.

### Change E — Make canary success and rollback criteria explicit

The locked JSON v1.0 `ValidationGate` currently carries metric names such as p95/p99 execution, failure rate, queue wait, DBU cost and AWS cost, but not full numeric comparator/threshold rules or rollback triggers.

For the next internal implementation increment, define deterministic validation policy such as:

```text
metric
baseline definition
comparator
allowed delta / threshold
minimum sample
measurement window
pass/fail aggregation
rollback trigger
action after pass/fail
```

Do not invent the allowed deltas; derive/approve them as product policy.

The public Reporter JSON Contract can remain v1.0.0 for initial live testing. If these validation details need to become first-class public fields, evolve the public contract deliberately (for example v1.1.0) rather than silently changing v1.0.0.

### Change F — Move Plan A selection from hard-coded behavior into explicit policy

The current conservative product rule—Plan A / in-place optimization is always `RECOMMENDED`, migration remains an alternative—is acceptable and was previously approved.

Keep the behavior for now, but express it as a versioned policy such as:

```text
scenario_selection_policy = CONSERVATIVE_IN_PLACE_FIRST
```

This makes the recommendation policy auditable and allows a future version to select a migration only after explicit compatibility/performance/economics criteria are approved.

### Change G — Extend golden tests for constraints-first semantics

Add golden cases for:

1. cost-down candidate with hard constraint satisfied;
2. hard constraint known violated;
3. required constraint evidence unknown;
4. safe canary path -> `TEST_REQUIRED`;
5. canary failure -> rollback outcome;
6. causal falsifier prevents recommendation;
7. second-order new bottleneck -> re-analysis;
8. better-savings scenario rejected/not selected because hard constraint fails.

## What should *not* change now

Do not change these simply to adopt the framework:

- single `publisher.py` fast-iteration core;
- current-epoch evidence model;
- deterministic analyzer/optimizer authority;
- seven SQL Warehouse candidate domains;
- independent candidate economics;
- stateful scenario estimator;
- two reviewer gates;
- immutable package IDs;
- Reporter JSON Contract v1.0.0 for the first live validation;
- Workbook projection architecture;
- read-only/advisory posture;
- Plan A conservative selection behavior until its policy is explicitly reopened.

## Testing recommendation

### Good to do now with v2.7.0

Proceed with live **read-only** testing against a real Databricks region to validate:

- system-table availability and source joins;
- supported-type gate;
- epoch reconstruction;
- Photon inference;
- owner/query-source attribution;
- DBU/AWS baseline reconciliation;
- threshold behavior on representative warehouses;
- package/workbook correctness;
- candidate/scenario economics sanity.

Because v2.7.0 does not apply configuration changes automatically, these tests are appropriate before the constraint-model refactor.

### Before operational recommendation execution / GA

Implement Changes A, B, D, E and the constraint golden tests. Decide Change C explicitly. Change F is a maintainability/policy hardening improvement and can be done in the same increment.

## Overall judgment

**v2.7.0 is suitable as the first real-data validation baseline.**

It already embodies first principles, counterfactual economics and second-order thinking unusually well for an initial optimizer. The framework does not require a redesign. It requires making the existing safety intent **explicit, typed, versioned and testable as constraints**, then tightening validation semantics for interventions whose post-change performance cannot be proven from pre-change evidence.
