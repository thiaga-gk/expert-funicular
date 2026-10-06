# Databricks SQL Warehouse Optimizer v2.8.0

Deterministic, read-only SQL Warehouse recommendation and post-change realization product.

**v2.8.0** upgrades the v2.7 Publisher baseline with a Phase-1 Realizer and fixes authoritative Plan-A pricing so downstream `PENDING_REEVALUATION` steps remain visible but do not inflate authoritative scenario economics.

## Release/component versions

| Surface | Version | Notes |
|---|---:|---|
| Product / Publisher release | **2.8.0** | integrated Phase-1 release |
| Publisher policy/analyzer/optimizer/model/estimator | v2.7 | semantics unchanged by this release |
| Publisher bundle estimator | **stateful-resource-bundle-v2.8** | excludes pending downstream steps from authoritative pricing |
| Reporter JSON Contract | 1.0.0 | unchanged / locked |
| Workbook Contract | 1.1.0 | unchanged / locked |
| Realizer | 1.0.0 | new Phase-1 post-change engine |
| Realizer Workbook Extension | 1.0.0 | presentation-only extension |

See `docs/release-notes-v2.8.0.md`.

## Phase-1 architecture

```text
Databricks system tables / governed evidence
        ↓
   publisher.py                 pre-change recommendation authority
        ↓
      PKG-*                     immutable RecommendationPackage
        ↓
 external HITL / observed change
        ↓
   realizer.py                  post-change measurement authority
        ↓
 RLE-* append-only ledger
        ↓
 RPKG-* / RSNAP-*              canonical realization truth
        ↓
 Workbook v1.1 + Realizer Workbook Extension v1.0
```

Both `publisher.py` and `realizer.py` intentionally remain single-file fast-iteration cores for Phase 1. Contracts, schemas, docs, tests, and presentation adapters remain separate.

## Canonical authorities

- **Publisher PKG** — what should change, ordering/dependencies, predicted economics.
- **Realizer RPKG/RLE/RSNAP** — what actually changed, validation, counterfactual realized value, attribution, corrections, drift, rollback and regeneration.
- **Workbook/UI** — presentation only; never recomputes recommendation or realization truth.

The PKG is immutable. Realizer never writes measured outcome back into the original recommendation package.

## Publisher quick start

Synthetic demo:

```bash
python publisher.py --demo --demo-count 1 --output-dir ./out
```

Live collection through a SQL/Serverless warehouse:

```bash
databricks auth login --host https://<workspace> --profile WHOPT
python publisher.py \
  --profile WHOPT \
  --serverless-warehouse-id <analysis-warehouse-id> \
  --source-region us-east-1 \
  --output-dir ./out
```

The approved all-purpose-cluster SQL-connector path is also supported. Jobs compute is not the Phase-1 collector target.

Primary Publisher sources:

- `system.compute.warehouses`
- `system.compute.warehouse_events`
- `system.query.history`
- `system.billing.usage`
- `system.billing.list_prices`

Pro/Classic AWS economics use `pricing/aws_ec2_on_demand.json` until a stronger approved source such as CUR/Data Exports is available.

## Realizer quick start

```bash
python publisher.py --demo --demo-count 1 --output-dir ./out --no-workbook
PKG=$(find ./out/packages -name '*.json' | head -1)
python realizer.py --package "$PKG" --demo --output-dir ./out
```

Modified implementation demo:

```bash
python realizer.py --package "$PKG" --demo-modified --output-dir ./out-modified
```

Controlled/live-derived observation:

```bash
python realizer.py \
  --package ./out/packages/<package>.json \
  --observation ./observation.json \
  --output-dir ./out
```

**Current live boundary:** `realizer.py` consumes the normalized observation contract. Direct Databricks post-change collection by passing an execution-target ID into `realizer.py` is not yet wired; the live collector/adapter plus source-system E2E remains a pre-production gate.

Realizer artifacts:

```text
realization/
├── ledger/         RPKG-*.jsonl
├── packages/       RPKG-*.json
├── snapshots/      RSNAP-*.json
├── evidence/       REVID-*.json
└── regeneration/   RERUN-*.json
```

Key semantics:

- `EXACT | MODIFIED | NOT_APPLIED | CONTRARY | UNRELATED` are independent application-conformance states.
- `MODIFIED` may realize value, but expected economics are recalculated for the actual applied state.
- `PENDING_REEVALUATION` never contributes stale authoritative/realized economics.
- realized savings use the old state under actual post-change workload, not naïve before/after spend;
- hard performance/reliability failure overrides savings;
- validated observed realization is distinct from recommendation-attributed realization;
- corrections append and replay rather than rewrite history;
- half-open `[start,end)` intervals and economic exclusivity prevent double counting.

## Realizer JSON examples

Canonical exact and modified examples are under:

```text
examples/realizer_jsons/
├── exact/
└── modified/
```

Each example includes the source PKG plus RPKG, RSNAP, RLE JSONL, REVID evidence, and regeneration request. These are synthetic demonstration economics, not production value claims.

## Workbooks

Locked **Workbook Contract v1.1.0** remains unchanged and contains the four Publisher views. The separately versioned **Realizer Workbook Extension v1.0.0** adds:

5. `Realization Summary`
6. `Realization Timeline`

It consumes PKG + RPKG/RLE and does no business/economic recomputation.

Example: `examples/realizer_demo/publisher-realizer-workbook.xlsx`.

## Documents

### Current Phase-1 product

- Parent Publisher: `docs/prd.md`, `docs/hld.md`, `docs/lld.md`, `docs/context.md`
- Reporter JSON: `docs/reporter-json-contract-v1.0.0.md`
- Workbook: `docs/workbook-contract-v1.1.0.md`
- Release notes: `docs/release-notes-v2.8.0.md`

### Realizer

`docs/realizer/` contains:

- `prd.md`
- `hld.md`
- `lld.md`
- `context.md`
- `realizer-json-ledger-contract-v1.0.0.md`
- `realizer-source-evidence-contract-v1.0.0.md`
- `realizer-policy-contract-v1.0.0.md`
- `golden-realization-scenarios-v1.0.0.md`
- `reporter-workbook-compatibility-contract-v1.0.0.md`
- `cross-document-readiness-review-v1.0.0.md`
- `implementation-release-plan-v1.0.0.md`
- `release-evidence-v1.0.0.md`

### Phase 2 — separate future document set

Phase 2 is deliberately separated from the current Phase-1 design. See `docs/phase2/`:

- `README.md`
- `prd.md`
- `hld.md`
- `lld.md`
- `context.md`
- `migration-release-plan.md`

Phase 2 scales **Publisher + Realizer together**. It is not runtime authority in v2.8.0.

## Claude skill

`.claude/skills/realizer-delivery/SKILL.md` is the Phase-1 delivery skill. It references the locked Realizer docs, enforces TDD/golden/determinism/financial gates, and intentionally delegates future scale-out work to `docs/phase2/`.

## Tests

```bash
python -m py_compile publisher.py realizer.py contracts/*.py reporters/*.py tests/test_*.py
python -m unittest discover -s tests -p 'test*.py' -v
```

Current offline evidence: **63/63 automated tests pass**, including **GR-001 through GR-040 (40/40)**, Publisher v2.8 regression, Realizer contracts/core, deterministic replay/idempotency, financial quality gates, and workbook projection.

## Release status

This repository is an **offline v2.8.0 release candidate**. Synthetic Publisher→Realizer evidence is included.

External gates still required before production merge/GA evidence:

1. live Databricks source-system Publisher→Realizer E2E;
2. actual Git release-worktree/merge evidence;
3. organizational `/code-review low` execution and resolution.

These are intentionally not represented as passed. See `RELEASE_STATUS.md` and `docs/realizer/release-evidence-v1.0.0.md`.
