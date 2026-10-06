# Publisher to Realizer example

A deterministic, synthetic example of the Realizer: one frozen Publisher package and two normalized post-change observations, one that applies the recommended step exactly and one that applies a modified one. Each observation is a file here; the package is a test fixture.

| File | What it is |
|---|---|
| `tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json` | The source RecommendationPackage (Reporter JSON Contract 1.0.0, the version the Realizer accepts). |
| `exact/observation.json` | The recommended step applied as recommended. The same observation `--demo` generates. |
| `modified/observation.json` | A different size applied (SMALL instead of the recommended MEDIUM). The same observation `--demo-modified` generates. |

All economics in this example are synthetic and must not be read as production value claims.

## Run it

From a checkout, with the package installed (`pip install -e .`) or `PYTHONPATH=src`:

```bash
realizer --package tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json \
  --observation examples/realizer/exact/observation.json --output-dir ./realizer_out

realizer --package tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json \
  --observation examples/realizer/modified/observation.json --output-dir ./realizer_out_modified
```

`--demo` and `--demo-modified` generate the same two observations from the package, so these produce the same files:

```bash
realizer --package tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json --demo --output-dir ./realizer_out
realizer --package tests/fixtures/realizer/packages/PKG-C1928BFAD6E14C166A0F518D.json --demo-modified --output-dir ./realizer_out_modified
```

Each run prints the path of the written realization package, `<output-dir>/realization/packages/RPKG-9BCA3C3DFB12CC37AA7CA775.json`; `--print-json` prints the snapshot instead. The other files go under `<output-dir>/realization/` (`ledger/`, `evidence/`, `regeneration/`, `snapshots/`). Use a separate output directory per variant: both runs append to the same ledger (`RPKG-...jsonl`) when they share one. The generated files are not committed; `tests/fixtures/realizer/expected/example-exact/` and `example-modified/` hold the pinned outputs.

## Expected result

Both variants realize the same package, so the realization package ID is the same; the snapshot, evidence and regeneration IDs differ.

### exact

```text
RPKG  RPKG-9BCA3C3DFB12CC37AA7CA775
RSNAP RSNAP-B4E0622B53B53930421A90E3
REVID REVID-C82136407213010727CEAB98
RERUN RERUN-5EAFEBC878ED2364E53E171A
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
RPKG  RPKG-9BCA3C3DFB12CC37AA7CA775
RSNAP RSNAP-709E1EC13A10E423918D76AB
REVID REVID-783EE1BC18D972E51EBFC4AE
RERUN RERUN-BE6D14C9D53D954C51BE6C47
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
