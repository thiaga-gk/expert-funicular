# Publisher Gates

## Gate 0 — Discovery / First Principles
Required:
- service identity and supported/excluded types;
- personas and scope;
- resource keys/lifecycle/GA-Beta-Preview distinctions;
- `discovery/system-model.yaml` with demand, state, pressure, performance/reliability, consumption, cost drivers, controllable variables, and candidate constraints.

Exit: service profile/system model approved; no unresolved High/Critical scope or first-principles ambiguity.

## Gate 1 — Source / Technique / Economics / Optimization Policy
Required:
- authoritative source contract;
- `catalog/optimization-policy.yaml` with lexicographic objective and constraints-first policy;
- technique catalog where every active technique contains F1-F6 records;
- dependency/second-order graph;
- economics contract with isolated and stateful counterfactual semantics.

Exit:
- every active technique has required evidence;
- every hard constraint has a source or approved safe-canary disposition;
- every required metric has a source;
- null/zero and signed-impact semantics are defined;
- every technique has a causal mechanism, counterfactual, second-order effects, validation/rollback plan;
- unresolved High/Critical items closed or explicitly deferred.

## Gate 2 — Contract
Required:
- domain dataclasses/enums;
- optimization-policy/constraint contracts;
- public reporter contract;
- deterministic hash inputs including policy/constraint-model versions when decision-affecting;
- workbook row grains;
- realization contract.

Exit: contracts approved; example package validates; deterministic ID inputs fixed; constraint/status semantics are unambiguous.

## Gate 3 — Implementation
Required:
- collector/normalizer/type gate/analyzers;
- deterministic constraint evaluation;
- optimizer/estimators/reviewers/scenarios;
- scenario constraint evaluation;
- reporter;
- TDD evidence.

Exit: unit/component/integration tests green, including constraint-edge tests.

## Gate 4 — Validation
Required:
- golden scenarios;
- synthetic fleet;
- deterministic rerun;
- contract/schema validation;
- workbook projection checks;
- unsupported-type test;
- hard-constraint violation/unknown/canary tests;
- causal falsifier tests where practical;
- cost double-count challenge;
- second-order invalidation challenge.

Exit: verifier has no unresolved High/Critical findings and `/code-review low` passes when available.

## Gate 5 — Release
Required:
- stale/legacy audit;
- docs/code/version alignment;
- F1-F6 artifact/runtime/test alignment;
- clean ZIP extraction test;
- live-source E2E evidence when available;
- release-evidence.yaml.

Exit: release may merge from worktree to the integration branch according to repo policy.
