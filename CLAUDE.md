# inframate-v1

Deterministic, read-only Databricks SQL Warehouse optimizer: publisher v2.10.0, Reporter JSON Contract v2.0.0, Workbook Contract v1.4.0. Overview and CLI usage are in `README.md`; the doc map is `docs/README.md`.

## Commands
Python >= 3.10. Setup: `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt && pip install -e .`.
- Compile check: `python -m compileall -q src tests scripts examples`
- All tests: `python -m unittest discover -s tests -v`
- One test: `python -m unittest tests.unit.<capability>.<module>.<Class>.<test_name> -v` (or a module: `tests.unit.reporter.test_workbook_contract`)
- Demo (one warehouse per golden scenario): `python -m publisher --demo --output-dir ./demo_out`, or without an install `python scripts/run_demo.py [output_dir]`
- Workbook validator: `python scripts/validate_workbook.py ./demo_out`
- Contract example: `python scripts/validate_contracts.py`
- Skill suite: `python scripts/validate_skill_suite.py`
- Oracle parity (single-file tag vs working tree): `python scripts/compare_single_file.py`

## Change loop
1. Make the smallest layer-local change.
2. Compile check, then all tests.
3. Regenerate the demo and run the workbook validator. Packages are semantic- and schema-validated as they are written, so a failed validation aborts the demo.
4. `git grep` for stale version, legacy and deferred references before release packaging (exclude `.worktrees/`).
5. A decision-affecting change bumps the affected component versions in `src/publisher/config/versions.py` and regenerates goldens (`scripts/update_goldens.py`) and the contract example in the same commit (`docs/proposals/remediation-runbook.md`).
6. A new `Thresholds` field needs a row in `docs/reference/thresholds.md`, and a new blocker code a row in `docs/reference/blocker-codes.md`; `tests/test_docs.py` fails otherwise. Put the numbers and codes there and name them in prose elsewhere (`docs/README.md`).
7. Update the docs that describe the changed behavior in the same change. Each topic has one home (`docs/README.md`); `tests/test_docs.py` must pass.

## Invariants
These summarize `docs/product/prd.md` (scope and requirements) and `docs/architecture/context.md` (domain semantics); on a conflict the PRD governs scope (`docs/README.md`).
- IMPORTANT: `RecommendationPackage` (Reporter JSON Contract) is the public machine source of truth. The workbook is projection-only: never put optimization, estimation, confidence, ID/hash or selection logic in Excel.
- IMPORTANT: identical normalized evidence + policy/model/contract versions must produce identical decisions, IDs, confidence and economics.
- `SUPPORTED_WAREHOUSE_TYPES` (`CLASSIC`, `PRO`, standard `SERVERLESS`) is fail-closed: never route unknown or future types into existing algorithms.
- One candidate per active domain: size, max clusters, min clusters, auto-stop, warehouse-type migration. Photon and the warehouse channel are observation-only and never recommended (Photon has no Databricks on/off price, so savings would be guessed; a channel move has no priced effect). Query rewrite, storage, schedule, Spot and statement-timeout tuning are out of scope.
- Read-only: OAuth profiles only (reject static PATs), no mutation/apply API, and never log credentials, tokens, raw SQL text or unnecessary config values.

## Source authority
- Config snapshots: `system.compute.warehouses`. The current epoch controls recommendations; older epochs are calibration/backtest only, with three exceptions: a capacity cut is held when an older epoch of the same warehouse within the last 365 days had higher peak query concurrency than the current recent window; evidence a configuration change cannot affect carries across that change inside the last 30 days (demand and per-query evidence across auto-stop, max- and min-cluster changes; capacity queueing across auto-stop changes), never across a size or type change; and a canary rollback restores the previous epoch's value when a fail-fast guard trips on the new epoch's own evidence. Running time, idle time, cluster-minutes and billing never carry, so economics stay current-epoch. Otherwise older epochs never choose a change, its target or its economics.
- Photon comes from current-epoch billing `product_features.is_photon`, not a config API. Owner: billing `identity_metadata.owned_by`. Query source: `system.query.history` `query_source` + `client_application`.
- REST is read-only: target resolution/validation and optional migration compatibility evidence.
- Preserve `(workspace_id, warehouse_id)` identity.

## Pipeline boundaries
Analyzer (collector → normalizer → epochs → derived metrics → findings; fails closed on unsupported warehouse types) → Governor (eligibility, `Thresholds`, run constraints, migration compatibility) → Optimizer (one technique per active knob, cross-technique conflicts, candidate estimator) → Reviewer candidate gate → Packager (scenario planner, sequencing, stateful bundle estimator, package estimate, evidence registry, contract assembly) → Reviewer package gate → Reporter (packages, evidence, workbook, manifest). `publish()` in `src/publisher/publisher.py` calls each capability once, in that order, and holds no domain logic. The analyzer observes, the governor decides what may be considered, the optimizer decides, the candidate estimator prices isolated changes, the bundle estimator prices stateful scenarios. Do not merge these responsibilities (`docs/architecture/hld.md`).

## Economics
- Keep DBU and AWS components separate. Unknown cost is null/unavailable, never zero or guessed.
- Candidate economics are non-additive; scenario economics are stateful; package economics are a scenario envelope, not a probability interval.
- Workbook annual impact is numeric and signed (positive = savings, negative = cost increase) with explicit currency and impact type. No strings like `Save USD ...` in economic cells.

## Identity and package write order
- IMPORTANT: public IDs (`EPOCH-`, `EVID-`, `CAND-`, `SCN-`, `REAL-`, `PKG-`) come from `src/publisher/contracts/reporter_contract.py` (`make_*`, `RecommendationPackage.expected_package_id()`, 24 hex). `src/publisher/common/ids.py` still has legacy 16-hex `epoch_id`/`finding_id`/`recommendation_id`/`scenario_id`: do not use them for public IDs (`tests/unit/packager/test_locked_reporter_integration.py` pins that packages ignore them).
- Volatile presentation metadata must not enter the hash payload. Hashed lists keep order, so pre-sort set-like data.
- Write order (`_build_locked_recommendation_package` in `src/publisher/packager/packager.py`, `package_locked_contract` in `src/publisher/reporter/json_reporter.py`): assemble the frozen package → set package ID and full hash → semantic `validate()` → schema validation (committed Draft 2020-12 schema in `schema/`) → `json.dumps(sort_keys=True)` → write `packages/<workspace>__<warehouse>.json`.

## Replay evidence
`<output-dir>/evidence.json` is a versioned envelope (`evidence_format_version` 1.0.0) checked against `schema/evidence.schema.json` on write and on `--from-evidence` load; other versions and bare lists are rejected, with no legacy shims. It holds owner emails and tags, so treat it as sensitive. A format change updates the dataclass, schema, version, `docs/contracts/evidence-format.md` and the `tests/golden/` fixtures together.

## Workbook
Column order and groups live only in `src/publisher/contracts/workbook_contract.py`; the renderer in `src/publisher/reporter/workbook_reporter.py` consumes them and keeps no second list. Exactly four visible sheets (Executive Summary, Warehouse Portfolio, Scenario Plans, Recommendation Registry), no formulas/macros/hidden sheets, IDs as text, audit JSON at the far right.

## Tests
Unittest only, no pytest. Golden cases live in `tests/golden/cases/<name>/` (`case.yaml` with the `expect` block, `source/` evidence or collector snapshots, `expected/` canonical output generated by `scripts/update_goldens.py`); the assertion DSL is documented in the `tests/golden/test_golden_publisher.py` docstring. Adding a case needs a folder with `case.yaml` and `source/`, then `expected/` from `python scripts/update_goldens.py --case <name> --approved-change "<why>"`.
`tests/test_docs.py` guards the docs (links, contract versions, cited headings). Which document owns which topic, the precedence rule and the writing rules are in `docs/README.md`.

## Reusable publisher skills
For a new Databricks service publisher, start at `.claude/skills/databricks-publisher-orchestrator/SKILL.md` and do not clone or search-replace SQL Warehouse semantics. Approved artifacts under `artifacts/databricks/<service-id>/` (created on demand by the orchestrator) are the cross-skill API; an upstream semantic change invalidates dependent approvals. The generic reporter contract uses `resource`; service-specific names belong in adapters. Details: `docs/skills.md`.
