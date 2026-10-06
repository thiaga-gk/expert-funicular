# Release Status — Databricks SQL Warehouse Optimizer v2.8.0

**Product release:** `2.8.0`  
**Realizer component:** `1.0.0`  
**Publisher design lineage:** v2.7 baseline  
**Status:** **OFFLINE RELEASE CANDIDATE COMPLETE**

## Green

- Publisher v2.8 regression, including pending-step pricing boundary.
- Realizer single-file Phase-1 implementation.
- Realizer contracts + JSON Schemas.
- GR-001..GR-040: **40/40 PASS**.
- Full automated suite: **63/63 PASS**.
- Exact and modified application paths.
- Deterministic ledger/replay/idempotency.
- Financial quality gates and same-scope denominators.
- Billing correction, drift, rollback, supersession and regeneration.
- Cumulative no-double-counting / economic exclusivity.
- Legacy Reporter compatibility boundary.
- Workbook v1.1.0 + Realizer Workbook Extension v1.0.0.
- Synthetic Publisher→Realizer exact and modified JSON examples.
- Dedicated future Phase-2 docs under `docs/phase2/`.

## Open external / connected-runtime gates

- Realizer direct live observation collection: **NOT YET WIRED INTO `realizer.py`**; current engine accepts the normalized observation contract. A connected Databricks adapter/E2E remains required before production readiness.

- Live Databricks source-system E2E: **NOT RUN — no credentials/profile available in this environment**.
- Actual Git release worktree/merge evidence: **NOT PROVABLE — distributed snapshot contains no `.git` metadata**.
- Organizational `/code-review low`: **NOT RUN in this environment**.

Do not represent the external gates as passed. Complete them in the connected development environment before production merge/GA evidence.

See `docs/realizer/release-evidence-v1.0.0.md` and `docs/release-notes-v2.8.0.md`.
