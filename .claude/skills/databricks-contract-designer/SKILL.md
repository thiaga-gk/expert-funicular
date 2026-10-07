---
name: databricks-contract-designer
description: Lock the typed domain, optimization-policy/constraint, public JSON reporter, deterministic identity/hash, realization, and workbook contracts for a Databricks optimization publisher before implementation. Use this skill whenever approved discovery/source/technique/economics artifacts must become dataclasses, enums, package hierarchy, candidate/scenario contracts, constraint evaluation, causal traceability, evidence references, validation plans, nullability/range rules, JSON Schema, deterministic IDs, workbook row grains, or contract validation invariants. Trigger it before publisher coding and for any proposed public contract change.
---

# Databricks Contract Designer

Act as a contract architect. Do not implement optimizer algorithms while contracts are unresolved.

Read the sibling orchestrator artifact protocol and `references/optimization-thinking-framework.md`. Load all approved upstream artifacts. Read `references/generic-resource-contract.md`, `references/deterministic-identity.md`, and `references/workbook-projection.md`.

## Procedure

1. Confirm upstream artifacts are `APPROVED`; stop on semantic conflicts.
2. Define stable internal typed boundaries. Prefer frozen dataclasses/enums for normalized evidence and decisions; keep source-shaped telemetry dictionaries behind adapters when schema volatility makes strict typing counterproductive.
3. Make the formal optimization framework explicit in internal contracts. At minimum represent approved optimization policy, constraint specifications/evaluations, causal/technique identity, counterfactual status, second-order dependency/invalidation, and validation/rollback plan. Keep the representation minimal and deterministic.
4. Lock a generic resource-oriented public package. Do not inherit SQL-Warehouse-specific names into reusable contracts.
5. Specify every field: required/optional, nullability, enum/domain, numeric range, unit, semantic meaning, source/provenance, and cross-field invariants.
6. Define content-addressed deterministic IDs and exact hash inputs for regime/epoch, evidence, candidate, scenario, realization, and package. Include policy/constraint-model versions where they can change decisions.
7. Define candidate/status cardinality, scenario composition, execution states, confidence semantics, economics layers, constraint disposition, validation semantics, and realization lifecycle.
8. Define JSON Schema (Draft 2020-12 by default) for structural validation plus Python semantic validators for cross-field invariants.
9. Define the workbook strictly as a projection of packages. Default grains:
   - Executive Summary: portfolio aggregation
   - Resource Portfolio: one row per package
   - Scenario Plans: one row per scenario
   - Recommendation Registry: one row per candidate
10. Keep financial cells numeric where available; signed impact positive=savings and negative=cost increase; currency/impact type explicit; null unavailable is never zero.
11. Produce/update `contracts/domain-contract.yaml`, `contracts/reporter-contract.yaml`, `contracts/workbook-contract.yaml`, schema/example artifacts, and acceptance criteria.
12. Run `scripts/validate_contract_bundle.py`.

## Contract hierarchy default

`recommendation_package_id, contract_version, resource, current_config, workload_profile, cost_baseline, assessment, evidence, candidates, scenarios, recommended_plan, package_economics, realization, provenance`

Service-specific contracts may add constraint/causal/validation fields where useful, but must not rename the generic cross-service concepts without an approved reason. A locked existing service contract should not be broken merely to retrofit the generic form; evolve it deliberately by version.

## Reviewer principle

JSON Schema validates shape/types/enums. Semantic validators enforce cross-references, hashes, economics math, constraint state consistency, status semantics, scenario feasibility/envelope, validation/rollback requirements, and realization invariants. Do not turn the JSON Schema into an unreadable business-rule engine.

## Downstream consumer

The package is also the input of a Realizer, which measures applied changes and accepts exactly one package contract version. Keep identity, each step's applied target value, validation windows and the assumptions a counterfactual needs in the package, and treat a package contract change as a coordinated change of both products. The SQL Warehouse Realizer's contracts are in `docs/contracts/realizer/`.
