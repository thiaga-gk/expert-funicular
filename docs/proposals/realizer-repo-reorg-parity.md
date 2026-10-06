# Realizer reorganization: parity record

Status: **Phases 0-2 complete** (freeze current behavior; extract contracts; extract config and common). The single-file Realizer is the behavioral oracle for the modular `src/realizer` rewrite. Design, phases and implementation rules: [`realizer-repo-reorg-design.md`](../architecture/realizer-repo-reorg-design.md) sections 21.4, 22 and 24.

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

The oracle reads no wall clock: ledger times, `as_of_utc`, IDs and lineage derive from the package and the observation, and it writes no paths into its files. Runs into different output directories, time zones and hash seeds produce identical canonical outputs, so no field needed normalization; the baseline still replaces the output directory with `<out>` should it ever appear. The workbook `.xlsx` is not in the baseline: the renderer needs the `artifact_tool` runtime and `RecommendationPackage` objects of the retired publisher, which the frozen JSON fixtures cannot rebuild; its test skips when `artifact_tool` is absent.

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

Still in the working copy, for the phases that own them: `RealizerPolicy`, `policy_to_dict` (governor); `normalize_scalar`, `config_hash`, `photon_bool_from_package`, `package_config`, `observed_config`, `scenario_by_id`, `recommended_scenario`, `candidate_by_id`, `candidate_steps` (analyzer); `make_cf_id` (estimator); and the matching, lifecycle, economics, attribution, ledger, snapshot, reporting, demo and CLI functions.

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
