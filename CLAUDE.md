# CLAUDE.md — v2.7.0 execution rules

## Primary invariants
- `RecommendationPackage` under Reporter JSON Contract v1.1.0 is the public machine source of truth.
- Workbook Contract v1.2.0 is projection-only; never put optimization, estimation, confidence, ID/hash, or selection logic in Excel.
- Identical normalized evidence + policy/model/contract versions must produce identical decisions, IDs, confidence and economics.

## Supported scope
- Active types: `CLASSIC`, `PRO`, standard `SERVERLESS`.
- `SUPPORTED_WAREHOUSE_TYPES` is fail-closed; never route unknown/future warehouse types into existing algorithms.
- One candidate per active domain: channel guardrail, Photon, size, max clusters, min clusters, auto-stop, warehouse-type migration.
- Query rewrite, storage, schedule, Spot policy and statement-timeout tuning are out of scope.

## Source authority
- Authoritative config snapshots: `system.compute.warehouses`.
- Current epoch controls active recommendations; historical epochs are calibration/backtest only.
- Photon is observed from current-epoch billing `product_features.is_photon`; do not represent it as an authoritative config API field.
- Owner attribution: billing `identity_metadata.owned_by`.
- Query source: `system.query.history.query_source` + `client_application`.
- REST is read-only and limited to target resolution/validation and optional migration compatibility evidence.
- Preserve `(workspace_id, warehouse_id)` identity.

## Decision boundaries
Collector → Normalizer → Supported-Type Gate → Analyzer → Policy/Optimizer → Modeler/Estimator → Reviewer Gate 1 → Scenario Planner → Stateful Bundle Estimator → Reviewer Gate 2 → RecommendationPackage → JSON validation → Workbook projection.

Analyzer observes; optimizer decides; candidate estimator prices isolated changes; bundle estimator prices stateful scenarios. Do not merge these responsibilities.

## Economics
- Keep DBU and AWS components separate.
- Candidate economics are non-additive.
- Scenario economics are stateful.
- Package economics are a scenario envelope, not a probability interval.
- Unknown cost is unavailable/null, never zero or guessed.
- Workbook annual-impact fields are numeric and signed: positive = savings; negative = cost increase. Keep an explicit currency and impact type.

## Public identity and validation
Use the contract helpers for `EPOCH-*`, `EVID-*`, `CAND-*`, `SCN-*`, `REAL-*`, and `PKG-*` identities. Volatile presentation metadata must not alter deterministic IDs.

Before writing each package:
1. assemble the frozen `RecommendationPackage`;
2. set/recompute package identity and full package hash;
3. run semantic/cross-reference validation;
4. serialize canonically;
5. validate against the committed Draft 2020-12 schema;
6. only then write `packages/<workspace>__<warehouse>.json`.

## Workbook
- Exact detailed-sheet column order/groups live in `contracts/workbook_contract.py`; renderer must consume those declarations rather than own a second column list.
- Exactly four visible sheets: Executive Summary, Warehouse Portfolio, Scenario Plans, Recommendation Registry.
- No business formulas/macros/hidden sheets.
- Financial values numeric where available; no strings such as `Save USD ...` in economic cells.
- IDs stored as text; verbose audit JSON belongs at the far right.

## Security
- OAuth profiles only for live auth; reject static PAT profiles.
- Never log credentials/tokens, raw SQL statement text, or unnecessary sensitive config values.
- No mutation/apply API in Phase 1.

## Change loop
1. Make the smallest layer-local change.
2. `python -m compileall -q publisher.py contracts reporters tests`
3. `python -m unittest discover -s tests -v`
4. regenerate the 15-warehouse demo;
5. validate every package against semantic + JSON Schema rules;
6. validate `workbook.xlsx` against exact sheet/column/grain rules and numeric economic semantics;
7. scan the repo for stale version/legacy/deferred references before release packaging.

## Reusable publisher skills
- For a new Databricks service publisher, start with `.claude/skills/databricks-publisher-orchestrator/SKILL.md`; do not clone/search-replace SQL Warehouse semantics.
- Use the specialist skills for discovery, source contract, technique catalog, economics, contract lock, builder, independent validator, and release sealer.
- Approved artifacts under `artifacts/databricks/<service-id>/` are the cross-skill API. Upstream semantic changes invalidate dependent approvals.
- Follow Skill Creator methodology: keep `SKILL.md` focused, load detailed references progressively, prefer deterministic scripts for repeatable validation, maintain trigger + functional evals, and validate the suite with `python tools/validate_skill_suite.py`.
- The generic reusable reporter contract uses `resource`; service-specific names belong in adapters/extensions.
