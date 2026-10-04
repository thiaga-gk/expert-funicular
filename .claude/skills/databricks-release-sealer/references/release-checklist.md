# Release Checklist

## Code
- no transitional duplicate public-report model
- no fail-open unsupported-type path
- no dead workbook/report serializer
- no unused compatibility write path
- no stale generated caches

## Contracts/reporting
- reporter contract version consistent
- workbook contract version consistent
- JSON validates semantically + schema
- workbook projection-only

## Docs
- Design docs current: each topic stated once in its home, no contradiction with code or contracts
- source/technique/threshold/economics references current
- README/CLAUDE version and execution rules current
- known limitations contain only unresolved truths

## Security
- OAuth/read-only policy enforced
- secrets/tokens/raw SQL not logged
- no mutation APIs in advisory release

## Evidence
- compile pass
- full tests pass
- golden scenarios pass
- synthetic fleet pass
- deterministic rerun pass
- workbook QA pass
- clean extracted ZIP pass
- live-source E2E pass or explicitly NOT_EXECUTED with reason
- `/code-review low` evidence when available
