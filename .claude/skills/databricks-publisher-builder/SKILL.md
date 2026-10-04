---
name: databricks-publisher-builder
description: Implement or extend a deterministic publisher.py-style Databricks optimization engine from approved service, source, technique, optimization-policy, economics, and contract artifacts. Use this skill whenever code must be written for collectors, normalization/config regimes, supported-type gating, analyzers, deterministic constraint evaluation, optimizers, modelers/estimators, reviewer gates, scenario planning, stateful bundle estimation, RecommendationPackage assembly, JSON/schema validation, workbook projection, CLI/replay/synthetic modes, or tests. Trigger it only after design/contract artifacts are approved; use it for implementation loops, not open-ended domain research.
---

# Databricks Publisher Builder

Act as the implementation builder. Build only from approved artifacts. If a required semantic decision is missing, stop and return it to the owning advisor skill.

Read the sibling orchestrator artifact protocol/gates and `references/optimization-thinking-framework.md`. Read `references/reference-architecture.md`, `references/implementation-rules.md`, and `references/tdd-loop.md`.

## Worktree and scope

- Work in a dedicated release worktree/branch off the repository's integration branch when Git is available.
- Keep the service/release scope fixed. Never auto-advance to another epic/service.
- Default to a single `publisher.py` during early validation when that is the approved product shape; structure it into clear internal sections/classes and split only by explicit design decision.
- Stable public contracts/reporters may remain separate modules when already locked; do not fragment the core optimization loop prematurely.

## Required runtime pipeline

`Collector → Normalizer → Supported-Type Gate → Analyzer → Constraint Evaluation → Deterministic Optimizer → Modeler/Estimator → Reviewer Gate 1 → Scenario Planner → Stateful Bundle Estimator → Scenario Constraint Evaluation → Reviewer Gate 2 → RecommendationPackage → semantic/hash validation → JSON Schema validation → Workbook projection`

Constraint evaluation may be a dedicated class or small pure deterministic functions. Prefer the simplest implementation that makes constraints explicit and testable.

## Implementation rules

1. Collector executes set-based source queries and returns source-shaped aggregates with provenance.
2. Normalizer produces stable typed evidence and current configuration regime.
3. Supported-type gate fails closed before any service-specific optimizer logic.
4. Analyzer observes facts/findings; it does not choose recommendations.
5. Constraint evaluation computes deterministic pre-change/proposed-state constraint states from the approved optimization policy. Unknown hard constraints never silently pass.
6. Optimizer applies approved deterministic technique rules only within allowed constraint disposition. It must preserve technique/causal identity and use `TEST_REQUIRED` when empirical validation is the approved way to prove a constraint.
7. Candidate estimator prices isolated counterfactual changes only; mark non-additive and carry declared second-order runtime/resource effects.
8. Reviewer Gate 1 rejects invalid candidate/config/evidence/constraint/economics/confidence/validation states.
9. Scenario planner creates ordered alternatives and dependency/invalidation behavior.
10. Bundle estimator applies state transitions sequentially and prevents double counting.
11. Scenario constraint evaluation verifies the composed proposed state remains feasible/testable; scenario economics do not overrule constraint failure.
12. Reviewer Gate 2 validates scenario cross-references, plan states, constraint feasibility, confidence propagation, economics, rollback/validation, and recommended-scenario invariants.
13. Assemble the locked public package directly; avoid transitional duplicate public-report models.
14. Validate deterministic hashes and schema before writing JSON.
15. Workbook/reporters consume locked package data only and contain no recommendation logic.
16. Preserve read-only API boundaries and safe logging.

## TDD loop

For each story: failing test → smallest implementation → unit test → constraint-edge test → integration test → golden scenario update/addition when semantics changed → verifier. Do not weaken tests to make code pass without an approved semantic change.

## Runtime modes

Provide, when appropriate:
- live collector mode;
- replay/evidence mode;
- synthetic/demo mode using the same downstream pipeline.

Synthetic/demo mode must not bypass analyzers, constraint evaluation, optimizers, reviewers, contracts, or validation plans.

## Completion evidence

Before handoff run compile, full tests, deterministic rerun, optimization-policy/constraint checks, contract/schema validation, synthetic fleet, workbook validation, and source E2E when credentials are available. Record exact commands/results.
