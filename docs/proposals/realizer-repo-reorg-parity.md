# Realizer reorganization: parity record

Status: **Phases 0-7 complete** (freeze current behavior; extract contracts; extract config and common; extract analyzer and collector; extract governor; extract reconciler; extract estimator; extract reviewer). The single-file Realizer is the behavioral oracle for the modular `src/realizer` rewrite. Design, phases and implementation rules: [`realizer-repo-reorg-design.md`](../architecture/realizer-repo-reorg-design.md) sections 21.4, 22 and 24.

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
