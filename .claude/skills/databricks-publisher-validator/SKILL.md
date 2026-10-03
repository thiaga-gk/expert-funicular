---
name: databricks-publisher-validator
description: Independently verify and challenge a Databricks optimization publisher after or during implementation. Use this skill whenever code, causal algorithms, constraints-first policy, contracts, economics, golden scenarios, deterministic IDs, unsupported-type behavior, scenario sequencing, workbook projection, synthetic fleets, or source E2E behavior must be tested adversarially. Trigger it for pre-merge verification, regression design, golden dataset creation, clean determinism checks, and whenever the builder claims a publisher story/release is done. Prefer an independent verifier context/model from the builder when available.
---

# Databricks Publisher Validator

Act as an independent verifier, not the implementation author. Do not silently fix production code; produce reproducible findings/tests and return fixes to the builder unless explicitly asked to patch tests/tools owned by validation.

Read approved artifacts, the sibling orchestrator's `references/optimization-thinking-framework.md`, and `references/challenge-catalog.md`. Use `references/golden-scenario-patterns.md` when constructing test coverage.

## Framework challenge

Validate all six axes independently:

- **F1 First Principles:** does the system model explain the real cost/performance driver rather than just list knobs?
- **F2 Causality:** can the evidence support the claimed mechanism, and are falsifiers tested?
- **F3 Constraints First:** can an attractive recommendation bypass a hard or unknown required constraint?
- **F4 Counterfactual:** does candidate economics/outcome match the declared intervention and uncertainty?
- **F5 Second Order:** are dependencies, invalidations, new bottlenecks, migrations, and remeasurement handled?
- **F6 Validation/Learning:** are success thresholds, rollback, realization, and regeneration explicit and tested?

## Validation layers

1. **Contract**: schema, semantic invariants, IDs/hash recomputation, cross-references, nullability/ranges/enums, policy/constraint versions.
2. **Algorithm**: threshold edges, blockers, insufficient evidence, NO_CHANGE, canary states, unsupported types, causal falsifiers.
3. **Constraints**: known violation, unknown evidence, pending validation, safe canary path, post-change state, scenario feasibility.
4. **Economics**: actual vs baseline, DBU/cloud separation, signed impacts, counterfactual/intervention consistency, stateful bundles, no candidate summation, scenario envelope traceability.
5. **Determinism**: identical evidence/version => byte-equivalent semantic JSON and IDs; volatile metadata excluded from identity.
6. **Scenario plan**: dependencies, invalidations, pending re-evaluation, mutually exclusive migrations, second-order bottlenecks, recommended-scenario policy.
7. **Synthetic fleet**: 10–20 representative resources through the same publisher path.
8. **Workbook**: exact sheets/grains/columns, numeric economics, no business formulas, explicit unavailable values, package traceability.
9. **Security/read-only**: no write APIs, no PAT-only bypass when OAuth policy requires OAuth, safe logging.
10. **Live-source E2E**: when credentials exist, run against source systems and capture evidence; otherwise mark not executed rather than simulate success.

## Minimum golden scenarios

Include at least: underutilized/idle, resource constrained, concurrency constrained, newly changed config, sparse evidence, unsupported type, pricing unavailable, dependency invalidation, migration alternative, healthy/no-change, **hard-constraint violation**, **hard-constraint evidence unknown**, and **canary constraint success/failure with rollback**. Add service-specific cases.

## Severity

- Critical: can publish unsafe/wrong mutation, corrupt identity/economics, leak secret, or violate contract broadly.
- High: wrong recommendation/economics, hard-constraint bypass, unsupported fallthrough, second-order invalidation failure, determinism break.
- Medium: explainability, coverage, maintainability or non-blocking projection defect.
- Low: style/readability/minor test hygiene.

Release blocks on unresolved Critical/High.

## Output

Write validation evidence under `artifacts/.../tests/` or release evidence references. Findings must include severity, reproducible command/input, expected, actual, affected framework axis/layer, and recommended owner skill.
