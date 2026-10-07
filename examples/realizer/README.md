# Publisher to Realizer example

A deterministic, synthetic example of the Realizer: one Publisher package and two normalized post-change observations, one that applies the recommended step exactly and one that applies a modified one. Each observation is a file here; the package is a test fixture.

| File | What it is |
|---|---|
| `tests/fixtures/realizer/packages/inplace-size-canary.json` | The source RecommendationPackage (Reporter JSON Contract 3.1.0, the only version the Realizer accepts, built from a real Publisher run by `scripts/build_realizer_fixtures.py`). |
| `exact/observation.json` | The recommended step applied as recommended. The same observation `--demo` generates. |
| `modified/observation.json` | A different size applied (SMALL instead of the recommended step 1, LARGE to MEDIUM). The same observation `--demo-modified` generates. |

All economics in this example are synthetic and must not be read as production value claims.

## Run it

From a checkout, with the package installed (`pip install -e .`) or `PYTHONPATH=src`:

```bash
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json \
  --observation examples/realizer/exact/observation.json --output-dir ./realizer_out

realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json \
  --observation examples/realizer/modified/observation.json --output-dir ./realizer_out_modified
```

`--demo` and `--demo-modified` generate the same two observations from the package, so these produce the same files:

```bash
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json --demo --output-dir ./realizer_out
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json --demo-modified --output-dir ./realizer_out_modified
```

Each run prints the path of the written realization package, `<output-dir>/realization/packages/RPKG-976E6C1C6E88CE76321758ED.json`; `--print-json` prints the snapshot instead. The other files go under `<output-dir>/realization/` (`ledger/`, `evidence/`, `regeneration/`, `snapshots/`). Use a separate output directory per variant: both runs append to the same ledger (`RPKG-...jsonl`) when they share one. The generated files are not committed; `tests/fixtures/realizer/expected/example-exact/` and `example-modified/` hold the pinned outputs.

## Expected result

Both variants realize the same package, so the realization package ID is the same; the snapshot, evidence and regeneration IDs differ.

### exact

```text
RPKG  RPKG-976E6C1C6E88CE76321758ED
RSNAP RSNAP-A64F3EAB64DE9F38D87F0286
REVID REVID-37F7F275591908182339F44B
RERUN RERUN-46E927B89BA067DACFE23007
```

```text
implementation_conformance = EXACT
validation_state            = PASSED
maturity                    = REPRESENTATIVE
realization_state           = REALIZED
denominator_source          = PUBLISHER_CANDIDATE
```

### modified

```text
RPKG  RPKG-976E6C1C6E88CE76321758ED
RSNAP RSNAP-7C60B0F40D19E981310B4AE3
REVID REVID-0054A9DB9BEB251DB6B705DA
RERUN RERUN-421CD2BEF5C476EECF5E45FA
```

```text
implementation_conformance = MODIFIED
validation_state            = PASSED
maturity                    = REPRESENTATIVE
realization_state           = REALIZED
denominator_source          = REESTIMATED_APPLIED_SCOPE
```

`implementation_conformance` is read from the snapshot's `application`, `validation_state` and `realization_state` from its `current_state`, `maturity` from `maturity.state` and `denominator_source` from `economics`. A modified application always uses a denominator re-estimated for the applied scope, never the Publisher's candidate ([`ledger-contract.md`](../../docs/contracts/realizer/ledger-contract.md)).

`tests/component/realizer/test_examples.py` realizes both examples and asserts these identities and outcomes, so this page cannot drift from the engine.
