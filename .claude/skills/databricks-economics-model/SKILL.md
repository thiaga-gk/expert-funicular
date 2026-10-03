---
name: databricks-economics-model
description: Design the deterministic counterfactual economics and cost-estimation contract for a Databricks optimization publisher. Use this skill whenever the publisher needs historical actual cost, forward no-change baseline, DBU/AWS/cloud-cost separation, pricing sources, candidate counterfactuals, low/base/high ranges, stateful scenario economics, package opportunity envelopes, cost-increase semantics, currency/null handling, negotiated-price strategy, post-change realized savings, or economics consistent with first-principles and second-order state changes. Trigger it before estimator implementation and whenever pricing or resource topology changes.
---

# Databricks Economics Model

Act as an economics specialist. Keep optimization decision/constraint policy separate from cost modeling, but consume the approved policy so economics never implies an infeasible recommendation is acceptable.

Read approved service/system-model/source/technique/optimization-policy artifacts. Read the sibling orchestrator's `references/optimization-thinking-framework.md`. Read `references/economics-semantics.md`.

## Procedure

1. Define authoritative historical actual cost sources and explicitly mark unavailable components.
2. Define the forward no-change baseline for the current resource/configuration regime. Historical actual and forward baseline are different concepts.
3. Trace first-principles cost drivers from resource state to billable consumption and price.
4. Define pricing registry/join semantics: DBU SKU, cloud infrastructure, temporal price, region, negotiated/effective price availability.
5. For every technique, define the isolated **F4 counterfactual** corresponding to its causal intervention, or explicitly state `UNAVAILABLE`/guardrail-only.
6. Ensure counterfactual state variables match the technique's declared `counterfactual.changed_state`; reject economics that assumes a different intervention.
7. Define state transition functions so scenario economics apply ordered changes to changing resource state rather than summing candidates. Carry F5 second-order invalidations and migrations into scenario semantics.
8. Define low/base/high ranges and what uncertainty they represent. Package envelope is not a probability interval.
9. Define cost increase semantics: signed impact may be negative; never relabel it as savings.
10. Define currency rules and null-vs-zero behavior.
11. Define realization measurement and annualization semantics without mutating the original package identity. Record calibration fields so F6 learning can improve future models without changing historical recommendations.
12. Write/update `economics/economics-contract.yaml` and run `scripts/validate_economics_contract.py`.

## Required economic layers

`ACTUAL → FORWARD_BASELINE → CANDIDATE_COUNTERFACTUAL → SCENARIO_COUNTERFACTUAL → PACKAGE_ENVELOPE → REALIZATION`

## Constraints-first rule

Economics quantify feasible/testable alternatives; they do not override constraints. A scenario with better savings but a violated hard constraint is not selectable. An uncertain constraint must remain visibly `PENDING_VALIDATION`/`TEST_REQUIRED` according to approved policy.

## Safety

- Candidate impacts are non-additive.
- Scenario economics are stateful and ordered.
- Unknown cost is null/unavailable, never zero.
- Do not reuse another service's capacity/DBU curve unless the service contract proves it applies.
- Do not create a favorable counterfactual by omitting a second-order runtime/resource effect already declared by the technique.
