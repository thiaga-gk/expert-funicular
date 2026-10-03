# Technique Design

Every active technique must be a causal intervention, not merely a tunable API field.

## Required base fields
- `technique_id`, `title`, `domain`, `knob`, `direction`
- applicable resource types
- required/optional evidence
- decision rule with explicit thresholds/formula references
- status outcomes and blocker codes
- risk, effort, confidence inputs
- canary/HITL requirement
- economics mechanism (`DIRECT_COST`, `CAPACITY_GUARDRAIL`, `MIGRATION`, `PERFORMANCE_INVESTMENT`, `NONE`)
- top-level dependencies/invalidates for runtime convenience
- source readiness dependencies

## Required formal thinking sections

### `first_principles`
- `problem`
- `driver_changed`
- `objective`

### `causal_mechanism`
- `observation`
- `hypothesis`
- `intervention`
- `expected_direct_effect`
- `falsifiers`

### `constraints`
- `hard`
- `validation_required`
- `soft`

Constraints apply to the proposed state. A current constraint violation may justify a performance intervention.

### `counterfactual`
- `model_ref`
- `changed_state`
- `uncertainty`

### `second_order_effects`
- `dependencies`
- `invalidates`
- `may_increase`
- `remeasure_required`

The top-level `dependencies` and `invalidates` must match these fields.

### `validation_learning`
- `requires_canary`
- `success_metrics`
- `rollback_triggers`
- `realization_metrics`

The top-level `requires_canary` must match this section.

## Constraint disposition
- hard post-change violation known -> `BLOCKED`
- hard-constraint evidence missing -> `INSUFFICIENT_EVIDENCE` unless approved safe canary -> `TEST_REQUIRED`
- validation required -> `TEST_REQUIRED`
- all hard constraints deterministically satisfied -> candidate may become `ACTIONABLE`

## Stable identity
Use stable catalog IDs; content-addressed candidate IDs belong to the contract/runtime layer.
