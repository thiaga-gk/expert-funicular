# Optimization Thinking Framework v1.0

Use this framework for every Databricks optimization publisher. It is a design and runtime-governance method, not an LLM runtime dependency.

## F1 — First Principles

Start from the resource system, not from API knobs.

Define:
- the user/business outcome;
- the primary economic or performance objective;
- the physical/compute drivers of cost and performance;
- demand/workload inputs;
- controllable state/configuration;
- observable evidence;
- non-controllable environmental factors.

Produce a causal system model such as:

`demand -> resource state -> utilization/pressure -> runtime/SLA -> consumption -> total cost`.

A technique is invalid if it cannot name the first-principles driver it changes.

## F2 — Causal Mechanism

For every technique, record the complete causal chain:

`observation -> inferred mechanism -> intervention -> expected state change -> expected outcome`.

Separate correlation from mechanism. Evidence that a resource is expensive is not evidence that a specific knob caused the expense.

Every technique must state:
- observed signal(s);
- causal hypothesis;
- intervention;
- expected direct effect;
- assumptions;
- falsifiers / evidence that would invalidate the hypothesis.

## F3 — Constraints-First Optimization

Do not optimize savings first. Determine feasibility first.

The default decision order is lexicographic:

1. hard constraints / safety / compatibility;
2. evidence sufficiency and supported-type feasibility;
3. required performance/reliability/service-level guardrails;
4. primary optimization objective (usually minimize forward annual total cost, or resolve performance pressure);
5. secondary objectives such as operational effort, stability, or simplicity.

Classify constraints:
- `HARD`: must be satisfied; a known post-change violation blocks the technique/scenario;
- `VALIDATION_REQUIRED`: cannot be proven pre-change but may be tested safely by canary/HITL;
- `SOFT`: influences ranking/explanation but does not by itself block;
- `INFORMATIONAL`: recorded for context only.

Constraint states:
- `SATISFIED`
- `VIOLATED`
- `UNKNOWN`
- `PENDING_VALIDATION`
- `NOT_APPLICABLE`

Default mapping:
- known hard post-change violation -> `BLOCKED`;
- missing evidence required to judge a hard constraint -> `INSUFFICIENT_EVIDENCE`, unless an approved safe canary converts it to `TEST_REQUIRED`;
- constraints requiring empirical proof -> `TEST_REQUIRED`;
- all required constraints deterministically satisfied -> may become `ACTIONABLE` if other policy rules pass.

Constraints must be evaluated on the proposed/recommended state, not only on the current state. A current SLA violation may be the reason for a performance-increasing recommendation.

## F4 — Counterfactual Economics / Outcome

Estimate the state if the intervention were applied and all other state were held constant where valid.

For every technique define:
- baseline state;
- changed state variables;
- counterfactual model;
- low/base/high or other approved uncertainty representation;
- cost/performance components affected;
- known-unavailable components;
- confidence / calibration evidence.

Candidate counterfactuals are standalone and non-additive.

Do not convert unknown to zero. Do not label a negative economic impact as savings.

## F5 — Second-Order Effects

Ask: "what changes next because this intervention changes the system?"

For every intervention identify:
- dependent recommendations invalidated;
- state variables altered;
- new bottlenecks that may emerge;
- feedback loops;
- operational or compatibility externalities;
- mutually exclusive branches/migrations;
- remeasurement/re-analysis point.

Scenario economics must apply changes to evolving state. Never sum independent candidate savings.

An invalidating upstream change must place affected downstream decisions into `PENDING_REEVALUATION` until fresh evidence is collected.

## F6 — Validation and Learning

A recommendation is incomplete without a validation contract.

Define:
- canary/HITL requirement;
- baseline window;
- post-change measurement window;
- exact success metrics and thresholds where possible;
- constraint pass/fail criteria;
- rollback triggers and rollback action;
- remeasurement period;
- realization/economic measurement;
- learning/calibration fields;
- conditions that regenerate recommendations.

Observed realization may calibrate future models but never rewrites the identity or historical semantics of the original recommendation package.

## Required technique record

Every active technique must contain these six sections:
- `first_principles`
- `causal_mechanism`
- `constraints`
- `counterfactual`
- `second_order_effects`
- `validation_learning`

## Required service optimization policy

Every service must define an `optimization-policy.yaml` before contract lock. At minimum:

- primary objective;
- objective ordering / lexicographic policy;
- hard constraints;
- validation-required constraints;
- soft constraints;
- default unknown-evidence disposition;
- scenario selection policy;
- rollback policy;
- realization policy.

## Runtime implication

The authoritative runtime remains deterministic. The framework should result in explicit typed policy/evidence/constraint evaluation, not free-form runtime LLM reasoning.

Recommended runtime shape:

`Collector -> Normalizer -> Supported-Type Gate -> Analyzer -> Constraint Evaluation -> Optimizer -> Candidate Estimator -> Reviewer 1 -> Scenario Planner -> Stateful Bundle Estimator -> Scenario Constraint Evaluation -> Reviewer 2 -> Package`.

Constraint evaluation may be implemented as a dedicated class or pure functions; do not create unnecessary abstraction when a simple deterministic structure suffices.
