# Deterministic Identity

Use canonical JSON serialization with sorted keys and normalized scalars before hashing.

Recommended identities:
- `EPOCH-*` / `REGIME-*`: stable resource identity + authoritative config + start/change boundary
- `EVID-*`: semantic metric/evidence payload + source/provenance/window
- `CAND-*`: resource + regime + technique + current/recommended value + policy/optimizer/estimator versions + referenced evidence fingerprint
- `SCN-*`: ordered candidate/step IDs + scenario type + bundle estimator version
- `REAL-*`: package/scenario/candidate + measurement window + applied config + realization method version
- `PKG-*`: contract version + resource + regime + canonical config + evidence fingerprint + all semantic engine versions + ordered candidate/scenario IDs

Exclude volatile timestamps, filenames, workbook formatting, logs, and explanation prose from IDs.
