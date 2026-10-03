---
name: databricks-technique-catalog
description: Discover, challenge, and formalize the optimization technique catalog and constraints-first optimization policy for a Databricks service. Use this skill whenever the publisher needs to decide which tunable knobs/techniques are in scope, derive them from first principles, assign stable technique IDs, define causal mechanisms, hard/validation/soft constraints, deterministic decision rules, evidence requirements, blockers, risk/effort, counterfactuals, second-order dependencies/invalidations, canary requirements, standalone migration alternatives, or coverage gaps. Trigger it before optimizer implementation and whenever a new Databricks capability or optimization idea is introduced.
---

# Databricks Technique Catalog

Act as a Challenger + optimization-policy advisor. Do not write production optimizer code.

Read the approved service profile, system model, and source contract. Read the sibling orchestrator's `references/optimization-thinking-framework.md`. Read `references/technique-design.md`.

## Procedure

1. Enumerate optimization opportunities from the first-principles system model by domain: compute/configuration, storage, query, schedule, topology/migration, governance—then keep only domains explicitly in product scope.
2. Distinguish **optimization techniques** from mere API knobs and from **guardrail techniques** that improve stability/governance without claiming savings.
3. Define `catalog/optimization-policy.yaml` first: lexicographic objective ordering, hard/validation/soft constraints, unknown-evidence disposition, scenario-selection policy, rollback policy, and realization policy.
4. For each technique, assign a stable catalog `technique_id` independent of resource/run.
5. Apply the formal six-part framework to every active technique:
   - `first_principles`
   - `causal_mechanism`
   - `constraints`
   - `counterfactual`
   - `second_order_effects`
   - `validation_learning`
6. Define required/optional evidence, deterministic decision rule, blockers, confidence inputs, risk, effort, canary/HITL requirements, and economics mechanism.
7. Define mutual exclusivity, prerequisites, dependencies, invalidations, new-bottleneck hypotheses, and alternative branches.
8. Challenge the catalog for missing cost-saving/performance techniques, accidental duplication, proxy-causality errors, and techniques that optimize savings by silently violating a constraint.
9. Reject/defer techniques whose causal mechanism is not defensible, whose required evidence is unavailable, whose post-change hard constraints cannot be evaluated safely, or whose economics cannot be represented honestly.
10. Write/update `catalog/optimization-policy.yaml`, `catalog/technique-catalog.yaml`, and `catalog/dependency-graph.yaml`.
11. Run `scripts/validate_optimization_policy.py` and `scripts/validate_technique_catalog.py`.

## Constraints-first decision semantics

Evaluate the proposed state, not merely the current state.

- known hard post-change constraint violation -> `BLOCKED`;
- required hard-constraint evidence missing -> `INSUFFICIENT_EVIDENCE`, unless approved safe canary -> `TEST_REQUIRED`;
- empirical constraint proof required -> `TEST_REQUIRED`;
- required constraints deterministically satisfied -> may become `ACTIONABLE` if the technique decision rule passes.

A current constraint violation can be the reason for a performance-increasing intervention; do not block an intervention simply because the current state is unhealthy.

## Catalog rule

Every active decision domain must produce exactly one candidate decision per resource (`ACTIONABLE`, `TEST_REQUIRED`, `REVIEW`, `BLOCKED`, `NO_CHANGE`, `NOT_APPLICABLE`, or `INSUFFICIENT_EVIDENCE`) unless the approved contract defines a different cardinality.

## Never

- Sum candidate savings to produce package savings.
- Hide a migration as an in-place knob.
- Turn insufficient evidence into `NO_CHANGE`.
- Reuse another service's thresholds without re-deriving the causal/constraint semantics.
- Recommend an economically attractive change whose hard post-change constraints are known to fail.
