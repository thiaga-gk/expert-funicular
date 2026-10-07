---
name: databricks-publisher-orchestrator
description: Orchestrate the end-to-end design, build, validation, and release of deterministic read-only Databricks optimization publishers for services such as SQL Warehouses, job clusters, Lakeflow/DLT pipelines, all-purpose compute, or future Databricks resources. Use this skill whenever the user asks to create, adapt, standardize, or productionize a publisher.py-style optimizer, cost-optimization engine, recommendation package, or reusable Databricks optimization product—even if they only mention one stage such as sources, techniques, contracts, golden tests, or release hardening. Coordinate the specialist publisher skills, enforce artifact and approval gates, require the formal optimization thinking framework, preserve determinism/HITL/read-only boundaries, and never let implementation invent unresolved domain semantics.
---

# Databricks Publisher Orchestrator

Coordinate the publisher skill suite. Keep this skill procedural; delegate domain analysis to the specialist skills.

## Core operating rule

Treat approved artifacts as the cross-skill API. Do not allow later stages to infer or silently replace decisions already recorded in approved artifacts.

Read `references/artifact-protocol.md`, `references/gates.md`, and `references/optimization-thinking-framework.md` before starting or advancing a publisher. Read `references/agent-patterns.md` when subagents or advisors are available.

## Formal optimization framework

Every service must explicitly pass six reasoning checks before implementation:

1. **F1 First Principles** — define demand, resource state, performance/reliability drivers, consumption, cost, and controllable state.
2. **F2 Causal Mechanism** — connect evidence to mechanism to intervention to expected direct effect.
3. **F3 Constraints-First Optimization** — determine feasibility and hard/validation/soft constraints before optimizing savings or performance.
4. **F4 Counterfactual Economics/Outcome** — model the isolated post-intervention state honestly.
5. **F5 Second-Order Effects** — define dependencies, invalidations, feedback, new bottlenecks, mutually exclusive branches, and remeasurement.
6. **F6 Validation & Learning** — define canary/HITL, numeric success criteria where possible, rollback, realization, and regeneration conditions.

Do not accept a technique catalog that treats this framework as optional narrative.

## Workflow

1. Resolve the target Databricks service/resource and release label.
2. Create or verify a dedicated Git worktree/branch off the repository's integration branch for the release. Never mix releases in one worktree.
3. Initialize `artifacts/databricks/<service-id>/` with `scripts/init_service_workspace.py` if it does not exist.
4. Invoke `databricks-service-discovery` to produce the service profile **and first-principles system model**; approve the discovery gate.
5. In parallel when tooling permits, invoke:
   - `databricks-source-contract`
   - `databricks-technique-catalog`
   - `databricks-economics-model`
6. Require `catalog/optimization-policy.yaml` plus the technique causal/constraint records. Stop at the design gate until source, technique, economics, constraints, and second-order artifacts are approved and unresolved High/Critical questions are closed or explicitly deferred.
7. Invoke `databricks-contract-designer`. Lock internal/public/reporting contracts, including explicit constraint and validation semantics, before coding.
8. Invoke `databricks-publisher-builder`. Implement only from approved artifacts using TDD. Never auto-advance to another epic/service.
9. Invoke `databricks-publisher-validator` as an independent verifier. Loop builder ↔ verifier until unit/integration/golden/contract/synthetic and thinking-framework checks pass.
10. Run `/code-review low` when available. Treat unresolved High/Critical findings as release blockers.
11. Invoke `databricks-release-sealer` to perform stale-code/docs/version/security/clean-room audits and package release evidence.
12. Merge the release worktree back only after the completion gate and E2E evidence are present.
13. Ground the golden scenarios in live data once the service runs against a real account (F6). In the SQL Warehouse exemplar repo this is `databricks-golden-miner`, which proposes golden changes through its own approval gate; for another service, plan the same step but do not reuse the miner's SQL Warehouse signatures.

A publisher only predicts. Realized savings are measured after a change is applied by a separate Realizer; the SQL Warehouse one is described in `docs/product/realizer/prd.md`, `docs/architecture/realizer/` and `docs/contracts/realizer/`. Design a new service's package contract so a Realizer can consume it (stable identity, the applied target value per step, validation windows and the assumptions a counterfactual needs), and treat a Realizer for a new service as its own design effort once the publisher contract is locked.

## Required gates

Use the gate states from `references/gates.md`:

`DISCOVERY → SOURCE/TECHNIQUE/ECONOMICS/POLICY → CONTRACT → IMPLEMENTATION → VALIDATION → RELEASE`.

Never skip a gate because the implementation "looks obvious".

## Non-negotiable product invariants

- Read-only/advisory first release; no mutation/apply API unless explicitly reopened.
- HITL/canary for risky, uncertain, or performance-affecting changes.
- Same normalized inputs + policy/model/contract versions => identical decisions, IDs, confidence, constraint states, and economics.
- Deterministic analyzers/constraint evaluators/optimizers/estimators are authoritative. LLMs may research, challenge, explain, or draft artifacts but must not make runtime recommendation decisions.
- Source authority must be explicit (`AUTHORITATIVE`, `OBSERVED`, `DERIVED`, `COMPATIBILITY_ONLY`, `UNAVAILABLE`).
- Current configuration regime/epoch controls active decisions; historical regimes are calibration/backtest evidence only.
- **Constraints are evaluated before optimization value.** Known hard post-change violations block; unknown required constraints must not silently pass.
- Candidate economics are independent/non-additive. Scenario economics are stateful. Package economics are a scenario envelope. Realization is measured after application and never rewrites original package identity.
- Fail closed on unsupported/future resource types.
- Workbook/report projections contain no recommendation business logic.

## Artifact policy

Use `artifacts/databricks/<service-id>/` as the durable handoff location. Every specialist skill must update the artifact registry and write deterministic, reviewable files. Do not pass material decisions only in chat prose.

Minimum policy artifacts before contract lock:
- `discovery/service-profile.yaml`
- `discovery/system-model.yaml`
- `sources/source-contract.yaml`
- `catalog/optimization-policy.yaml`
- `catalog/technique-catalog.yaml`
- `catalog/dependency-graph.yaml`
- `economics/economics-contract.yaml`

## Parallelism

Parallelize independent research only. Do not parallelize contract-dependent implementation stages that can create divergent definitions.

Safe parallel set:
- source research
- technique/causal research
- economics/pricing research

Sequential after approval:
- optimization-policy/contract lock
- implementation
- validation
- release seal

## Stop conditions

Stop and surface the issue instead of guessing when:
- a required authoritative source cannot be identified;
- a hard constraint cannot be evaluated and no approved safe canary path exists;
- a cost model cannot separate known zero from unavailable;
- resource identity is ambiguous;
- a technique lacks a defensible causal mechanism;
- a technique lacks post-change constraint semantics;
- source retention/freshness makes current-regime decisions unreliable;
- an unsupported resource type would fall through an existing algorithm;
- candidate/scenario economics would double count;
- second-order invalidation is unresolved;
- a public contract change is required but has not been approved.

## Output

At orchestration completion, provide:
- versioned repo/worktree status;
- approved artifact/gate summary;
- optimization-framework coverage summary;
- test/golden/E2E evidence;
- known limitations;
- final release artifact path/hash;
- explicit next service/release action, without starting it automatically.
