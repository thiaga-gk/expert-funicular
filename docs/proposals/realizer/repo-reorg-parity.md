# Realizer reorganization: parity record

Status: **Complete** (Phases 0-11). Phases 0-10 extracted the single-file Realizer into `src/realizer`, with the single-file implementation as the behavioral oracle; Phase 11 retired the oracle on fixture parity. The oracle is the git tag `realizer-reorg-oracle-1.0.0` (last single-file commit `f5ab82f`) plus the stored canonical outputs; no in-tree copy remains. Design, phases and implementation rules: [`repo-reorg-design.md`](../../architecture/realizer/repo-reorg-design.md) sections 21.4, 22, 24 and 27.

The sections for Phases 0-10 below are the record as written at the time: the oracle paths they cite (`legacy/realizer_single_file/...`), the facade re-exports, `scripts/compare_realizer_single_file.py` and `tests/unit/realizer/parity.py` no longer exist; Phase 11 says what replaced them.

The behaviors the phases kept from the single-file release for parity are listed as current state, each with why it is kept, in [`known-limitations.md`](../../reference/realizer/known-limitations.md); the "Known oracle behavior kept for parity" blocks below record when and how each was found and stay as written.

## Phase 0: what is frozen

| Item | Where |
|---|---|
| Oracle: the v1.0.0 single-file Realizer (`realizer.py`, its `contracts/`, `reporters/`, `schema/`, docs and examples), moved unchanged from the root `realizer/` folder so that no root-level `realizer/` shadows the `src/realizer` package | `legacy/realizer_single_file/` |
| Oracle loader: imports the oracle under the private package name `legacy_realizer_single_file`, aliasing its top-level `contracts`/`reporters` imports only while its own modules load, so neither name nor `realizer` leaks into `sys.modules` | `tests/regression/realizer/oracle.py` |
| Frozen Publisher package (PKG fixture) | `tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json`, byte-identical to the `source-package.json` of both committed examples and to the demo `publisher_out` package |
| Derived package for GR-019 (the recommended plan with the first step invalidating nothing and the second ready for canary) | `tests/fixtures/realizer/packages/GR-019-two-ready-steps.json` |
| Scenarios: the two committed example observations, every golden scenario that calls `realize()` (GR-001..GR-040 except the function-level GR-004, GR-035, GR-036, GR-039), a replay-through-generation case, a financial-quality-gate case and a reprocessed restatement | `tests/fixtures/realizer/observations/*.json` (41 scenarios) |
| Canonical outputs per scenario: RLE ledger, RPKG, every RSNAP, REVID evidence, RERUN regeneration request, per-run IDs with the legacy compatibility projection, and the workbook-extension projection | `tests/fixtures/realizer/expected/<scenario>/` |
| Golden scenario registry (test IDs repointed to the new test module) | `tests/fixtures/realizer/golden-registry.json` |
| Comparison runner and baseline regeneration | `scripts/compare_realizer_single_file.py`, `scripts/update_realizer_goldens.py` |
| Tests: the oracle's own suites (core, contract, GR-001..GR-040, workbook projection) on the PKG fixture, and the compatibility baseline (stored outputs, determinism, idempotency, committed examples) | `tests/regression/realizer/` |

## Phase 0: proof the oracle runs as shipped

- The oracle reproduces the committed examples byte for byte: `legacy/realizer_single_file/examples/realizer_jsons/exact/realization/` (`RPKG-9BCA3C3DFB12CC37AA7CA775`, `RSNAP-B4E0622B53B53930421A90E3`, `REVID-C82136407213010727CEAB98`, `RERUN-5EAFEBC878ED2364E53E171A`), `legacy/realizer_single_file/examples/realizer_jsons/modified/realization/` (`RSNAP-709E1EC13A10E423918D76AB`, `REVID-783EE1BC18D972E51EBFC4AE`, `RERUN-BE6D14C9D53D954C51BE6C47`) and `legacy/realizer_single_file/examples/realizer_demo/realization/`, through the CLI and through the baseline (`test_example_scenarios_match_committed_examples`).
- GR-001..GR-040 pass on the PKG fixture (40/40), as the snapshot reported.

## Phase 0: edits to the oracle

- One import: `from publisher import SIZE_ORDER` became `from publisher.config.defaults import SIZE_ORDER`. The single-file publisher module no longer exists; the list in `src/publisher/config/defaults.py` is identical to the single-file publisher's (2.7.0 through 2.10.0: `2X_SMALL` .. `5X_LARGE`), so the `enumerate()` size-rank mapping is unchanged.
- A header comment marks `realizer.py` as the frozen oracle. No realization logic changed.

## Phase 0: tests removed from the oracle

`test_publisher_v28_regression.py` and `legacy_test_publisher_v21_reference.py` tested the snapshot's own single-file publisher (`synthetic_demo_evidence`, `build_report`, `build_locked_recommendation_packages`, `PUBLISHER_VERSION`), not the Realizer. That publisher is not in this repository's history (the repository's own 2.8.0 publisher has a different API), so they were deleted as obsolete. The other suites now load the PKG fixture instead of building a package through it.

## Variable fields

The oracle reads no wall clock: ledger times, `as_of_utc`, IDs and lineage derive from the package and the observation, and it writes no paths into its files. Runs into different output directories, time zones and hash seeds produce identical canonical outputs (`tests/regression/realizer/test_subprocess_determinism.py` runs both engines in fresh processes under `PYTHONHASHSEED` 0 and 1 and `TZ` UTC and Asia/Kolkata), so no field needed normalization; the baseline still replaces the output directory with `<out>` should it ever appear. The workbook `.xlsx` is not in the baseline: the renderer needs the `artifact_tool` runtime and `RecommendationPackage` objects of the retired publisher, which the frozen JSON fixtures cannot rebuild; its test skips when `artifact_tool` is absent.

## Known oracle behavior kept for parity

Reprocessing a billing-restatement observation once more (GR-026's second observation) appends a second `BILLING_CORRECTION_RECORDED` entry whose `corrects_entry_id` is the restated measurement itself, and the snapshot's `correction_count` goes from 1 to 2; a further pass adds nothing. Every other scenario's last observation is idempotent. Under the implementation rules this is kept through the extraction (the `restatement-reprocessed` scenario pins it for the candidate) and corrected later as a separately reviewed change.

## Contract pinning

The PKG fixture is v2.8-era Publisher output (Reporter JSON Contract 1.0.0), and the oracle accepts only Reporter JSON Contract 1.0.0 (`validate_package_shape`). The current publisher emits a later contract version, so the oracle and its baseline are pinned to 1.0.0 packages and cannot consume a package the current publisher writes. Adapting the Realizer to the current contract is a semantic change, outside the behavior-preserving extraction.

## Phases 1 and 2: contracts, config and common

`src/realizer/` is a package (found by the existing `[tool.setuptools.packages.find] where = ["src"]`; no console script or version changed). Its engine `src/realizer/realizer.py` is a working copy of the oracle with the same behavior: the definitions below were removed from it and are imported from their new modules, and the CLI's inline loading and logging setup call the moved helpers. The oracle under `legacy/realizer_single_file/` is unchanged and stays the comparison reference. Later phases keep shrinking the working copy.

### Where the moved names live

Every name keeps its spelling, signature, content dicts, messages and values.

| Module | Names | From |
|---|---|---|
| `src/realizer/contracts/realization.py` | `CONTRACT_VERSION`, `LEDGER_CONTRACT_VERSION`, `HASH_HEX_LENGTH`, `ValidationState`, `RealizationState`, `MaturityState`, `AttributionState`, `AttributionClaimStatus`, `RecognitionStatus`, `CompatibilityStatus`, `EventType`, `_primitive`, `canonical_decimal`, `canonical_json`, `sha256_hex`, `content_id`, `_require`, `_validate_id`, `RealizationLedgerEntry`, `RealizationPackage` | `contracts/realizer_contract.py` |
| `src/realizer/contracts/realization.py` | `make_rpkg_id`, `make_evidence_id`, `make_app_id`, `make_val_id`, `make_measurement_id` | `realizer.py` |
| `src/realizer/contracts/analysis.py` (re-exported by `realization.py`) | `IntentState`, `OriginAttribution`, `EvidenceQuality` | `contracts/realizer_contract.py` |
| `src/realizer/contracts/reconciliation.py` (re-exported by `realization.py`) | `SourcePackageDisposition`, `ImplementationConformance`, `ApplicationState`, `PlanProgressState`, `RegenerationStatus` | `contracts/realizer_contract.py` |
| `src/realizer/contracts/realization_schema.py` | `realization_package_schema`, `ledger_entry_schema` | `contracts/realizer_schema.py` |
| `src/realizer/contracts/report.py` | `REALIZER_WORKBOOK_EXTENSION_VERSION`, `SHEET_REALIZATION_SUMMARY`, `SHEET_REALIZATION_TIMELINE`, `SHEET_NAMES`, `RealizerWorkbookProjection`, `_json`, `_money_float`, `project_summary`, `project_timeline`, `project_realizer_workbook` | `contracts/realizer_workbook_contract.py` |
| `src/realizer/contracts/request.py` | `RealizationRequest` (new; names the inputs `realize()` takes) | none |
| `src/realizer/config/settings.py` | `PRODUCT_VERSION`, `REALIZER_VERSION`, `PROJECTOR_VERSION`, `POLICY_VERSION`, `ANALYZER_GATEWAY_VERSION`, `MODELER_GATEWAY_VERSION`, `ESTIMATOR_GATEWAY_VERSION`, `ATTRIBUTION_VERSION`, `EXCLUSIVITY_VERSION`, `CONFIG_KNOBS`, `SIZE_ORDER` | `realizer.py` |
| `src/realizer/config/loader.py` | `validate_package_shape`; `load_package` and `load_policy_mapping` (the CLI's inline `load_json` calls for `--package` and `--policy`) | `realizer.py` |
| `src/realizer/common/logging.py` | `LOGGER`; `setup_logging` (the CLI's `logging.basicConfig` call) | `realizer.py` |
| `src/realizer/common/utils.py` | `utc_iso`, `decimal`, `money`, `ratio_string`, `load_json`, `write_json` | `realizer.py` |

Left in the working copy by these phases for the phases that own them: `make_cf_id` (estimator) and the matching, lifecycle, economics, attribution, ledger, snapshot, reporting and CLI functions. The policy, the analyzer functions and the demo moved in Phases 3 and 4 (below).

### Decisions

- **One public contract namespace.** The contract enums are grouped as design section 14 suggests (observation-side states in `analysis.py`, plan, application and lifecycle states in `reconciliation.py`), and `realization.py` re-exports them, so its `__all__` is the oracle contract's `__all__` plus the five ID builders and `rc.<Name>` works as before. The enum objects are not copied.
- **`make_cf_id` stays in the engine.** It hashes `config_hash(package_config(package))` (analyzer code) and `MODELER_GATEWAY_VERSION`, and contracts import nothing from config (as in the publisher). It moves with the estimator, which can then pass the old configuration hash in.
- **No domain logic in `common`.** `normalize_scalar` and `config_hash` encode the warehouse size and type vocabulary and the knob set, so they wait for the analyzer. `common/utils.py` imports `canonical_decimal` from the contracts for `ratio_string`; the contracts import nothing from `common` or `config`.
- **Policy defaults stay with `RealizerPolicy`.** It belongs in `governor/policy.py` (the role of the publisher's `Thresholds`), so the config layer loads only the override mapping: `RealizerPolicy.from_mapping(load_policy_mapping(path))`. The CLI keeps its load order (package, observation, policy).
- **No `common/errors.py` and no execution mode.** The Realizer raises only `ValueError`/`KeyError` and maps none of them to an exit status, and it has no execution modes; an empty module or an unused `RealizerError` would be stubs. Adopting an error type with an exit-status mapping is a separate CLI change.
- **Schema builders beside the contract.** `contracts/realization_schema.py` follows the publisher's `reporter_schema.py`. The committed schema files stay under `legacy/realizer_single_file/schema/`; a unit test pins the builders to their bytes. Validating against them on write is a separate change.
- **Logging unchanged.** `setup_logging` makes the same `basicConfig` call, including the literal `Z` after a local-time `asctime`.

### Verification

| Check | Result |
|---|---|
| `scripts/compare_realizer_single_file.py` (candidate `realizer.realizer`) | 41 scenarios: oracle = stored baseline = modular engine |
| Unnormalized output trees of every scenario, oracle against modular (`test_single_file_compatibility.ModularCompatibilityTests`): ledger JSONL, RPKG, every RSNAP, REVID, RERUN, plus the returned snapshots and the `realizer` log lines | byte-identical, 41/41 |
| Stored baseline, determinism across output directories, idempotent reprocessing and the pinned GR-026 restatement, run on each engine | pass on both |
| The oracle's own suites (core 7, contract 5, GR-001..GR-040, workbook projection 2), run on each engine | pass on both |
| Unit tests `tests/unit/realizer/{contracts,config,common}/`: public names and enum values, canonical helpers, validation messages, every stored RLE/RPKG/RSNAP round-tripping byte for byte, IDs against the oracle and the stored outputs, schema builders against the committed files, the report projection against every stored `report.json`, versions, size order, package gate, policy defaults and `from_mapping`, CLI output | pass |
| Test suite | 924 tests (819 before: plus 54 runs of the ported oracle suites on the modular engine, 7 modular compatibility tests and 44 unit tests; 4 skip as before), all pass |

The oracle's unused local `actor` in `legacy_compatibility` is kept in the working copy (pyflakes reports it); removing it is cleanup for the reporter phase.

## Phases 3 and 4: analyzer, collector and governor

The engine now runs `analyzer.run` and then `governor.run` for every realize run and consumes their results; the rest of it stays in the working copy for Phases 5-10. `realize` builds a `RunContext` (`build_context`: package shape gate, policy default, RPKG id; the first three statements of the oracle's `realize`), appends the lineage entry, runs the Analyzer and the Governor, and calls `process_observation(context, analysis, governance)`. The oracle is unchanged.

### Where the moved names live

Every name keeps its spelling, signature, messages and values; `realizer.realizer.<name>` still resolves for each one.

| Module | Names | From |
|---|---|---|
| `src/realizer/analyzer/analysis.py` | `normalize_scalar`, `config_hash`, `photon_bool_from_package`, `package_config`, `observed_config`, `scenario_by_id`, `recommended_scenario`, `candidate_by_id`, `candidate_steps` | `realizer.py` |
| `src/realizer/analyzer/analysis.py` | `analyze_observation` (the first statements of `process_observation`: the three UTC times, observed and source configurations, both hashes, the REVID id and evidence record), `summarize_history` (latest application and latest measurement of a loaded ledger), `config_drift` (the drift test) | `process_observation` |
| `src/realizer/analyzer/analyzer.py` | `run(context) -> RealizationAnalysis` | new (replaces the above statements and the history reads of `process_observation`) |
| `src/realizer/analyzer/collector/collector.py` | `load_observation` (replay of an observation file; the CLI's inline `load_json`), `demo_observation` | `realizer.py` |
| `src/realizer/packager/ledger.py` | `ledger_path`, `load_ledger`, `latest_event` (the read side of the ledger only) | `realizer.py` |
| `src/realizer/governor/policy.py` | `RealizerPolicy` (+ `from_mapping`, `snapshot_id`), `policy_to_dict`, `readiness`, `validate_performance`, `counterfactual_eligible`, `economic_quality_result`, `authority_eligible`, `savings_improvement_material` | `realizer.py` |
| `src/realizer/governor/governor.py` | `run(context, analysis) -> GovernanceResult` | new |
| `src/realizer/contracts/analysis.py` | `ObservationAnalysis`, `RealizationHistory`, `RealizationAnalysis` (internal hand-offs; `__all__` stays the public enums) | new |
| `src/realizer/contracts/governance.py` | `GovernanceResult` | new |
| `src/realizer/contracts/request.py` | `RunContext` | new |
| `src/realizer/common/utils.py` | `Deferred` | new |
| `src/realizer/config/settings.py` | `NON_AUTHORITATIVE_EXECUTION_STATES` (the state set `authority_eligible` tested inline) | `realizer.py` |
| `src/realizer/realizer.py` | `build_context` | new (first statements of `realize`) |

Not moved, per the extraction map: `compare_step`, `choose_application`, `_directional_relation` (reconciler), `expected_annual_for_exact_step`, `calculate_economics`, `attribution_result`, `make_cf_id` (estimator), `append_if_absent`, `ledger_checkpoint`, `make_ledger_entry`, `project_snapshot`. `attribution_result` keeps its policy thresholds inline, so no attribution-policy check exists yet to move.

### Decisions

- **Collector is a replay and demo stub.** `collector.py` holds `load_observation` and `demo_observation` and nothing else: the oracle has no live collection, so there is no `queries/` folder, no SQL and no live entry point. The design's exit criterion "collector integration tests pass" is not applicable and is recorded as such. `demo_observation` imports `authority_eligible` from the governor, because the demo picks the first step the policy lets carry authority.
- **Ledger read side moved early.** The Analyzer must read the history a run starts from and cannot import the engine, so `ledger_path`, `load_ledger` and `latest_event` moved to `packager/ledger.py` (the extraction map's early slice of the Packager). Appending, checkpointing and the snapshot stay in the engine.
- **Failed runs keep their half-written ledger (map option A, not atomic).** A run that raises midway leaves every earlier append and file in place, as before. The engine still appends the lineage entry before the Analyzer runs, so a malformed observation fails inside `analyzer.run` after it and before the evidence file, as it always did, and `analyzer.run` writes nothing. Nothing reads back what the same run appended, so reading the history before the evidence file is written (the oracle read it after) is unobservable: the ledger was already read twice by then, so the read cannot fail.
- **Policy checks stay lazy: `Deferred`.** The oracle evaluates each check only on its branch, so a malformed field the run never reaches does not fail it (for example a malformed `performance` on an observation that applied nothing, or `elapsed_days` on a drift observation). A governor that evaluated every check and raised would change that. `Deferred.of(fn, ...)` calls the pure check early and keeps the `Exception` it raised; `get()` returns the value or re-raises the same exception. The engine calls `get()` at exactly the oracle's evaluation points: readiness (before the validation episode), performance (after VALIDATION_STARTED), counterfactual eligibility (after VALIDATION_COMPLETED), economic quality (inside the available-economics branch). `GovernanceResult.policy_snapshot_id` is a `Deferred` too, since a policy whose snapshot cannot be hashed only fails where the id is used. The checks have no I/O and no logging, so evaluating them early has no side effect. `calculate_economics` and `attribution_result` still call `readiness` and `economic_quality_result` themselves, as before.
- **`RealizationAnalysis.observation` is not deferred.** The analyzer's own failures (a missing time, a non-mapping `config`) happen in the oracle at the start of observation processing, so `analyzer.run` raises them directly where the engine calls it. When the later phases move the lineage append into the Packager and run the Analyzer first, that failure must be held as a `Deferred` or halt marker and raised after the append; this is the same decision as the Governor's and is left to the phase that reorders them.
- **A lineage-only run reads the history too.** `analyzer.run` always returns the history (an extra read-only file read when there is no observation); `observation` is `None` and the Governor holds no checks.
- **Internal hand-offs.** `RealizationAnalysis`, `ObservationAnalysis`, `RealizationHistory`, `GovernanceResult` and `RunContext` are internal values; the contracts import `Deferred` and the policy type only under `TYPE_CHECKING`.
- **Re-exports.** The names removed from the working copy stay importable from `realizer.realizer` (marked `noqa: F401`), as in Phases 1 and 2, so existing callers and tests are unaffected. Unused-import warnings from pyflakes on `src/realizer/realizer.py` are these re-exports and the oracle's unused local `actor`.

### Verification

| Check | Result |
|---|---|
| `scripts/compare_realizer_single_file.py` (candidate `realizer.realizer`, through the shadowing guard) | 41 scenarios: oracle = stored baseline = modular engine |
| Unnormalized output trees and log lines of every scenario, oracle against modular (`test_single_file_compatibility`) | byte-identical, 41/41 |
| Subprocess determinism (`test_subprocess_determinism`: both engines, `PYTHONHASHSEED` 0 and 1, `TZ` UTC and Asia/Kolkata, 9 scenarios covering replay-through, restatement, reprocessed restatement, drift, rollback, supersession, scenario scope, migration and policy change) | identical across all eight combinations and equal to the stored baseline |
| Analyzer unit tests (`tests/unit/realizer/analyzer/`): every moved function and `analyze_observation` against the oracle on valid and malformed input (same exception type and message), the evidence record against every stored REVID file of the 41 scenarios, `run` (history, drift flag, read-only, lineage-only, corrupt ledger) for every run of every scenario, the collector (replay errors, demo observation for both packages and malformed packages), failed runs (same outcome and same output tree as the oracle) | pass |
| Governor unit tests (`tests/unit/realizer/governor/`): policy defaults, overrides, `snapshot_id` and `from_mapping` errors, every check against the oracle over every scenario observation and five policies and over malformed observations, `run` (`Deferred.get` equals the oracle call or raises its exception, run never raises, pure), failed and lazy runs: for each check, a malformed field raises the oracle's exception at the oracle's step with the oracle's half-written ledger, and an unused malformed field does not raise | pass |
| Mutation check | making the Governor evaluate `readiness` and `validate_performance` eagerly (no `Deferred`) fails 19 of the new tests |
| Ledger read side, `RunContext`/`build_context`, `Deferred` unit tests | pass |

### Review-gap fixes (found by the Phases 1-2 review)

1. **Candidate-missing is an error.** `scripts/compare_realizer_single_file.py` no longer falls back to comparing the oracle with the stored baseline when the candidate cannot be found. An unresolvable candidate exits 2 with a message on stderr, the oracle-only mode is the explicit `--oracle-only` flag, and the default candidate is resolved through `canonical.modular_engine()` so that the `src/realizer` shadowing guard runs. Exit codes: 0 identical, 1 a difference or a candidate crash, 2 an unresolvable candidate or bad arguments. `tests/regression/realizer/test_compare_script.py` pins them, including a drifting and a crashing candidate (exit 1) and the real script in a subprocess.
2. **Time zone and hash seed are tested.** `test_subprocess_determinism.py` (see the verification table), about 0.3 s.
3. **Error paths the 41 scenarios cannot reach are compared directly.** Every moved function with a raise or lazy branch has a test running the oracle's copy and the moved one on the same malformed input and asserting the same exception type and message; whole-run tests compare the outcome and the output tree (files and directories) of failing runs.

### Behavior differences

None known. All 41 scenarios and every compared malformed case behave identically to the oracle.

## Phase 5: reconciler

The engine now runs `reconciler.run` after `governor.run` and records the lifecycle events it decides. `realize` runs the Analyzer, the Governor and the Reconciler; `process_observation(context, analysis, governance, reconciliation)` writes the evidence file, appends the lifecycle slots in the oracle's order (drift, the application events, intent, rollback requested, rollback observed, supersession), then either appends the regeneration request that ends the run (and writes its RERUN file) or, when validation is due, runs the oracle's validation and measurement section unchanged and appends the downstream regeneration after the measurement. The validation and measurement section and the ledger appends stay in the engine for Phases 6-8. The oracle is unchanged.

### Where the moved names live

Every name keeps its spelling, signature, messages and values; `realizer.realizer.<name>` still resolves for each one.

| Module | Names | From |
|---|---|---|
| `src/realizer/reconciler/matching.py` | `_directional_relation`, `compare_step`, `choose_application` | `realizer.py` |
| `src/realizer/reconciler/matching.py` | `match` (the selected scenario, the step matches, the authority changes and the scenario-scope decision) | `process_observation` |
| `src/realizer/reconciler/lifecycle.py` | `plan` (drift, active-episode reuse, the scenario-scope application, the per-step application loop, intent, rollback, supersession, the regeneration that ends the run, `validation_due`), `LifecyclePlan` | `process_observation` |
| `src/realizer/reconciler/lifecycle.py` | `regeneration_request` (the RERUN id and payload), `upstream_regeneration_request` (the downstream-invalidation rule) | `request_regeneration`, `process_observation` |
| `src/realizer/reconciler/reconciler.py` | `run(context, analysis, governance) -> ReconciliationResult` | new |
| `src/realizer/contracts/reconciliation.py` | `PlanMatch`, `PlannedApplication`, `ActiveApplication`, `RegenerationRequest`, `ReconciliationResult` (internal hand-offs; `__all__` stays the public enums) | new |
| `src/realizer/realizer.py` | `request_regeneration` (same signature: `regeneration_request`, then `append_regeneration`), `append_regeneration` (the ledger append and the RERUN file write) | `request_regeneration` |

Left in the engine for later phases: the validation and measurement section of `process_observation` (estimator and reviewer, Phases 6-7), `make_ledger_entry`, `append_if_absent`, `append_regeneration`, the evidence-file write and the slot-by-slot commit (packager, Phase 8).

### Decisions

- **Reconciliation decides, the engine records.** The Reconciler builds payloads, episode ids and event types; the engine turns them into ledger entries (`make_ledger_entry`) and appends them one by one with the per-append duplicate suppression, so the RLE ids, the `ledger.append` and `ledger.duplicate_suppressed` log lines and every file stay identical. The interim commit loop in `process_observation` encodes the oracle's slot order; it moves to the Packager in Phase 8.
- **Failed runs keep their half-written ledger (Option A): `halt`.** `reconciler.run` never raises. `lifecycle.plan` fills a `LifecyclePlan` in recording order, each event as soon as it is decided; if anything raises (matching, the scenario-scope invalidation set, a payload value, a rollback or supersession flag, the regeneration request, the readiness check), `run` keeps the exception as `ReconciliationResult.halt` together with the events decided before it. The engine appends exactly those and then raises `halt`, so the run fails with the oracle's exception after the oracle's entries. This covers every raise point by construction, including values a JSON observation cannot carry (an object whose text or truth value cannot be taken), instead of case-by-case reasoning about which computation can raise between two appends.
- **Readiness is read where the oracle read it.** `lifecycle.plan` calls `governance.readiness.get()` as its last step, only for an active application that neither drifted nor was applied contrary, after every lifecycle event; a failure there is a `halt` raised after the lifecycle slots and before the validation episode. A run with no active application, a drift or a contrary application never reads it. `validation_due` is the readiness verdict; the engine reads the cached readiness value for the validation payloads.
- **The ending regeneration is a value, the downstream one is held.** The regeneration that ends the run (drift, contrary) is decided after every lifecycle event, so it is computed in place and a failure is a `halt` raised before its append. The downstream regeneration (an invalidating or modified application whose remeasurement window has passed) comes after the measurement in the oracle, so `upstream_regeneration` is a `Deferred`, created only when validation is due and forced by the engine after the measurement and the billing correction.
- **Explicit episode flag.** The oracle's identity test "the active application is the previous run's application" is `ActiveApplication.is_previous`, set when the previous application is taken over (drift, or the same configuration observed again) and cleared when this run opens an episode. It matters only for a plan that repeats a step number with an observation that neither drifts nor reuses the previous episode; a unit test covers that shape.
- **Reconciler runs before the evidence file.** `reconciler.run` reads and writes nothing and never raises, so running it from `realize` before `process_observation` writes the evidence file is unobservable; a matching failure is raised after the evidence file, where the oracle raised it. When the Packager takes over the evidence write, the same `halt` places the failure after it.
- **Contracts.** The hand-off dataclasses are frozen and internal; `contracts/reconciliation.py` imports `Deferred` and `EventType` only under `TYPE_CHECKING`, so the contracts still import nothing at run time from `common` and `realization.py` can keep re-exporting its enums. Payload mappings are passed through unchanged (no copies, no read-only wrappers) so their content and key order match what the oracle hashed.

### Verification

| Check | Result |
|---|---|
| `scripts/compare_realizer_single_file.py` (candidate `realizer.realizer`, through the shadowing guard) | 41 scenarios: oracle = stored baseline = modular engine |
| Unnormalized output trees and log lines of every scenario, oracle against modular (`test_single_file_compatibility`), and subprocess determinism | byte-identical, 41/41; identical across hash seeds and time zones |
| Matching unit tests (`tests/unit/realizer/reconciler/test_matching.py`): `_directional_relation` over 4,096 value triples, `compare_step` over every plan step of both packages against every observed configuration and over every knob with valid and malformed values, `choose_application` for every scenario observation and five scenario choices, `match` against the oracle's inline statements for every scenario observation; malformed packages and observations raise the oracle's exception | pass |
| Lifecycle unit tests (`test_lifecycle.py`): `regeneration_request` against the oracle's `request_regeneration` for every stored application payload and the three reasons, fallbacks, policies and malformed input; `upstream_regeneration_request` over 1,440 input combinations; the engine's `request_regeneration` writes the oracle's tree; drift, reuse, rollback, supersession, contrary, scenario scope, migration, modified, pending downstream, not ready and the repeated-step reuse rule, each also compared tree for tree with the oracle | pass |
| `run` over every run of every scenario (`test_reconciler.py`): the decided lifecycle slots, with duplicate suppression, are exactly the entries the oracle appended after its evidence file; then the ending regeneration, the validation section with the downstream regeneration last, or nothing; every RERUN file equals the decided payload; every lifecycle kind occurs | pass |
| Failed runs (`test_failed_runs.py`): matching failures (malformed step number, plan entry, invalidation list, scenario, recommended plan, an applied non-recommended scenario), the scenario-scope invalidation set, values that fail after the drift entry, after the intent and after each rollback entry, the ending regeneration (drift and contrary, policy whose snapshot cannot be hashed), readiness after a scenario-scope application, the downstream regeneration (policy and step remeasurement window) after the measurement and after a billing correction; unreached inputs do not fail; recovery runs | same outcome and same output tree as the oracle |
| Mutation checks | a Reconciler that raises at once instead of keeping `halt` fails the automated mutation test (3 of 3 failed-run trees differ); reading readiness before the lifecycle fails 3 tests; computing the ending regeneration before the lifecycle fails 1; forcing the downstream regeneration eagerly fails 1; dropping the `is_previous` flag fails 1 |
| Test suite | 1073 tests (1018 before: plus 55 reconciler unit tests; 4 skip as before), all pass |

### Known oracle behavior kept for parity

- An authority-eligible step whose observed value is unrelated to the recommendation (for example an unknown warehouse size) is recorded as `APPLICATION_OBSERVED` although it never becomes the active application; the snapshot then counts it as an applied application with conformance `UNRELATED`, and later runs treat it as the previous application.
- `compare_step` compares the normalized observed value with the package's raw values, so a package value with different spelling or spacing matches differently than its normalized form would.

Both are recorded for a separately reviewed change.

### Behavior differences

None known. All 41 scenarios and every compared malformed case behave identically to the oracle.

## Phases 6 and 7: estimator and reviewer

When validation is due, `process_observation` now runs `estimator.run` and then `reviewer.run` and records the facts they decide: the validation episode (VALIDATION_READY, VALIDATION_STARTED, VALIDATION_COMPLETED), then COUNTERFACTUAL_RECORDED, ECONOMIC_MEASUREMENT_RECORDED and BILLING_CORRECTION_RECORDED, in the oracle's order, and then forces the downstream regeneration and appends it. The ledger appends stay in the engine for Phase 8. The oracle is unchanged.

### Where the moved names live

Every name keeps its spelling, signature, messages and values; `realizer.realizer.<name>` still resolves for each one, and so does every other name the engine had (the helpers it imported for them).

| Module | Names | From |
|---|---|---|
| `src/realizer/estimator/economics.py` | `make_cf_id` (same signature and content dict), `expected_annual_for_exact_step`, `calculate_economics` | `realizer.py` |
| `src/realizer/estimator/economics.py` | `counterfactual_record` (the CF id and the COUNTERFACTUAL_RECORDED payload), `measurement_facts` (the MEAS id and the measurement payload fields), `correction_facts` (the BILLING_CORRECTION_RECORDED payload and the entry it corrects) | `process_observation` |
| `src/realizer/estimator/attribution.py` | `attribution_result`, `authoritative_cumulative_realization` (not on the realize path) | `realizer.py` |
| `src/realizer/estimator/estimator.py` | `run(context, analysis, governance, reconciliation) -> RealizationEstimate` | new |
| `src/realizer/reviewer/validation.py` | `plan_episode` (the VAL id and the ready, started and completed payloads from the readiness and performance verdicts) | `process_observation` |
| `src/realizer/reviewer/economics.py` | `recognition_status` (CONFIRMED, REJECTED or PROVISIONAL, the measurement payload's expression) | `process_observation` |
| `src/realizer/reviewer/reviewer.py` | `run(context, analysis, governance, reconciliation, estimate) -> ReviewResult` | new |
| `src/realizer/contracts/realization.py` | `CounterfactualRecord`, `MeasurementFacts`, `CorrectionFacts`, `RealizationEstimate`, `ValidationEpisode`, `ReviewResult` (internal hand-offs, after the public types and outside `__all__`) | new |

Left in the engine for Phase 8: `make_ledger_entry`, `ledger_business_key`, `append_if_absent`, `ledger_checkpoint`, `append_regeneration`, `request_regeneration`, the evidence-file write, the slot-by-slot commit in `process_observation` (lifecycle, validation, measurement and regeneration slots) and `project_snapshot`; for Phases 9 and 10, `realize`, `build_context`, `legacy_compatibility`, `parse_args` and `main`.

### Decisions

- **Failed runs keep their half-written ledger (Option A): `halt` per stage.** Both stages run before the engine records any validation entry, but the oracle raised between those entries. `estimator.run` and `reviewer.run` never raise: each decides its facts in recording order and, if a step raises, keeps the exception as `halt` together with the facts decided before it, as the Reconciler does. The engine records the episode's payloads present and raises the review's `halt`; then records the counterfactual, the measurement and the correction present and raises the estimate's `halt`; then forces the downstream regeneration. Since the episode is recorded before every estimator slot, its failure is raised first, which is the oracle's order: the only estimator step that precedes VALIDATION_COMPLETED in the oracle is the performance verdict, and the completed payload reads the same verdict, so the review halts with the same exception. Entry construction (`make_ledger_entry`, including the `corrects_entry_id` check) still happens in the engine immediately before each append, so its failures land where they always did.
- **Where each window now raises.** Before VALIDATION_READY: the episode id (the policy snapshot, values the id hashes). After VALIDATION_STARTED: the performance verdict. After VALIDATION_COMPLETED: counterfactual eligibility, the counterfactual id and `money` of its cost. After COUNTERFACTUAL_RECORDED (or after the completed validation when there is no counterfactual cost): `calculate_economics`, the financial-quality gates, the attribution (`int` of the overlap count) and the measurement id and payload. After ECONOMIC_MEASUREMENT_RECORDED: the correction payload and the `corrects_entry_id` check. After the measurement or the correction: the downstream regeneration's remeasurement windows. A failure of the started payload alone after the ready entry is not reachable from input: every value it records is part of the episode id.
- **Policy checks stay lazy.** The Estimator reads the governor's `Deferred` checks at the oracle's points: performance, then counterfactual eligibility, then (only for available economics, after `calculate_economics`) the quality gates. Unavailable economics never read the gates, the attribution inputs or the correction; a run that is not validated reads none of them.
- **Economics are a new mapping.** The oracle added `quality_gates` to the economics in place before attribution read it. The Estimator builds `{**economics, "quality_gates": gates}` instead: the same keys in the same order (the gates last) and the same values, and every serialization sorts keys. Nothing else held the first mapping, and the gates mapping is shared by the economics and the attribution's `economic_quality_gates` exactly as before (it is deep-copied into the ledger payload).
- **The Reviewer assembles only.** The oracle has no review gate, so the Reviewer adds no check that can reject or alter an outcome: it builds the episode payloads from the governor's verdicts, takes the recognition status from the estimate's verdicts, and passes the estimate through as it is. The design's reviewer checks (evidence completeness, lifecycle, rollback and drift validation, baseline, actual-cost, attribution, overlap, no-double-count, annualization and expected-versus-realized consistency) would be new behavior and are not added.
- **No step, plan or package realization levels.** The oracle measures once per active application episode: a candidate-scope application is the closest thing to a step realization, a scenario-scope application to a plan realization, and the snapshot's latest measurement to a package realization; `authoritative_cumulative_realization` is the only package-level helper and is off the realize path. The design's three realization levels would be new behavior and are not invented here.
- **Measurement payload assembly.** The Estimator returns the measurement fields and the Reviewer the recognition status; the engine joins them into the ECONOMIC_MEASUREMENT_RECORDED payload in the oracle's key order. That join moves to the Packager in Phase 8.
- **Contracts.** The hand-offs live in `contracts/realization.py`, after the public types and outside `__all__` (design section 14 places the realization estimate and review result there); the earlier capabilities' hand-offs are imported there only under `TYPE_CHECKING`. Payload mappings are passed through unchanged, so their content and key order match what the oracle hashed.
- **Shared test helpers.** `Unprintable`, `Untestable`, `events`, `files`, `ledger_lines`, a side-by-side run of every scenario with each modular stage's calls captured, and an independent assembly of the validation and measurement entries moved to `tests/unit/realizer/parity.py`, so no test module imports another.

### Verification

| Check | Result |
|---|---|
| `scripts/compare_realizer_single_file.py` (candidate `realizer.realizer`, through the shadowing guard) | 41 scenarios: oracle = stored baseline = modular engine |
| Unnormalized output trees and log lines of every scenario, oracle against modular (`test_single_file_compatibility`), and subprocess determinism | byte-identical, 41/41; identical across hash seeds and time zones |
| Moved functions (`test_economics.py`, `test_attribution.py`): source text and signature identical to the oracle's; `make_cf_id` over stored applications, scenario observations and policies; `expected_annual_for_exact_step` over every plan step of both packages and nine candidate-economics shapes; `calculate_economics` over 11,088 combinations (applications, economics, readiness, verdicts, policies) and `attribution_result` over 25,920; malformed input raises the oracle's exception | pass |
| Economics cases pinned to values: ratio classes (realized, under-realized, re-optimization, unclassified), every denominator source, period and annualization fallbacks, separate DBU and AWS components with unknown values null, trailing-twelve-month savings, cost increase, unavailable economics, guardrail failure, a counterfactual that cannot support a claim, billing correction fallbacks, overlap never claimed, each quality gate's disqualifier, and the cumulative realization measured against the original counterfactual (no double count) | pass |
| `estimator.run` and `reviewer.run` over every run of every scenario (`test_estimator.py`, `test_reviewer.py`): the episode, counterfactual, measurement and correction entries assembled from their results, with duplicate suppression, are exactly the oracle's entries for those slots; both run only when validation is due; realized, under-realized, failed-validation, blocked and monitoring states, confirmed, rejected and provisional recognition, and billing corrections all occur | pass |
| Failed runs (`estimator/test_failed_runs.py`, `reviewer/test_failed_runs.py`): the episode id, the performance verdict, counterfactual eligibility, cost and id, economics, quality gates, attribution, measurement id and payload, the correction (`corrects_entry_id`, payload values), and the downstream regeneration after the measurement and after the correction; failures that compete (performance and economics, economics and gates, gates and attribution); unreached inputs; recovery | same outcome and same output tree as the oracle |
| Mutation checks (automated) | an Estimator or a Reviewer that raises at once, and an Estimator that reads the quality gates eagerly, each fail their mutation test |
| Mutation checks (source edits, realizer unit suites) | the Estimator raising at once fails 51 test cases; the Reviewer raising at once 17; reading the quality gates before the economics 6; reading them for unavailable economics 4; raising the estimate's failure before the episode 38; forcing the downstream regeneration before the episode 2; raising the correction's failure before the measurement is recorded 3 |
| Test suite | 1136 tests (1073 before: plus 63 estimator and reviewer unit tests; 4 skip as before), all pass |

### Known oracle behavior kept for parity

- The measurement's `correction_status` is the observation's raw correction type (`ORIGINAL` when absent, the spelling as given otherwise), while the BILLING_CORRECTION_RECORDED payload upper-cases the type and defaults it to `RESTATEMENT`.
- The counterfactual id hashes the observation's `window_start_utc` as given, while the validation and measurement ids fall back to the applied time when it is absent.
- `attribution_result` turns an origin that cannot be turned into text into `UNKNOWN`, because its enum fallback also catches the text conversion's error.
- On the realize path `calculate_economics` re-evaluates readiness, which is always ready there (validation is due only when ready), and `attribution_result` always receives the quality gates, so its own gate fallback is reached only by direct calls.
- A cost increase is classified as under-realized with re-optimization recommended and a negative ratio, and a claimable one is attributed as negative savings.

All are recorded for a separately reviewed change.

### Behavior differences

None known. All 41 scenarios and every compared malformed case behave identically to the oracle.

## Phase 8: packager

The ledger write side, the snapshot projection and the slot-by-slot commit leave the engine. `realize` now records the lineage (`packager.begin`), runs the Analyzer, the Governor and the Reconciler, runs the Estimator and the Reviewer when validation is due, and hands the reviewed result to `packager.run`, which records every event in the oracle's order, reloads the ledger, applies the replay cut and projects the snapshot. `process_observation` is gone. The oracle is unchanged.

### Where the moved names live

Every name keeps its spelling, signature, messages and values; `realizer.realizer.<name>` still resolves for each one (functions are the same objects, imported with `# noqa: F401`), and so does every other name the engine had.

| Module | Names | From |
|---|---|---|
| `src/realizer/packager/ledger.py` | `ledger_business_key`, `make_ledger_entry`, `append_if_absent` (reload-per-append duplicate suppression and its `ledger.append` / `ledger.duplicate_suppressed` log lines), `ledger_checkpoint` | `realizer.py` |
| `src/realizer/packager/ledger.py` | `append_regeneration` (the REGENERATION_REQUESTED entry, then the RERUN file) | `realizer.py` |
| `src/realizer/packager/ledger.py` | `record_creation` (the REALIZATION_CREATED entry), `commit_events` (the evidence file, the lifecycle slots, the terminal regeneration or the validation, measurement and downstream-regeneration slots, with the halts raised at their positions), `evidence_path`, `regeneration_path`, `replacement_payload` (the supersession and link payload), `measurement_payload` (the measurement facts joined with the recognition status) | `realize`, `process_observation` |
| `src/realizer/packager/snapshot.py` | `project_snapshot`, whole and verbatim (same source text as the oracle's) | `realizer.py` |
| `src/realizer/packager/packager.py` | `begin(context)`, `run(context, reviewed) -> PackagingResult` | new |
| `src/realizer/packager/packager.py` | `request_regeneration` (same signature, re-exported by the engine) | `realizer.py` |
| `src/realizer/contracts/realization.py` | `PackagingResult` (`realization`, `entries`, `written`), an internal hand-off after the public types and outside `__all__` | new |
| `src/realizer/realizer.py` | `review(context, analysis, governance, reconciliation) -> ReviewResult`: runs the Estimator and the Reviewer when validation is due and the reconciliation did not fail, otherwise an empty review | `process_observation` |

Left in the engine for Phases 9 and 10: `realize`, `build_context`, `review`, `legacy_compatibility`, `parse_args` and `main`, and the two final writes of `realization/packages/<RPKG>.json` and `realization/snapshots/<RSNAP>.json` (the Reporter writes them in Phase 9).

### Decisions

- **The lineage entry is recorded before the Analyzer, so `begin` is separate from `run`.** The oracle appends REALIZATION_CREATED first and a malformed observation fails in the Analyzer after it. The Analyzer raises rather than keeping a `halt`, so committing the lineage inside `run` would leave no entry after an Analyzer failure. `packager.begin` records it at the oracle's position; `run` records the rest, and calling both never appends the entry twice (a repeat would log `ledger.duplicate_suppressed`). The oracle's initial ledger read, whose result is overwritten, stays as the first statement of `record_creation`: it keeps a corrupt ledger failing before anything is built.
- **One commit, the oracle's order and positions.** `commit_events` records the evidence file, then drift, the application events, intent, rollback requested and observed, and supersession and the replacement link, and raises the reconciliation's `halt`; then either the terminal regeneration and its RERUN file (the run ends) or, when validation is due, the validation episode (ready, started, completed) and the review's `halt`, the counterfactual, the measurement and the correction and the estimate's `halt`, then the downstream regeneration (forced last, so its remeasurement failure comes after the measurement and the correction). Every entry is built by `make_ledger_entry` immediately before its own append, so a malformed value (a `corrects_entry_id` that is not an RLE id) fails at the position it always failed. The dedup stays per append: a repeated entry is suppressed even when an earlier slot of the same run recorded it.
- **Option A is kept.** Failed runs leave the oracle's half-written ledger: the stages keep failures as `halt`, and the Packager records the facts decided before it and raises. The Estimator and the Reviewer still run before the commit (they never raise), but only when validation is due and the reconciliation did not fail, so a failed reconciliation never reaches them, as before.
- **The replay cut is applied after the commit.** `run` reloads the ledger, cuts it at `replay_through` (an invalid cut raises, and a cut before every entry leaves nothing to project, both after the events were recorded and before any package or snapshot file is written) and projects the snapshot from the cut entries. `PackagingResult.entries` holds those entries and `written` the evidence (REVID) and regeneration (RERUN) files written, in write order. The package and snapshot files are not among them.
- **`project_snapshot` is moved whole.** It folds lifecycle precedence, plan progress, validation, realization and maturity state and regeneration status over every entry; splitting it would risk the snapshot identity.
- **`request_regeneration` lives in `packager.py`, not `ledger.py`.** It needs the Reconciler's `regeneration_request`, and importing the Reconciler from `ledger.py` would close an import cycle (the Analyzer reads the ledger). `append_regeneration` stays in `ledger.py`.
- **Payload builders only where the engine built payloads inline.** Two exist: the supersession and link payload and the measurement payload (the join the previous phases left in the engine). The other events' payloads are decided by the capabilities and passed through unchanged.
- **Shared test helpers.** The side-by-side scenario runner now also captures the Packager's calls and each run's replay cut (`tests/unit/realizer/parity.py`).

### Verification

| Check | Result |
|---|---|
| `scripts/compare_realizer_single_file.py` (candidate `realizer.realizer`) | 41 scenarios: oracle = stored baseline = modular engine; the stored baseline is not regenerated |
| Unnormalized output trees and log lines of every scenario, oracle against modular (`test_single_file_compatibility`), and subprocess determinism | byte-identical, 41/41 |
| Moved functions (`test_ledger.py`): source text and signature identical to the oracle's; `make_ledger_entry` rebuilds all 363 stored entries of the 41 ledgers with their RLE ids and bytes; `ledger_business_key` over the same entries; 17 malformed-input cases raise the oracle's exception; `ledger_checkpoint` over every prefix, forward and reversed (808 comparisons) | pass |
| `append_if_absent` (`test_ledger.py`): each of the 41 ledgers appended entry by entry, then every entry again; returns, file bytes and log lines identical; a corrupt ledger and an entry that cannot be serialized fail like the oracle and leave the file alone | pass |
| `project_snapshot` (`test_snapshot.py`): 452 projections (every file-order prefix and every recorded-time cut of the 41 ledgers) equal the oracle's; each of the 51 stored snapshots (RSNAP) is reproduced from a prefix of its ledger; shuffled and duplicated entries; malformed package and entry input | pass |
| `packager.run` over all 53 runs of the 41 scenarios (`test_packager.py`): the result's entries are the oracle's ledger lines after the run, cut at the replay time; the snapshot is the package body the oracle wrote; the written files are the evidence file and the regeneration files, all present in the oracle's tree; output trees and log lines identical | pass |
| Replay (`test_packager.py`): 171 cuts (every recorded time of every ledger, one before all entries, one after all) reproduce the oracle's snapshot and leave the same trees; the cut is applied after the commit; the same observation twice records nothing the second time and logs only `ledger.duplicate_suppressed`; a billing restatement is a correction of the earlier measurement | pass |
| Failed runs (`packager/test_failed_runs.py`): a corrupt ledger, the Analyzer, matching, after drift, intent, rollback requested and rollback observed, readiness, the review (after ready and started, before the episode id), the counterfactual, economics, attribution, the correction entry and payload, the downstream regeneration after the measurement and after the correction, and a replay cut that is not a time or leaves nothing to project; recovery after a failure | same outcome and same output tree as the oracle; the last event each leaves is pinned |
| Mutation checks (automated) | recording one pair of adjacent slots in the other order (nine pairs: supersession and link, application and intent, drift and rollback, ready and started, started and completed, completed and counterfactual, counterfactual and measurement, measurement and correction, measurement and downstream regeneration), an `append_if_absent` without duplicate suppression, a Packager that raises a kept failure before recording the events ahead of it (reconciliation, review, estimate) and one that forces the downstream regeneration early each fail their mutation test |
| Mutation checks (source edits, realizer unit suites) | supersession and link swapped fails 10 checks; no duplicate suppression 125; reconciliation failure raised before the lifecycle 29; intent before the application events 308; evidence file not written 332; lineage recorded after the Analyzer 11; replay cut ignored 9; correction without its `corrects_entry_id` 23; recognition status dropped 229; downstream regeneration forced before the validation 4; estimate failure raised before the review's 23. One edit is equivalent and passes: writing the RERUN file before its ledger append (the append cannot fail once the ledger loaded) |
| Test suite | 1176 tests (1136 before: plus 40 packager unit tests; 4 skip as before), all pass |

### Known oracle behavior kept for parity

- `realize` reads the ledger before it records the lineage entry and then overwrites the result: only the read's failure is observable, so a corrupt ledger raises before the lineage entry exists, while a malformed observation raises after it.
- A repeated entry is suppressed by id per append, so a run that records an event an earlier run (or an earlier slot) already recorded logs `ledger.duplicate_suppressed` and still rewrites its evidence and regeneration files with the same content.
- A replay cut before every entry raises `cannot project empty ledger` after the events were recorded, and a cut that is not a time raises after them too; neither writes the package or snapshot file.

All are recorded for a separately reviewed change.

### Behavior differences

None known. All 41 scenarios and every compared malformed case behave identically to the oracle.

## Phases 9 and 10: reporter and orchestration

The two final writes and the legacy compatibility projection leave the engine for the Reporter, `realize` becomes the publisher-shaped facade, and the command line moves to a CLI adapter with a console script. `realizer.py` now only sequences the capabilities. The oracle is unchanged.

### Where the moved names live

Every name keeps its spelling, signature, messages and values; `realizer.realizer.<name>` still resolves for each one (functions are the same objects, imported with `# noqa: F401`; `parse_args` and `main` resolve lazily, see Decisions), and so does every other name the engine had, including the standard-library modules it imported.

| Module | Names | From |
|---|---|---|
| `src/realizer/reporter/reporter.py` | `run(context, packaged) -> RealizationReport` (writes `realization/packages/<RPKG>.json`, then `realization/snapshots/<RSNAP>.json`, the same payload) | `realize` |
| `src/realizer/reporter/reporter.py` | `legacy_compatibility`, source text identical to the oracle's | `realizer.py` |
| `src/realizer/contracts/report.py` | `RealizationReport` (`realization`, `files`), an internal hand-off beside the workbook projection; `report.py` has no `__all__`, so no public list changed | new |
| `src/realizer/cli/realize.py` | `parse_args`, `run` (the body of `main`) | `realizer.py` |
| `src/realizer/cli/main.py` | `main` (hands its arguments to `realize.run`) | `realizer.py` |
| `src/realizer/__main__.py`, `scripts/run_realizer.py`, `pyproject.toml` `[project.scripts]` | `python -m realizer`, the no-install wrapper, `realizer = "realizer.cli.main:main"` | new |
| `src/realizer/realizer.py` | `realize_request(request) -> RealizationReport`, the pipeline | `realize` |
| `src/realizer/realizer.py` | `realize(package, observation, output_dir, *, policy=None, replay_through=None)`, the oracle signature, now `realize_request(...).realization` | `realize` |
| `src/realizer/realizer.py` | `build_context` (unchanged) and `review` (the Estimator and the Reviewer as one call, see Decisions) | `realizer.py` |
| `src/realizer/estimator/estimator.py`, `src/realizer/reviewer/reviewer.py` | the gate on `reconciliation.halt` and `reconciliation.validation_due`, now inside each `run` | `review` |

Not moved: the single-file `.xlsx` renderer (`legacy/realizer_single_file/reporters/realizer_workbook_reporter.py`), and `legacy/realizer_single_file/` itself (Phase 11).

### The facade

```python
def realize_request(request: RealizationRequest) -> RealizationReport:
    context = build_context(request)
    packager.begin(context)
    analysis = analyzer.run(context)
    governance = governor.run(context, analysis)
    reconciliation = reconciler.run(context, analysis, governance)
    estimate = estimator.run(context, analysis, governance, reconciliation)
    reviewed = reviewer.run(context, analysis, governance, reconciliation, estimate)
    packaged = packager.run(context, reviewed)
    return reporter.run(context, packaged)


def realize(package, observation, output_dir, *, policy=None, replay_through=None) -> rc.RealizationPackage:
    return realize_request(RealizationRequest(
        package=package, observation=observation, output_dir=output_dir, policy=policy, replay_through=replay_through,
    )).realization
```

### Decisions

- **Two entry points.** `realize_request(request) -> RealizationReport` is the pipeline (the publisher's `publish` shape); `realize` keeps the oracle's signature and return value (the snapshot) and delegates to it, so the regression baseline, the ported oracle suites, the unit tests and the comparison script run unchanged. `RealizationRequest`'s fields are still exactly `realize`'s parameters in order (pinned by `contracts/test_request.py`).
- **The gate moved into the stages.** The `review` helper ran the Estimator and the Reviewer only when validation was due and the reconciliation had not failed. `estimator.run` and `reviewer.run` now return an empty result (`RealizationEstimate()`, a `ReviewResult` with no episode, status or halt) in those cases, and the facade calls both every time. The Packager never read an estimate or a review on a halted or not-due run, so nothing recorded changes. The staged-run test helper accordingly asserts that both stages did nothing, instead of that they were not called.
- **`review` stays as a name.** It is not used by the pipeline; it composes the two stages (no condition of its own) so that every name the engine had still resolves. The Packager unit test that built a review through it is unchanged.
- **`parse_args` and `main` resolve lazily.** The CLI imports the facade, so the facade cannot import the CLI at load without a cycle. A module-level `__getattr__` returns the CLI's functions on first access; `from realizer.realizer import main` and `realizer.realizer.parse_args` work, and any other missing name still raises `AttributeError`. `python -m realizer.realizer` still runs the CLI.
- **The CLI reaches the core only through the facade.** It imports `realizer.realizer` (for `realize`, `demo_observation`, `load_observation` and `RealizerPolicy`, as the publisher CLI reaches its demo through the facade), `realizer.config.loader` and `realizer.common.logging`; a test checks with `ast.walk` that no capability package is imported. Flags, defaults, help text, load order (package, observation or demo, policy), stdout (the package path, or the snapshot with `--print-json`) and logging setup are the oracle's.
- **No exit-status mapping and no `common/errors.py`** (decided in Phase 2: a behavior change). A failed run still ends in an uncaught exception (status 1); argparse errors exit 2 as before.
- **No `reporter/workbook.py`.** The workbook projection already lives in `contracts/report.py`; the renderer needs the `artifact_tool` runtime and the retired Publisher's workbook reporter, and nothing on the realize path calls it. A module would be empty or a stub (design 4.3), so none is created; a test pins the Reporter package to `reporter.py`. The workbook stays projection-only.
- **The dead local in `legacy_compatibility` is kept.** The function is moved verbatim, including the oracle's unused `actor` local (pyflakes flags it). Removing it would not change the output, but a verbatim move lets a test compare the source text with the oracle's.
- **Failed writes keep the oracle's tree (Option A).** `reporter.run` writes the package, then the snapshot, with the same writer; a failure of either leaves the ledger and files the oracle left (the package file when only the snapshot fails). The Reporter raises, as the oracle did; it has no `halt`.
- **The facade holds no domain logic.** Tests check on the source that its functions contain only the stage calls (no `if`, loop, `try` or comprehension in the pipeline) and none of the vocabulary of SQL, lifecycle, realized-savings formulas, ledger folding or rendering; `build_context` keeps the oracle's policy default.

### Verification

| Check | Result |
|---|---|
| `scripts/compare_realizer_single_file.py` (candidate `realizer.realizer`) | 41 scenarios: oracle = stored baseline = modular engine; the stored baseline is not regenerated |
| Unnormalized output trees and log lines of every scenario, oracle against modular (`test_single_file_compatibility`), and subprocess determinism | byte-identical, 41/41 |
| `reporter.run` over all 53 runs of the 41 scenarios (`reporter/test_reporter.py`): the output tree equals the oracle's; the report's files are the package then the snapshot, both holding the snapshot's payload; the writes happen in that order; nothing already written is touched, nothing is logged | pass |
| Failed writes (package directory blocked, snapshot directory blocked): outcome and whole output tree equal the oracle's; the package file exists only in the second case | pass |
| `legacy_compatibility` (`reporter/test_reporter.py`): source and signature identical to the oracle's; every snapshot of every run of every scenario projects like the oracle's (states NOT_APPLIED, APPLIED, REALIZED, UNDER_REALIZED, VALIDATION_FAILED, ROLLED_BACK, MONITORING, DRIFTED, BLOCKED seen); every state, actor and conformance combination, including unmapped states and malformed snapshots, same result or exception | pass |
| Facade (`facade/test_realizer.py`): build context, the lineage entry and the seven stages called once each in order with each result handed on; a failing stage stops the run; `realize` equals `realize_request(...).realization`; every scenario through the request entry equals the oracle's files; the estimator and the reviewer return empty results when nothing is due or the reconciliation halted (even when validation looks due) | pass |
| Re-export check (`facade/test_realizer.py`): the 101 names in the engine namespace before this change (dumped from the module) all resolve; `parse_args`, `main` and `legacy_compatibility` are the moved objects; an unknown name still raises `AttributeError` | pass |
| CLI (`cli/test_cli.py`): no capability package imported (`ast.walk`) and only the facade, contracts, config and common allowed; parse results, help text and argparse errors equal the oracle's; demo, demo-modified with `--print-json`, replay cuts, an observation file with a policy file and a lineage-only run give the same exit status, stdout and output tree as the oracle's `main`; failures are the same uncaught exceptions; load order package, observation, policy; `python -m realizer` and `scripts/run_realizer.py` run the demo (exit 0, the oracle's package id); a missing `--package` exits 2; the console script is declared | pass |
| Mutation check (source edit) | changing the CLI's `--log-level` default fails the option-parity test |
| `tests/test_docs.py` | pass |
| pyflakes `src/realizer` | 76 findings: 75 are the facade's `noqa: F401` re-exports (pyflakes does not read `noqa`) and 1 is the verbatim `actor` local in `legacy_compatibility` |
| Test suite | 1225 tests (1176 before: plus 14 reporter, 18 facade and 17 CLI tests; 4 skip as before), all pass |

### Known oracle behavior kept for parity

- `legacy_compatibility` assigns an `actor` it never reads (the check below it reads the application's actor again).
- The CLI's log format labels local time as `Z`, and an uncaught failure gives a traceback with exit status 1.

Both are recorded for a separately reviewed change.

### Behavior differences

None known. The only observable change is an addition: the `realizer` console script, `python -m realizer` and `scripts/run_realizer.py`. All 41 scenarios, the CLI modes and every compared malformed case behave identically to the oracle.

## Phase 11: oracle retired

### Decision

The oracle is retired on 1:1 fixture parity, as the Publisher's was ([`repo-reorg-parity.md`](../publisher/repo-reorg-parity.md) and design section 21.5 there). That replaces the design's condition (section 22, Phase 11) that representative real source-system runs pass first. The condition cannot be met yet, for two reasons that Phase 11 did not change:

- the Realizer accepts only Publisher packages with `contract_version` 1.0.0 (`validate_package_shape`), while the current Publisher emits a later contract version, so a package from today's Publisher cannot be realized;
- the Realizer has no live collector: it consumes a normalized observation file or the synthetic demo observation (`collector.py` holds only `load_observation` and `demo_observation`), so there is nothing to run against a live source.

Both are semantic changes, outside a behavior-preserving reorganization. A real source-system run is therefore an open item, not a retirement condition.

### Deleted, moved and dropped

| Item | Disposition |
|---|---|
| `legacy/realizer_single_file/realizer.py` and its `contracts/realizer_*.py` | Deleted. Reachable as `git show realizer-reorg-oracle-1.0.0:legacy/realizer_single_file/realizer.py`; `scripts/compare_realizer_with_ref.py --ref realizer-reorg-oracle-1.0.0` runs it |
| `legacy/realizer_single_file/docs/realizer/*.md` (12 documents) | Moved with `git mv`, content kept, a note added naming `src/realizer`: product requirements to `docs/product/realizer/`, context, HLD and LLD to `docs/architecture/realizer/`, the four contracts to `docs/contracts/realizer/`, golden scenarios, release plan, release evidence and readiness review to `docs/proposals/realizer/` (version suffixes dropped from the names) |
| `legacy/realizer_single_file/schema/realization-*.schema.json` | Moved to `schema/`; the unit test pins the builders in `src/realizer/contracts/realization_schema.py` to these files byte for byte |
| `examples/realizer_jsons/` (exact, modified) and `examples/realizer_demo/` | Restored in reduced form as `examples/realizer/`: the two observations byte for byte (`exact/observation.json`, `modified/observation.json`) and a README with the run commands, the expected identities and outcomes and the synthetic-economics note; `tests/component/realizer/test_examples.py` realizes both and asserts them, so the README cannot drift. Not restored: the 70 KB source package (the README points at the byte-identical fixture `tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json`), the generated outputs (pinned in `tests/fixtures/realizer/expected/example-exact/` and `example-modified/`, which store the same RPKG, RSNAP, REVID and RERUN files, checked by `test_example_scenarios_keep_the_documented_identities`), the demo's `publisher_out/` (the v2.8.0 Publisher's outputs) and its `.xlsx` workbook. Verified equal at the start of the phase by `test_example_scenarios_match_committed_examples` (the 1225-test run) |
| `README.md`, `RELEASE_STATUS.md`, `VERSION` (2.8.0), `requirements.txt` of the oracle | Dropped. The still-true facts are in the main `README.md` "Realizer" section (the command, options and output tree), in [`known-limitations.md`](../../reference/realizer/known-limitations.md) (the contract-1.0.0 input, no live collector, no live end-to-end run, no `.xlsx` writer, and the behaviors kept for parity) and in `docs/proposals/realizer/release-evidence.md` (the release status and open gates as of v1.0.0). `jsonschema` is in the repository's own `requirements.txt` |
| `pricing/` and the frozen Publisher-1.0.0 copies `contracts/reporter_contract.py`, `workbook_contract.py`, `reporter_schema.py`, `reporters/workbook_reporter.py` | Dropped: the Realizer reads its package as plain JSON and never imported them on the realize path. The Reporter JSON Contract 1.0.0 code stays readable from the tag (`git show "realizer-reorg-oracle-1.0.0:legacy/realizer_single_file/contracts/reporter_contract.py"`), as the starting point of the 3.x adapter in the known limitations |
| `reporters/realizer_workbook_reporter.py` (the `.xlsx` renderer) | Dropped. It needs the `artifact_tool` runtime, nothing on the realize path calls it, and the workbook projection stays in `src/realizer/contracts/report.py`. Its only test, which skipped when `artifact_tool` was absent, is removed (skipped tests 4 to 3) ([`known-limitations.md`](../../reference/realizer/known-limitations.md)) |
| `tests/regression/realizer/oracle.py`, `altered_engines.py`, `tests/unit/realizer/parity.py` oracle halves | Deleted; `parity.py` became `tests/unit/realizer/helpers.py` (values that cannot be serialized, run trees, staged scenario runs), modular only |
| `scripts/compare_realizer_single_file.py` | Replaced by `scripts/compare_realizer_with_ref.py` |
| Facade compatibility namespace in `src/realizer/realizer.py` | Pruned (below) |

### Tests without the oracle

Every test that loaded the oracle was kept, converted or dropped. Counted over the test methods of the 38 Realizer test files that existed at the start of the phase (403 methods; 381 remain, in the moved files too):

- **Converted (158 test methods).** The comparison with the oracle is replaced by a stored expectation (`tests/realizer_expect.py`, `tests/fixtures/realizer/expectations/<test module>.json`): a short value is stored whole and a long one (the 11,088-case economics grid, the 25,920-case attribution grid, the 4,096-triple relation grid, the 1,440-input downstream rule, the ledger-prefix projections) as its SHA-256 and length plus a few readable spot values. They cover what the 41-scenario baseline cannot: malformed-input exception types and messages for every moved function, the half-written ledger of every failed-run and crash-window case (stored as outcomes, ledger event order and file digests), lazy and `Deferred` behavior, replay cuts, the CLI option table, help text and behavior per mode, and the grids. 664 expectations in 28 files, 302,011 bytes in all (the largest, the analyzer's, is 55 KB).
- **Kept (223 test methods).** Tests that never needed the oracle (pinned values, behavior, mutation checks, the GR-001..GR-040 scenarios, the core suite) or whose oracle half was only a second assertion on a value the test already pinned. The four oracle suites (core, contract, GR-001..GR-040, workbook projection) ran once per engine as a base class and a modular subclass; they now run once.
- **Dropped (22 methods net).** Source-text and signature equality with the oracle, "same object as the facade re-export" checks, the pin of the 101 names of the old engine namespace, oracle-vs-modular duplicates of what the stored baseline already checks (byte-identical output trees and log lines over the 41 scenarios, the oracle's own baseline run, the oracle half of the idempotency and determinism checks, the subprocess determinism runs of the oracle), the `.xlsx` renderer test, and the compare script's oracle mode (`--oracle-only`, `--candidate`, the altered-engine fixtures).

Run count: 1225 tests (4 skipped) before this phase, 1152 now (3 skipped). The change of 73:

| Group | Before | After | Change | Reason |
|---|---:|---:|---:|---|
| Oracle suites (core, contract, GR-001..GR-040, workbook projection) | 110 | 55 | -55 | each ran on both engines; the oracle copy and the renderer skip are gone |
| `test_single_file_compatibility` to `test_stored_baseline` | 14 | 8 | -6 | the oracle's run and the byte-for-byte oracle comparison are gone; the example check pins identities |
| `test_subprocess_determinism` | 6 | 5 | -1 | the oracle runs are gone |
| `test_compare_script` | 12 | 11 | -1 | the oracle mode is gone; the new tests cover refs, difference reporting and the oracle tag |
| `test_update_script` (new) | 0 | 4 | +4 | approved-change guard, expectation module list, size bound |
| Unit tests under `tests/unit/realizer/` | | | -17 | facade re-export, source-text, namespace and oracle-baseline tests |
| `tests/test_docs.py` | 16 | 19 | +3 | links, repo paths and map entries of the Realizer documents |

(The oracle suites include the moved `test_contract_helpers` and `test_workbook_projection`; GR-001..GR-040 are `tests/golden/realizer/test_golden_realization.py`, the core suite is `tests/component/realizer/test_realizer_core.py`.)

Where the stored values came from: before the oracle was deleted, the converted tests were run in a one-off substitution run that replaced every moved function, class and constant by the oracle's same-named object (with oracle-backed stand-ins for the functions only the modular code has, such as the stage `run`s) and recorded their results. All 664 expectations are identical to those results, none is "modular only", and the committed files were recorded from the engine. The scenario baseline `tests/fixtures/realizer/expected/` is unchanged (`git diff master -- tests/fixtures/realizer/expected` is empty). Stored messages that name types in set-iteration order and Python-version-dependent text are normalized in the tests, and the unit tests pass under several `PYTHONHASHSEED` values.

Regeneration, deliberately: `python scripts/update_realizer_goldens.py --approved-change "<why>"` rebuilds the scenario baseline from the engine, and `--expectations` re-runs the converted tests in record mode (deleting the old files first). Review the diff, and bump the Realizer versions in `src/realizer/config/settings.py` when a change affects decisions.

### Facade

`src/realizer/realizer.py` is now 81 lines: `build_context`, `realize_request`, `realize` (the positional entry point) and the policy type and the demo and replay observation loaders that the CLI reaches through it. Removed: the block of re-exports of every name the single-file engine had, `review()`, and the module `__getattr__` that forwarded `parse_args` and `main`. The test that pinned the 101 names is replaced by one that pins the facade's public names. Tests import from the capability modules. `legacy_compatibility` keeps its name (a Reporter v1.0.0 projection, not oracle code) and lost its unused `actor` local. Its output for every snapshot the engine produces is unchanged (the 41 scenarios' stored `runs.json` and the state, actor and conformance grid); the one input whose behavior differs is a malformed snapshot with a state that has no legacy mapping and no application mapping, which used to raise at the unused read and now returns UNREPRESENTABLE (pinned by `test_a_state_without_a_legacy_mapping_does_not_read_the_application`).

### Scripts

`scripts/compare_realizer_with_ref.py --ref <git ref> [--scenario <name>]` checks the ref out into a temporary worktree, runs its Realizer and the working tree's over every scenario in `tests/fixtures/realizer/observations/` in fresh interpreters, and diffs the canonical outputs aspect by aspect. Exit 0 identical, 1 a difference or a crashed run, 2 a bad ref, an unknown scenario or invalid arguments. It runs whichever engine the ref has, so `--ref realizer-reorg-oracle-1.0.0` runs the single-file Realizer (`legacy/realizer_single_file/` in the tag) against the working tree; the manual route is `git worktree add <dir> realizer-reorg-oracle-1.0.0` and `PYTHONPATH=<dir>/legacy/realizer_single_file:<dir>/src python -c "import realizer"`.

### Verification

| Check | Result |
|---|---|
| Compile check (`src tests scripts examples`) | pass |
| Test suite | 1152 tests (1225 before, see above), 3 skipped, all pass |
| `tests/test_docs.py` | pass |
| `git diff master -- tests/fixtures/realizer/expected` | empty (the stored baseline is not regenerated) |
| `scripts/compare_realizer_with_ref.py --ref master` | 41 scenarios identical |
| `scripts/compare_realizer_with_ref.py --ref realizer-reorg-oracle-1.0.0` | 41 scenarios identical to the single-file Realizer |
| Demo `python -m realizer --package PKG-C1928BFAD6E14C166A0F518D.json --demo` | prints `.../realization/packages/RPKG-9BCA3C3DFB12CC37AA7CA775.json`, the committed example ID |
| Oracle verification of the stored expectations | 664 of 664 identical to a run of the oracle, 0 differences |
| pyflakes `src/realizer` | clean |
| `examples/realizer/` (after the phase) | `realizer --package <fixture> --observation examples/realizer/{exact,modified}/observation.json` prints `.../realization/packages/RPKG-9BCA3C3DFB12CC37AA7CA775.json`; the written RSNAP, REVID and RERUN IDs and the outcome fields equal the README, asserted by `tests/component/realizer/test_examples.py` |
| `scripts/validate_contracts.py`, `scripts/validate_skill_suite.py` | pass |

### Definition of done (design section 25)

| Item | Status |
|---|---|
| Architecture: `realizer.py` orchestrates only; one package per capability; Reporter projects only | Met (the facade holds only stage calls, checked on the source) |
| Maintainability: no one-function files, cohesive modules | Met |
| Determinism: same inputs reproduce the same result; replay reproduces the snapshot; duplicate evidence is idempotent | Met, with the recorded exception of a billing restatement reprocessed once (kept from the single-file release, pinned) |
| Lifecycle golden coverage (full and partial application, invalidation, re-evaluation, validation success and failure, drift, rollback, supersession, overlap) | Met by GR-001..GR-040 |
| Economics: no summation across overlap; authoritative package realized value; expected versus realized reconciles; annualization policy-controlled | Met as the single-file release defined them (the cumulative helper `authoritative_cumulative_realization` is off the realize path) |
| Section 10 step, plan and package realization levels | Not met, deliberately: the single-file release has none, and inventing them would be new behavior. The measurement per application episode (candidate or scenario scope) is the closest thing |
| Section 11 reviewer checks (evidence completeness, lifecycle, baseline, attribution, overlap, no-double-count, annualization, consistency) | Not met, deliberately: the Reviewer assembles what the single-file release decided and adds no check that can reject |
| Unit, component, golden and compatibility tests pass | Met |
| Integration tests (collector SQL, source schema, full run against configured source systems) | Not met: there is no live collector and no integration test against a live source |
| Real source-system run | Not met: blocked by the contract-1.0.0 package and the missing live collector (Decision above) |
| Code review completes | Run per phase on the branches before merge; Phase 11 review pending with the merge |
| Release evidence captured | This record, the stored baseline and `docs/proposals/realizer/release-evidence.md` (frozen at v1.0.0) |

## After the reorganization: contract 3.1.0 input (gap review RD01)

The Realizer gap review ([`gap-review.md`](gap-review.md)) retired the contract-1.0.0 input recorded above. The Realizer now reads exactly the Reporter JSON Contract 3.1.0 the Publisher writes (`validate_package` in `src/realizer/config/loader.py`: version, committed schema, recomputed package id and full hash, empty `realization` placeholder, no carried-forward plan).

Fixtures. The two 1.0.0 package fixtures were deleted. `scripts/build_realizer_fixtures.py` rebuilds the package fixtures from real Publisher output: the demo run deterministically (`--demo --flat-output --no-workbook`, fixed demo analysis time) for `wh-demo-006` (`inplace-size-canary`: a READY_FOR_CANARY size cut that invalidates the cluster knobs, a READY auto-stop step, a PENDING_REEVALUATION step, a REMEASURE step and a priced migration alternative with a compatibility step), `wh-demo-009` (`two-ready-steps`: two authority-eligible steps followed by a pending step) and `wh-demo-003` (`unpriced-candidate`), and the package of the golden case `s02-scope-migration-only` (`scope-migration-only`: step-less NO_CHANGE_BASELINE). The script checks each package still has the plan shape it stands for and validates it as the Realizer does; `--check` reports drift from the Publisher's current output.

Scenarios. The 41 stored scenarios were re-pointed: GR-019 at `two-ready-steps` (both authority-eligible steps applied), every other at `inplace-size-canary`; the observed configurations keep the same role per knob (the size step's target, a contrary or a more aggressive size, the pending step's own target, the migration's type); `applied_scenario_id` names the new migration scenario. Observations built on the stock demo economics take the new demo economics (`demo_observation` now sizes its 30-day savings to the applied step's published base savings, so a demo realizes about what the package expected); observations with their own economics are scaled by the ratio of the new to the old denominator, so each keeps its ratio class.

Decision-equivalence check. Every scenario was run on the branch and compared with master's stored outputs (the 1.0.0 fixture) on: implementation conformance, application state and scope, modification relation, validation state and rule results, maturity, realization state and classification detail, source package disposition, recognition status of every measurement, attribution claim status, attribution state and hard disqualifiers, denominator source, ratio class (null, negative, below 0.5, below 0.8, at least 0.8), plan progress state, regeneration status, drift state, the legacy compatibility projection of every run, the number of evidence and regeneration files, the same fields for every intermediate snapshot, and the ordered ledger event-type sequence. Result: 41 of 41 scenarios identical; only ids and amounts differ.

| Final realization state | Scenarios |
|---|---|
| REALIZED | GR-002, 003, 005, 010, 017, 018, 019, 021, 022, 026, 027, 030, 031, 032, 033, 038, 040, example-exact, example-modified, financial-quality-gates, restatement-reprocessed |
| UNDER_REALIZED | GR-011, 012, 028, 029 |
| VALIDATION_FAILED | GR-013, 014, 034 |
| BLOCKED | GR-015 |
| MONITORING | GR-016 |
| APPLIED | GR-006, 008, 009, 037 |
| DRIFTED | GR-023, 024 |
| ROLLED_BACK | GR-025 |
| NOT_APPLIED | GR-001, 007, 020, replay-through-generation |

The single-file oracle (git tag `realizer-reorg-oracle-1.0.0`) reads only 1.0.0 packages, so `scripts/compare_realizer_with_ref.py --ref realizer-reorg-oracle-1.0.0` no longer applies to the stored scenarios; this check is the equivalence record for the move to 3.1.0.

## Gap review batch 1.5: the auto-stop slice and its decision changes

Batch 1.5 of the gap review ([`gap-review.md`](gap-review.md)) added the live path (bridge to the Publisher's collector SQL, measurement evidence, projector, auto-stop replay, billed cross-check), the backtest and the end-to-end golden layer, and changed four rules by decision (RD08, RD09, RD15, RD17). Each rule change was checked with the decision-equivalence check above over the 41 stored scenarios, against the previous commit's stored outputs:

| Change | Scenarios whose decision changed | How |
|---|---|---|
| Observation format 2.1.0, measurement evidence, live path | none | ids only |
| Billed cross-check | none | outputs unchanged |
| Package guardrails (RD09) | 32 | the size canary measured at 8 days is inside its 30-day guard window: CLAIMABLE -> OBSERVABLE_ONLY with `CANARY_GUARD_WINDOW_OPEN`, recognition PROVISIONAL (where already not claimable, the disqualifier is added); GR-013's +7 percent p95 is within the balanced profile's 25 percent (its row now uses +30 to keep testing a p95 failure); GR-003 uses 30 elapsed days to keep testing a claimable exact application |
| Classification against the published range (RD15) | none | every stored ratio class falls on the same side of the published low |
| Qualified origin, approvals (RD17) | none | no stored scenario uses an inferred origin |
| Independent changes with reasons (RD08) | none | non-authority steps now carry their reason in the payload |
| Realizer component versions 1.1.0 | none | ids only |

The end-to-end cases (GE-01..GE-12) and the backtest run on simulated worlds, where the replay equals the oracle (consistency, not accuracy). Two restart-heavy cases disagree with the simulated bill by design: the simulator bills no restart time while the replay charges the package's restart assumption, which is the case the billed cross-check exists to expose.

## Gap review batch 2: plan semantics and its decision changes

Batch 2 of the gap review ([`gap-review.md`](gap-review.md)) made the Realizer read the 3.x plan: step roles, the applied scenario, the denominator for the scope applied, the run scope, cost profile and observed change of the source package, and regeneration evidence. Decisions RD06, RD07, RD10, RD11 and RD12 were taken as recommended (RD06 part (b) moves to batch 4). Each change was checked with the decision-equivalence check over the 41 stored scenarios against the previous commit's stored outputs, and over the end-to-end cases:

| Change | Stored scenarios whose decision changed | End-to-end cases whose decision changed | How |
|---|---|---|---|
| Photon and the channel out of the configuration hash (RD07) | none | none | ids only |
| Step roles; a canary rollback is a safety action (RD06) | none | none | application payloads gain `step_role`; new fixtures `canary-rollback`, `capacity-add-scoped`, `capacity-cut-scoped` |
| Remeasure days from the REMEASURE step; source scope, profile and observed change; regeneration evidence | none | none | fields added; a repeated regeneration request is still suppressed as a duplicate |
| Live windows and billed change for a step without a replay; observation format 2.2.0 | none | GE-13 added | the observation gains `billed_change` |
| Applied scenario inferred; compatibility recorded (RD11) | none | none | applications gain `compatibility_status`, `annotations` |
| Denominator for the scope applied; migration range informational (RD11) | GR-021 | none | the Plan B migration REALIZED -> MONITORING (`MIGRATION_RANGE_INFORMATIONAL`), its ratio still reported |
| Modified auto-stop against the package's option grid (RD12) | none | GE-05, GE-06 | MONITORING -> REALIZED: GE-05 (15 applied where 10 recommended) 9,376.46/yr against 4,681.50 / 7,022.25 / 9,363.00; GE-06 (210 applied where 15 recommended, aggressive) 13,944.69/yr against base 10,448.98 (the balanced package's exact step for 210: 10,476.47) |
| Performance and capacity verdicts (RD10) | none | GE-13; GE-14 added | GE-13 (max clusters 2 -> 5) APPLIED -> PERFORMANCE_VALIDATED: queueing 9.72 -> 0.76 percent, p95 2,500 ms both, billed +12,887.78/yr within 1.25 x the modeled 15,628.48; GE-14 (zero-priced max cut 4 -> 2) CAPACITY_VALIDATED |
| Realizer 1.2.0, contract 1.1.0, component versions | none | none | ids only |

A canary-rollback end-to-end case was not added: the demo simulator ties the C55 p95 regression to the newest configuration epoch, so a world where the rollback is applied still shows it (the unit tests cover the role).

## Gap review batch 3: measurement completeness and its decision changes

Batch 3 of the gap review ([`gap-review.md`](gap-review.md)) checked the cost components of an observation, gave the counterfactual an identity over what the evidence states, and measured a change whose workload shifted. Decision RD19 was taken as recommended (option 1: measure and report, never classify while shifted). Each change was checked with the decision-equivalence check over the 41 stored scenarios against the previous commit's stored outputs, and over the end-to-end cases:

| Change | Stored scenarios whose decision changed | End-to-end cases whose decision changed | How |
|---|---|---|---|
| Cost components reconcile with the realized total within 0.03; an AWS component needs a DBU component and an evidence type; AWS cash or freed commitment only with `CUR_ACTUAL`; a DBU-only caller observation compared with the DBU range | none | none | the GR-038 input observation gained its DBU component (2,563.12 + modeled AWS 50 = 10,000.00 - 7,386.88), which the new check requires; no stored observation is DBU-only |
| Counterfactual id over the method, the window (with the measurement id's fallbacks), the replay target, the assumptions and an evidence fingerprint; the workload no longer hashed; the counterfactual payload states them | none | none | ids only |
| Workload shift (RD19): an auto-stop replay outside 0.75 to 1.25 of the pre-change volume is measured and reported as `MONITORING` / `WORKLOAD_SHIFT`; a guard-judged step waits with the reason stated | none | GE-15 added | GE-15 (the GE-01 change while the volume halves from the change day): before, NOT_READY with nothing measured; now 479.53 in 28 days (6,251.02/yr), `MONITORING`, `WORKLOAD_SHIFT`, claimable; replay DBU 304.81 equals the oracle's |
| Realizer 1.3.0; policy, analyzer gateway and estimator gateway 1.3.0, modeler gateway 1.2.0; contract unchanged (1.1.0) | none | none | ids only |

The demo simulator gained optional `from`/`until` days on a workload component (a world whose workload changes at a date); the Publisher demo's outputs are unchanged (0 canonical differences against master over 30 runs).

## Gap review batch 4: lifecycle and ledger for daily runs and its decision changes

Batch 4 of the gap review ([`gap-review.md`](gap-review.md)) made the Realizer hold up under daily runs and several packages per warehouse: one owner per change across lineages, supersession and successors, closed episodes, rollback read from the configuration, plan progress, realized to date, corrections of re-measured windows, ordering by parsed time and append order, and a ledger lock. Decisions RD20 to RD23 were taken as recommended (RD22 with per-step episodes, RD23 without new SQL; see gap-review.md); RD06 part (b) as decided at batch 2. Each change was checked with the decision-equivalence check over the 41 stored scenarios against the previous commit's stored outputs, and over the end-to-end cases:

| Change | Stored scenarios whose decision changed | End-to-end cases whose decision changed | How |
|---|---|---|---|
| Ledger order by parsed recorded time, then append order; times compared as instants | none | none | snapshot ids, checkpoint hashes and timeline order move where entries share a recorded time |
| A re-measured window with other economics is a correction of that window's earlier measurement; one correction per key and version; unchanged files are not rewritten | restatement-reprocessed | none | the reprocessed restatement records nothing new: one correction instead of two (former known limitation 7) |
| Advisory lock per ledger for a run; a truncated last line quarantined | none | none | no stored scenario has a truncated line |
| Rollback read from the configuration (a contradicting flag is refused); a closed episode never absorbs a new application; drift and rollback dated at their epoch boundary | GR-024, GR-025 | none | GR-025's return to the source size is a rollback only: no drift entry, no drift regeneration, drift section `NONE`; GR-024's drift input moved to a knob no step changes (min clusters 2; size LARGE is now a rollback): still `DRIFTED`, plus the independent change it records |
| Plan progress (RD22): a later plan step is its own application, a held step applied early is an independent change with a `PENDING_STEP_APPLIED` regeneration, only unexplained changes drift; independent changes recorded once | GR-023 | none | GR-023's drift input applied the held max-clusters step (now an independent change, not a drift); moved to min clusters 2, so it still tests the drift boundary |
| Savings after a rollback or drift (RD21): realized to date kept and claimable for the closed interval, no run-rate, no ratio | GR-023, GR-024, GR-025 | none | annualized and ratio null after the closing event (ratio class `>=0.8` to none) |
| Realized to date over every measurement of the episode (RD23), coverage stated, `MATURE` at 365 covered days; observation format 3.0.0 without the caller's to-date and TTM labels | GR-040 | none | GR-040 is now two 200-day runs: realized to date 24,000.00, TTM 22,600.00 (the latest 365 covered days), run-rate 29,200.00, `MATURE`, `REALIZED`; a caller's TTM no longer makes a single run mature |
| One owner per change across a warehouse's lineages (RD20), same-epoch supersession, successor link, cross-lineage canary rollback (RD06 b), churn rule and PolicyDiff on the run path, a self-replacing package refused | GR-034 | none | GR-034 records a `POLICY_DIFF_RECORDED` entry (p95 limit 5 to 4); every run also keeps its source package copy, the policy settings and the warehouse's state index, and the realization package states `successor_package_id` |
| Realizer 1.4.0; projector 1.2.0, policy 1.4.0, analyzer and estimator gateways 1.4.0, attribution 1.3.0, exclusivity 1.1.0; realization package contract 1.2.0, ledger envelope 1.1.0 | none | none | ids only |

New package fixtures from dated Publisher demo runs (`autostop-daily-oct02`, `autostop-daily-oct03`, `successor-after-autostop`, `canary-before-cut`) back the cross-lineage tests; the existing fixtures and the Publisher's outputs are unchanged.
