# Realizer Cross-Document Readiness Review v1.0.0

> Frozen record of the v1.0.0 release, written when the Realizer was a single-file implementation (`realizer.py`). The code it describes is now `src/realizer/`; that single-file implementation is kept only as the git tag `realizer-reorg-oracle-1.0.0`. Paths and commands below are those of the release and are not maintained; see [`repo-reorg-parity.md`](repo-reorg-parity.md).

**Status:** COMPLETE — OFFLINE PHASE-1 RELEASE CANDIDATE READY  
**Review date:** 2026-10-03  
**Review scope:** Realizer JSON/Ledger, Source/Evidence, Policy, Goldens, PRD, HLD, LLD, Context, Reporter/Workbook compatibility, Product v2.8.0 repository, evolved from the Publisher v2.7.0 baseline, plus implemented Phase-1 Realizer.

## 1. Overall readiness

| Domain | Readiness |
|---|---:|
| Product semantics | 100% |
| PKG/RPKG lineage | 100% |
| Application semantics | 100% |
| Validation/counterfactual economics | 100% |
| Attribution/exclusivity | 100% |
| Corrections/drift/rollback | 100% |
| Phase-1 implementation strategy | 100% |
| Reporter compatibility design | 100% |
| Offline implementation alignment | 100% |
| Live Databricks source-system evidence | **EXTERNAL GATE — NOT RUN HERE** |
| Organizational Git worktree + `/code-review low` | **EXTERNAL GATE — NOT RUN HERE** |

**Conclusion:** all known product/design ambiguities are closed and the offline Phase-1 implementation is green. No architecture redesign is required. Production merge remains gated by live source-system evidence and the organization's actual Git/code-review workflow.

## 2. Locked decisions verified

1. Product v2.8.0 is the integrated Phase-1 release; Publisher design lineage is v2.7.0.
2. `publisher.py` remains a one-file fast-iteration core.
3. `realizer.py` is a second one-file fast-iteration core.
4. PKG is immutable; RPKG is separate.
5. Ledger is append-only authority; RSNAP is derived.
6. `EXACT|MODIFIED|NOT_APPLIED|CONTRARY|UNRELATED` supersedes old atomic partial semantics for current Publisher steps.
7. Modified actual state can realize savings and uses actual-scope economics.
8. Hard guardrails dominate savings.
9. M08 uses old state under actual post-change workload.
10. Estimator REALIZED owns money; Realizer owns classification/claimability.
11. Exact applied scope owns the denominator.
12. Pending reevaluation never gets stale economics.
13. Observed and recommendation-attributed realization remain separate.
14. Supersession is not economic handoff.
15. Corrections append; negative outcomes remain visible.
16. Phase 2 applies to Publisher + Realizer together after Phase-1 proof.

## 3. Prior findings — final disposition

### R-001 — Workbook contract version mismatch — **RESOLVED**

`contracts/workbook_contract.py` now declares Workbook Contract `1.1.0`, matching the locked specification. No v1.1 semantics were changed.

### R-002 — Legacy Reporter realization insufficient — **RESOLVED BY BOUNDARY**

Reporter v1.0.0 remains unchanged. Canonical realization is RPKG/RLE/RSNAP. The compatibility layer explicitly reports lossless/lossy/unrepresentable mappings and never fabricates actor identity or injects Realizer `REVID-*` into immutable Publisher evidence.

### R-003 — Parent documents contained older realization language — **RESOLVED**

README, CLAUDE, and parent PRD/HLD/LLD/context now contain an explicit Realizer Phase-1 extension notice. Historical Publisher semantics remain intact; measured-outcome authority is `docs/realizer/*`.

### R-004 — Pending-step authoritative pricing — **RESOLVED + REGRESSION COVERED**

`StatefulBundleEstimator.estimate()` now stops authoritative scenario pricing at the first `PENDING_REEVALUATION` step. The stale downstream targets remain visible but are not priced into authoritative Plan A economics. Realizer independently retains the same-scope denominator guard.

Regression: `test_pending_reevaluation_steps_not_priced_into_authoritative_plan_a`.

### R-005 — No public Publisher `plan_step_id` — **ACCEPTED**

Realizer uses `(scenario_id, step)` as the Publisher step reference and does not invent a Publisher-issued ID.

### R-006 — PKG initializes legacy realization `NOT_APPLIED` — **ACCEPTED**

Measured outcomes remain in RPKG and never mutate PKG.

### R-007 — Actor/evidence mismatch in legacy projection — **RESOLVED BY ADAPTER**

Unknown actor remains unknown. Fresh post-change `REVID-*` stays outside PKG evidence.

### R-008 — Materialized Publisher version labels stale at v2.6 — **RESOLVED**

The recovered source implementation matched the v2.7 design baseline. The integrated release is v2.8.0: unchanged subsystem semantics retain v2.7 tags, while the changed authoritative bundle-pricing behavior is versioned `stateful-resource-bundle-v2.8`.

### R-009 — Recovered root `CLAUDE.md` belonged to a job-cluster product — **RESOLVED**

Root `CLAUDE.md` now describes this SQL Warehouse Publisher/Realizer repo and its Phase-1/Phase-2 governance.

## 4. Executable behavioral readiness

All locked Golden Realization Scenarios are implemented:

```text
GR-001 ... GR-040 = PASS
```

The implementation additionally verifies:

- PKG immutability;
- stable application-episode reuse for repeated observations;
- schema validation;
- replay-through historical reconstruction;
- ledger-derived `as_of_utc`;
- unknown actor non-fabrication;
- exact and modified same-scope denominator semantics;
- Plan B scenario realization;
- simultaneous compatible-change scenario attribution;
- append-only billing restatements;
- config drift interval closure;
- zero retroactive credit on replacement packages;
- deterministic replay and event idempotency;
- non-double-counted cumulative realization.

## 5. Financial consistency review

Aligned and executable invariants:

- actual-state realization;
- same-scope denominator;
- DBU + attributable AWS component separation;
- modeled AWS economic value distinct from hard-cash evidence;
- economic basis separation;
- negative values preserved;
- annualization only when representative;
- authoritative final-state cumulative realization instead of blind annualized episode sums;
- unresolved economic overlap blocks recommendation-attributed portfolio value.

## 6. Source consistency review

`system.compute.warehouses` remains authoritative for current core configuration. Analyzer owns normalized post-change performance/workload/regime evidence. Billing owns DBU consumption/corrections. A versioned AWS registry is explicitly modeled evidence until stronger authoritative AWS billing evidence is available.

No API/source is silently promoted to configuration authority for convenience.

## 7. Reporting readiness

The locked Workbook v1.1.0 remains unchanged as the Publisher projection.

Realizer Workbook Extension v1.0.0 adds two presentation-only sheets:

1. `Realization Summary`
2. `Realization Timeline`

Verified combined workbook sheet count: **6**.  
Formula-error scan: **0 matches**.

## 8. Offline release evidence

- Python compile: PASS.
- Automated tests: **63/63 PASS**.
- Realizer goldens: **40/40 PASS**.
- Publisher v2.8 regression: PASS.
- Exact Publisher→Realizer synthetic flow: PASS.
- Modified application flow: PASS.
- Workbook extension projection: PASS.
- Security/static scan for obvious `eval`/`exec`/token-print/TODO/FIXME patterns in new Realizer surfaces: no findings.

See [`release-evidence.md`](release-evidence.md).

## 9. External gates still open

### GATE-LIVE-001 — Live Databricks source-system E2E

Not run in this environment because no Databricks profile/host/token/SQL endpoint is configured. Do not treat synthetic evidence as a live-source pass.

Required connected-environment proof:

```text
live system tables
→ publisher.py
→ PKG
→ controlled/HITL post-change observation
→ realizer.py
→ validation/counterfactual/economics
→ RPKG
→ selective regeneration
→ replacement PKG
```

### GATE-GIT-001 — Real release worktree/merge evidence

This supplied artifact is a repository snapshot and contains no `.git` metadata, so a genuine branch/worktree/merge cannot be proven here. In the development repository, execute the release in its dedicated branch/worktree from `develop` and preserve merge evidence.

### GATE-REVIEW-001 — `/code-review low`

A manual/static review and full regression were performed here. The organization's actual slash-command review cannot be invoked/proven in this container and remains required before merge.

## 10. Final judgment

**Offline Phase-1 RC: READY.**  
**Production merge/GA evidence: NOT YET COMPLETE until the three external gates above are satisfied.**

No additional product-design document is required before connected-environment validation.


### R-009 — Direct Realizer live observation collector — **OPEN PRE-PRODUCTION GATE**

The deterministic Realizer core consumes the locked normalized observation contract. Direct post-change collection from Databricks by passing the analysis Warehouse/all-purpose cluster target into `realizer.py` is not yet wired. This does not invalidate offline contract/golden proof, but it blocks calling the release production-complete until the live adapter and source-system E2E are implemented and validated.
