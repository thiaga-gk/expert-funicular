# Realizer Phase-1 Implementation & Release Plan v1.0.0

> Frozen record of the v1.0.0 release, written when the Realizer was a single-file implementation (`realizer.py`). The code it describes is now `src/realizer/`; that single-file implementation is kept only as the git tag `realizer-reorg-oracle-1.0.0`. Paths and commands below are those of the release and are not maintained; see [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

> The paths this plan cites (`docs/realizer/`) are in the tag `realizer-reorg-oracle-1.0.0` under `legacy/realizer_single_file/` (now moved to `docs/<type>/realizer/`, see [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md)); read one with `git show "realizer-reorg-oracle-1.0.0:legacy/realizer_single_file/<path>"`. `docs/phase2/migration-release-plan.md` is not in the tag or anywhere in this repository's history.

**Status:** LOCKED FOR EXECUTION  
**Baseline:** Product v2.8.0 repository (evolved from the Publisher v2.7.0 baseline)  
**Release:** `REL-REALIZER-1.0.0`  
**Worktree:** `release/sqlwhopt-v2.8.0-realizer-v1.0.0` from `develop`

## 1. Delivery strategy

One Phase-1 product release/worktree with sequential epics. Keep `publisher.py` and `realizer.py` single-file cores. Each epic has a local evidence gate; final merge occurs only after full E2E/reconciliation/review evidence.

## 2. Epic order

| Epic | Scope | Primary goldens | Exit evidence |
|---|---|---|---|
| E0 | Repo/contract alignment | structural | Publisher regression, schema baselines |
| E1 | Realizer contract + deterministic identities/ledger/projection | GR-001,032,033 | contract/idempotency/replay |
| E2 | Application matching + plan authority | GR-002..007,017,018,020 | exact/modified/pending tests |
| E3 | Readiness + post-change validation | GR-008,009,013,014 | readiness/guardrail tests |
| E4 | M08 + Estimator REALIZED + denominator | GR-010..016,028,029 | normalized realized economics |
| E5 | Attribution + economic exclusivity | GR-019,021,030,031,038,039 | no-double-counting/claimability |
| E6 | Corrections + drift + rollback + supersession/regeneration | GR-022..027,034..037 | append/replay/handoff tests |
| E7 | Reporter/Workbook compatibility extension | workbook assertions | legacy + canonical projection tests |
| E8 | Full E2E, financial reconciliation, release hardening | all | live/synthetic E2E, `/code-review low` |

## 3. E0 — Repo and Publisher alignment

### Stories

- E0-S1 Create `docs/realizer/*` (in the tag under `legacy/realizer_single_file/docs/realizer/`), contracts/schema/test locations in existing repo.
- E0-S2 Align `WORKBOOK_CONTRACT_VERSION` implementation with locked v1.1.0 without semantic change.
- E0-S3 Add explicit Realizer extension notice to README/CLAUDE.
- E0-S4 Fix Publisher authoritative Plan-A scenario pricing so pricing stops at first invalidating/canary boundary; downstream pending steps become contingent/unpriced for authoritative plan economics.
- E0-S5 Add Publisher regression proving pending downstream economics are not included in authoritative Plan-A savings.

### Gate

Existing Publisher tests + new pricing regression PASS. Package IDs only change when the fixed semantic payload legitimately changes; document expected fixture updates.

## 4. E1 — Contract, ledger and projection

### Stories

- Implement `contracts/realizer_contract.py` frozen dataclasses/enums/validation.
- Implement `contracts/realizer_schema.py` + JSON schemas.
- Implement canonical JSON, Decimal normalization and ID helpers in `realizer.py`.
- Implement local JSONL ledger append-if-absent/checkpoint/replay.
- Implement deterministic RPKG/RSNAP projection.

### Gate

GR-001/032/033, schema validation, duplicate suppression and byte/semantic replay PASS.

## 5. E2 — Application matching

### Stories

- Parse PKG/scenario/plan-step authority.
- Collect/accept observed config snapshots.
- Implement numeric/size/boolean/semantic comparators.
- Implement EXACT/MODIFIED/NOT_APPLIED/CONTRARY/UNRELATED.
- Keep intent and origin separate.
- Block stale pending-step authority.
- Derive plan progress separately.

### Gate

GR-002..007/017/018/020 PASS.

## 6. E3 — Readiness and validation

### Stories

- PolicySnapshot support for 7d/100 queries/representative regime/evidence quality.
- Low-frequency workload wait semantics.
- Analyzer post-change gateway/adaptor.
- Persist rule-level guardrail evidence.
- Hard failure -> VALIDATION_FAILED while preserving observed economic diagnostics.

### Gate

GR-008/009/013/014 PASS.

## 7. E4 — Realized economics

### Stories

- M08 `REALIZATION_COUNTERFACTUAL` gateway.
- Estimator REALIZED gateway.
- Actual corrected DBU + attributable AWS cost evidence.
- Exact-scope denominator resolver.
- Modified-target re-estimation.
- Annualization maturity gate.
- Signed negative value.

### Gate

GR-010..016/028/029 PASS and financial equation reconciliation PASS.

## 8. E5 — Attribution/exclusivity

### Stories

- Origin attribution and claimability.
- Separate observed vs recommendation-attributed KPIs.
- Scenario-level attribution for inseparable compatible changes.
- Economic atom ownership and overlap blocking.
- Multi-step authoritative cumulative realization.
- AWS modeled-economic vs cash distinction.

### Gate

GR-019/021/030/031/038/039 PASS; portfolio overlap = 0.

## 9. E6 — Corrections/lifecycle/regeneration

### Stories

- Billing retraction/restatement chain.
- Bitemporal as-known-then/current-corrected projections.
- Configuration/workload/regime/policy/financial/source-quality drift handling.
- Rollback requested vs observed.
- Half-open handoff intervals.
- Supersession without automatic economic handoff.
- Actual-state regeneration request and replacement PKG linkage.

### Gate

GR-022..027/034..037 PASS.

## 10. E7 — Reporting compatibility

### Stories

- Implement explicit legacy compatibility result `LOSSLESS|LOSSY|UNREPRESENTABLE`.
- Never fabricate actor or inject REVID into PKG evidence.
- Preserve legacy Workbook v1.1.0 path unchanged.
- Add separately versioned Realizer workbook extension projection driven by PKG+RPKG.
- Add recommended vs actual, realization/maturity/attribution/economics/regeneration fields.

### Gate

Legacy workbook regression PASS; Realizer projection tests PASS; workbook performs no business recomputation.

## 11. E8 — E2E/release

### Required runs

```text
py_compile
Publisher unit/regression
Realizer contract/unit/component
GR-001..GR-040
synthetic Publisher→Realizer→replacement-Publisher
live read-only source validation where credentials/source are available
financial reconciliation
Reporter/Workbook compatibility
replay determinism
/code-review low
```

### Final release evidence

- exact commands/results;
- Python/component versions;
- package/RPKG/checkpoint hashes;
- source mode/region;
- golden matrix;
- financial reconciliation totals;
- known limitations;
- code-review findings/resolution.

## 12. Merge gate

Merge `release/sqlwhopt-v2.8.0-realizer-v1.0.0` to `develop` only when E0-E8 pass. Do not start Product Phase 2 as runtime authority before the Phase-1 gate is complete.

## 13. Phase-2 handoff

This plan ends at the Phase-1 release gate. The subsequent scale-out is governed by `docs/phase2/migration-release-plan.md` (a path that never existed in this repository or in the tag); do not mix Phase-2 implementation into the Phase-1 release worktree.
