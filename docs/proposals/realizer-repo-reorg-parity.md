# Realizer reorganization: parity record

Status: **Phases 0-4 complete** (freeze current behavior; extract contracts; extract config and common; extract analyzer and collector; extract governor). The single-file Realizer is the behavioral oracle for the modular `src/realizer` rewrite. Design, phases and implementation rules: [`realizer-repo-reorg-design.md`](../architecture/realizer-repo-reorg-design.md) sections 21.4, 22 and 24.

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
