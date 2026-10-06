# Realizer reorganization: parity record

Status: **Phase 0 (freeze current behavior) complete.** The single-file Realizer is the behavioral oracle for the modular `src/realizer` rewrite. Design, phases and implementation rules: [`realizer-repo-reorg-design.md`](../architecture/realizer-repo-reorg-design.md) sections 21.4, 22 and 24.

## Phase 0: what is frozen

| Item | Where |
|---|---|
| Oracle: the v1.0.0 single-file Realizer (`realizer.py`, its `contracts/`, `reporters/`, `schema/`, docs and examples), moved unchanged from the root `realizer/` folder so that no root-level `realizer/` shadows the future `src/realizer` package | `legacy/realizer_single_file/` |
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
