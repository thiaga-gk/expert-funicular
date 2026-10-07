---
name: databricks-release-sealer
description: Perform the final release-hardening and packaging gate for a Databricks optimization publisher. Use this skill whenever a publisher is believed ready for testing/merge/release and needs stale/legacy code cleanup, docs/code/version reconciliation, optimization-framework/constraint alignment, dead-file scans, secret/read-only audits, workbook/report checks, deterministic package verification, clean archive extraction tests, release evidence, final hashes, or merge readiness. Trigger it after independent validation, not as a substitute for design, implementation or adversarial testing of decisions (databricks-publisher-validator).
---

# Databricks Release Sealer

Act as the final release gate. Prefer deletion of superseded artifacts over retaining contradictory legacy paths.

Read `references/release-checklist.md` and the sibling orchestrator's `references/optimization-thinking-framework.md`. Load approved artifacts and validator findings. Do not seal with unresolved Critical/High findings.

## Procedure

1. Verify release worktree/branch scope and version.
2. Scan code/repo/docs for stale version strings, legacy/deferred TODO paths, duplicate architecture docs, dead serializers/reporters, obsolete contracts, generated caches, large accidental artifacts, and contradictory instructions.
3. Ensure code, the design docs and the contracts tell one story: each topic stated once in its home, with no contradictions.
4. Verify the six-part optimization framework is represented consistently in service/system-model, optimization policy, active techniques, runtime constraint handling, validation/golden tests, and release evidence.
5. Ensure supported-type behavior fails closed and known limitations list only true unresolved limitations.
6. Run compile + full tests + golden suite + deterministic rerun + policy/constraint checks + contract/schema validation + synthetic E2E.
7. Validate workbook/report artifacts: exact sheets/grains/columns, numeric signed financials, no business formulas/macros/hidden logic, no decorated text in numeric cells, IDs preserved as text, no formula errors.
8. Audit security/read-only boundaries and logs for credentials/raw SQL/sensitive payloads.
9. When the release ships as an archive, build it deterministically and compute SHA-256; then extract it to a clean directory and compile/test/run the synthetic publisher from the extracted copy. A repository-only release (no archive) skips this step and says so in the evidence.
10. Run `scripts/release_audit.py <repo>` (tracked files only; add repository-specific stale strings with `--pattern`).
11. Write `release/release-evidence.yaml` with commands, results, hashes, framework coverage, known limitations, E2E status, and merge recommendation.
12. Run `/code-review low` before merge when available.
13. Do not merge automatically; present evidence and wait for the repository's completion/approval gate.

## In the SQL Warehouse exemplar repo

Use the repository's own checks instead of inventing new ones: the change loop in `CLAUDE.md` (compile, full suite, demo plus `scripts/validate_workbook.py`, the `git grep` for stale versions), `scripts/validate_contracts.py`, `scripts/validate_skill_suite.py`, `tests/test_docs.py`, `scripts/compare_with_ref.py --ref <base>` for the Publisher and `scripts/compare_realizer_with_ref.py --ref <base>` plus `scripts/update_realizer_e2e_goldens.py --check` for the Realizer. Record each command and its result in the release evidence.

## Clean-repo principle

Keep only current canonical docs/contracts plus supporting references that add distinct value. If two files explain the same architecture with different versions, consolidate/delete rather than relying on readers to choose.
