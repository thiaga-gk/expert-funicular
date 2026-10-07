# Realizer Phase-1 Release Evidence v1.0.0

> Frozen record of the v1.0.0 release, written when the Realizer was a single-file implementation (`realizer.py`). The code it describes is now `src/realizer/`; that single-file implementation is kept only as the git tag `realizer-reorg-oracle-1.0.0`. Paths and commands below are those of the release and are not maintained; see [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md).

> The artifacts this record cites by path (`examples/test-results.txt`, `examples/realizer_demo/`, `examples/realizer_jsons/`, `docs/realizer/`) are in the tag `realizer-reorg-oracle-1.0.0` under `legacy/realizer_single_file/`; read one with `git show "realizer-reorg-oracle-1.0.0:legacy/realizer_single_file/<path>"`. `docs/phase2/` is not in the tag or anywhere in this repository's history.

**Release:** `REL-REALIZER-1.0.0`  
**Evidence date:** 2026-10-03  
**Product release:** v2.8.0  
**Publisher design lineage:** v2.7 baseline  
**Status:** OFFLINE RELEASE CANDIDATE — CONNECTED-ENVIRONMENT GATES OPEN

## 1. Implemented surfaces

- `publisher.py` — single-file Publisher core at product release v2.8.0; pending-step pricing guard is versioned through `stateful-resource-bundle-v2.8`.
- `realizer.py` — Phase-1 single-file Realizer core.
- `contracts/realizer_contract.py`
- `contracts/realizer_schema.py`
- `schema/realization-package.schema.json`
- `schema/realization-ledger-entry.schema.json`
- `contracts/realizer_workbook_contract.py`
- `reporters/realizer_workbook_reporter.py`
- complete `docs/realizer/` contract/design set (in the tag under `legacy/realizer_single_file/docs/realizer/`; now in `docs/<type>/realizer/`, see [`repo-reorg-parity.md`](../../proposals/realizer/repo-reorg-parity.md)).
- `.claude/skills/realizer-delivery/SKILL.md`.

## 2. Final compile command

```bash
python -m py_compile publisher.py realizer.py contracts/*.py reporters/*.py tests/test_*.py
```

Result: **PASS**.

## 3. Final automated regression

```bash
python -m unittest discover -s tests -p 'test*.py' -v
```

Result:

```text
Ran 63 tests
OK
```

Detailed output: `git show "realizer-reorg-oracle-1.0.0:legacy/realizer_single_file/examples/test-results.txt"`.

Coverage by suite:

| Suite | Tests | Result |
|---|---:|---|
| Publisher v2.8 regression | 9 | PASS |
| Realizer core | 7 | PASS |
| Realizer contracts | 5 | PASS |
| Realizer goldens GR-001..GR-040 | 40 | PASS |
| Realizer workbook contract | 2 | PASS |
| **Total** | **63** | **PASS** |

## 4. Golden matrix

`GR-001` through `GR-040`: **40/40 PASS**.

Registry: `tests/golden/realizer/registry.json`.

Core non-waivable cases proven include modified implementation, hard guardrail failure, no counterfactual, staged Plan A, modified Plan A, supersession while still applied, economic lineage handoff, drift, rollback, billing correction, workload normalization, overlap, idempotency, determinism and cumulative no-double-counting.

## 5. Publisher alignment evidence

### Version alignment

The integrated product release is now v2.8.0. Unchanged Publisher policy/analyzer/optimizer/model/estimator semantics remain tagged v2.7 for replay honesty; the changed authoritative bundle-pricing behavior is tagged `stateful-resource-bundle-v2.8`.

### Pending downstream pricing defect

Authoritative scenario pricing now stops at the first `PENDING_REEVALUATION` step. Downstream max/min/auto-stop may remain visible as stale context but cannot inflate authoritative Plan A economics.

Regression:

```text
test_pending_reevaluation_steps_not_priced_into_authoritative_plan_a = PASS
```

### Workbook implementation version

`WORKBOOK_CONTRACT_VERSION = "1.1.0"`, aligned to the locked specification.

## 6. Synthetic Publisher→Realizer E2E

Reproducible commands:

```bash
python publisher.py --demo --demo-count 1 \
  --output-dir examples/realizer_demo/publisher_out --no-workbook

PKG=$(find examples/realizer_demo/publisher_out/packages -type f -name '*.json' | head -1)
python realizer.py --package "$PKG" --demo \
  --output-dir examples/realizer_demo
```

Current deterministic example identities:

```text
PKG   PKG-C1928BFAD6E14C166A0F518D
RPKG  RPKG-9BCA3C3DFB12CC37AA7CA775
RSNAP RSNAP-B4E0622B53B53930421A90E3
```

Example result:

```text
implementation_conformance = EXACT
validation_state            = PASSED
maturity                    = REPRESENTATIVE
realization_state           = REALIZED
denominator_source          = PUBLISHER_CANDIDATE
```

Synthetic economics are demonstration data, not production value claims.

## 7. Modified implementation proof

Command:

```bash
python realizer.py --package "$PKG" --demo-modified --output-dir /tmp/sqlwhopt-modified
```

Verified:

```text
implementation_conformance = MODIFIED
modification_relation       = MORE_AGGRESSIVE
denominator_source          = REESTIMATED_APPLIED_SCOPE
regeneration_status         = REQUESTED
```

This proves Realizer measures the actual target instead of pretending the recommended target was applied.

## 8. Workbook extension evidence

Artifact: `examples/realizer_demo/publisher-realizer-workbook.xlsx`, in the tag only (`legacy/realizer_single_file/examples/realizer_demo/publisher-realizer-workbook.xlsx`; not restored, the `.xlsx` writer is retired).

Verified with `artifact_tool`:

```text
Executive Summary
Warehouse Portfolio
Scenario Plans
Recommendation Registry
Realization Summary
Realization Timeline
```

`Realization Summary` contains the exact PKG/RPKG/RSNAP identity and application conformance from canonical artifacts.

Formula-error scan (`#REF!|#DIV/0!|#VALUE!|#NAME?|#N/A`): **0 matches**.

The extension performs no recommendation/economic recalculation.

## 9. Manual/static review

Reviewed the implementation delta and new surfaces for:

- broad Publisher refactor: none;
- PKG mutation: blocked/tested;
- stale pending economics: fixed/tested;
- missing actor fabrication: blocked/tested;
- repeated observation creating duplicate APP: fixed/tested;
- drift incorrectly creating new stale-package realization: fixed/tested;
- no-counterfactual incorrectly claiming realization: fixed/tested;
- billing correction without prior-event linkage: fixed/tested;
- scenario/Plan-B realization gap: fixed/tested;
- simultaneous compatible-change attribution gap: fixed/tested;
- locked financial claimability gates (attributable spend, price coverage, currency consistency, correction-chain completeness, material cost availability): implemented/tested;
- Realizer warehouse-size ordering now derives from Publisher canonical `SIZE_ORDER`; no competing size registry;
- obvious `eval`/`exec`/credential-print/TODO/FIXME patterns in new Realizer code: none found.

This is not a substitute for the organization's required `/code-review low` gate.

## 9.1 JSON example bundle

The repo now includes side-by-side exact and modified canonical examples under `examples/realizer_jsons/` (in the tag under `legacy/realizer_single_file/examples/realizer_jsons/`; the two observations are restored as `examples/realizer/`). Each contains source PKG, normalized observation, RPKG, RSNAP, append-only RLE JSONL, REVID evidence, and regeneration request.

Exact snapshot: `RSNAP-B4E0622B53B53930421A90E3`.  
Modified snapshot: `RSNAP-709E1EC13A10E423918D76AB`.

## 9.2 Phase-2 separation

Future scale-out design is separated from the Phase-1 Realizer docs under `docs/phase2/` (a path that never existed in this repository or in the tag) with its own PRD/HLD/LLD/Context and migration-release plan. Phase 2 is not runtime authority in v2.8.0.

## 10. External gate evidence

The Phase-1 Realizer engine currently consumes a normalized post-change observation JSON. Direct Databricks collection from a warehouse/all-purpose execution target is not yet wired into `realizer.py`; the live adapter and E2E are therefore a connected-runtime implementation/validation gate, not a completed offline gate.

Environment check found no configured Databricks profile/host/token/HTTP endpoint. Therefore a live Databricks E2E was **not run**.

The delivered repo contains no `.git` metadata, so a real release worktree/branch/merge cannot be proven inside this artifact.

Required before production merge:

1. run live read-only/source-system Publisher collection;
2. run controlled/HITL Realizer validation against actual post-change evidence;
3. reconcile realized DBU/AWS value and zero overlap;
4. run `/code-review low` and resolve findings;
5. merge the dedicated release worktree only after all evidence is retained.

## 11. Release verdict

**Offline implementation/golden RC: PASS.**  
**Production merge: BLOCKED only on explicit connected-environment/governance gates, not on unresolved design or offline code/test failures.**
