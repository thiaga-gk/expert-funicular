# Publisher → Realizer Synthetic Demo — v2.8.0

This directory contains a deterministic synthetic end-to-end example for the integrated product v2.8.0 / Realizer v1.0.0.

Current exact example identities:

```text
PKG   PKG-C1928BFAD6E14C166A0F518D
RPKG  RPKG-9BCA3C3DFB12CC37AA7CA775
RSNAP RSNAP-B4E0622B53B53930421A90E3
```

Expected outcome:

```text
implementation_conformance = EXACT
validation_state            = PASSED
maturity                    = REPRESENTATIVE
realization_state           = REALIZED
denominator_source          = PUBLISHER_CANDIDATE
```

`publisher-realizer-workbook.xlsx` contains the locked Publisher Workbook v1.1.0 views plus the separately versioned Realizer Workbook Extension v1.0.0.

For side-by-side exact and modified JSON artifacts, see `../realizer_jsons/`.

All economics in this example are synthetic and must not be interpreted as production value claims.
