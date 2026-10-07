# Publisher to Realizer example

A deterministic, synthetic example of the Realizer: one Publisher package and two normalized post-change observations, one that applies the recommended step exactly and one that applies a modified one. Each observation is a file here; the package is a test fixture.

| File | What it is |
|---|---|
| `tests/fixtures/realizer/packages/inplace-size-canary.json` | The source RecommendationPackage (Reporter JSON Contract 3.1.0, the only version the Realizer accepts, built from a real Publisher run by `scripts/build_realizer_fixtures.py`). |
| `exact/observation.json` | The recommended step applied as recommended. |
| `modified/observation.json` | A different size applied (SMALL instead of the recommended step 1, LARGE to MEDIUM). |

The two observations are fixed synthetic fixtures (their economics are sized to the step's published savings), not measurements, and must not be read as production value claims.

## Run it

From a checkout, with the package installed (`pip install -e .`) or `PYTHONPATH=src`:

```bash
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json \
  --observation examples/realizer/exact/observation.json --output-dir ./realizer_out

realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json \
  --observation examples/realizer/modified/observation.json --output-dir ./realizer_out_modified
```

`--demo` and `--demo-modified` do not read these files: they measure the package's warehouse (a Publisher demo-fleet warehouse) in its simulated world, with the recommended step (or, modified, SMALL) applied a week after the package was generated, through the same path as a live run, and keep the measurement evidence in the state, under `<output-dir>/state/realization/measurements/`, for `--from-evidence`. The demo measures this package's size change (LARGE to MEDIUM) by pricing the 28 settled days at the old size: realized 2,821.88 in the window, 36,785.22 a year against the published 20,493.89 to 35,021.21, billed cross-check `AGREES`, realization state `REALIZED`. Its numbers come from the simulated world, not from the observation files below, and differ from the examples.

```bash
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json --demo --output-dir ./realizer_demo
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json --demo-modified --output-dir ./realizer_demo_modified
```

More forms, commands only (the outputs are not pinned here):

```bash
# Replay the measurement evidence the demo (or a live run) wrote, to the same observation, offline:
realizer --package tests/fixtures/realizer/packages/inplace-size-canary.json \
  --from-evidence ./realizer_demo/state/realization/measurements/<file>.json --output-dir ./realizer_replay

# A fleet run over every warehouse of a Publisher run (a demo-fleet run, here), with run manifest and realization workbook:
publisher --demo --output-dir ./demo_out
realizer --publisher-run ./demo_out --demo --output-dir ./realizer_fleet

# Past auto-stop changes, no package, on a simulated fleet:
realizer --backtest --demo --output-dir ./realizer_backtest
```

Each run prints the path of the written realization package in its run folder, `<output-dir>/run_date=YYYY-MM-DD/run_ts=YYYYMMDDTHHMMSSZ/realization/packages/RPKG-976E6C1C6E88CE76321758ED.json` (with `--flat-output`, `<output-dir>/realization/packages/...`); `--print-json` prints the snapshot instead. The snapshot and the run log `realizer.log` are in the same run folder; the history goes under `<output-dir>/state/realization/` (`ledger/`, `evidence/`, `regeneration/`, `measurements/`, `policies/`, `sources/`, `state/`). Use a separate output directory per variant: both runs append to the same ledger (`RPKG-...jsonl`) when they share a state. The generated files are not committed; `tests/fixtures/realizer/expected/example-exact/` and `example-modified/` hold the pinned outputs.

## Expected result

Both variants realize the same package, so the realization package ID is the same; the snapshot, evidence and regeneration IDs differ.

### exact

```text
RPKG  RPKG-976E6C1C6E88CE76321758ED
RSNAP RSNAP-01752AB27669B07E544B2B5D
REVID REVID-37F7F275591908182339F44B
RERUN RERUN-B1F60D50B9002716EC15E4DE
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
RSNAP RSNAP-B8A90CA3DC90055A4E7D4A8E
REVID REVID-0054A9DB9BEB251DB6B705DA
RERUN RERUN-4A94ACD1AFCA25E7D3EA709A
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
