---
name: databricks-release-sealer
description: Perform the final release-hardening and packaging gate for a Databricks optimization publisher. Use this skill whenever a publisher is believed ready for testing/merge/release and needs stale/legacy code cleanup, docs/code/version reconciliation, optimization-framework/constraint alignment, dead-file scans, secret/read-only audits, workbook/report checks, deterministic package verification, clean ZIP extraction tests, release evidence, final hashes, or merge readiness. Trigger it after independent validation, not as a substitute for design or implementation.
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
9. Build a deterministic release archive where practical and compute SHA-256.
10. Extract the actual archive to a clean directory; compile/test/run the synthetic publisher from the extracted copy.
11. Write `release/release-evidence.yaml` with commands, results, hashes, framework coverage, known limitations, E2E status, and merge recommendation.
12. Run `/code-review low` before merge when available.
13. Do not merge automatically; present evidence and wait for the repository's completion/approval gate.

## Clean-repo principle

Keep only current canonical docs/contracts plus supporting references that add distinct value. If two files explain the same architecture with different versions, consolidate/delete rather than relying on readers to choose.
