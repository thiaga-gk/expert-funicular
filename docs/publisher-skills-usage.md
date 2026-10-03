# Databricks Publisher Skills v1.1.0

Reusable Claude skill suite for deterministic Databricks optimization publishers.

## Start

Copy `.claude/skills/` into the target repo and invoke the orchestrator, for example:

> Use the Databricks publisher orchestrator to build a deterministic job-cluster compute optimization publisher. Start with discovery and stop at the first approval gate.

## Formal thinking framework

Every publisher must pass:

1. F1 First Principles
2. F2 Causal Mechanism
3. F3 Constraints-First Optimization
4. F4 Counterfactual Economics/Outcome
5. F5 Second-Order Effects
6. F6 Validation & Learning

The authoritative description is:
`.claude/skills/databricks-publisher-orchestrator/references/optimization-thinking-framework.md`.

## Expected lifecycle

`Discovery/System Model → Source Contract → Optimization Policy + Technique Catalog → Economics → Contract Lock → publisher.py → Independent Validation → Release Seal`

Independent research can run in parallel, but contract-dependent implementation stages should remain sequential.

## Fast-iteration code shape

Default to a single `publisher.py` for the core decision/runtime pipeline during early validation. Keep locked public contracts/reporters separate when that boundary is already useful. Do not prematurely split the optimization engine into many modules.

## Validate the skill package

```bash
python tools/validate_skill_suite.py
```

The suite also includes deterministic validators for source, optimization policy, technique, economics, contract, golden-manifest, and release artifacts.

## Reference exemplar

SQL Warehouse Publisher v2.7.0 remains the reference implementation. The suite reuses its architectural lessons but requires every new service to derive its own first-principles model, constraints, source evidence, causal techniques, and counterfactuals.
