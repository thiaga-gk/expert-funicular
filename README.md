# Databricks SQL Warehouse Optimizer

Deterministic, read-only optimizer for Databricks SQL warehouses. The publisher uses **Reporter JSON Contract v1.3.0** as the machine source of truth and **Workbook Reporter Contract v1.4.0** as the professional human projection.

## Install

Python 3.10 or newer.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Canonical flow

```mermaid
flowchart LR
    ST[Databricks system tables] --> C[Collector]
    C --> N[Normalize + current epoch]
    N --> G{Supported-type gate}
    G -->|CLASSIC / PRO / SERVERLESS| A[Analyze → optimize → estimate]
    G -->|Other| X[Fail closed]
    A --> R1[Reviewer gate 1]
    R1 --> S[Scenario planner + stateful bundle estimator]
    S --> R2[Reviewer gate 2]
    R2 --> P[RecommendationPackage v1.3.0]
    P --> J[packages/*.json]
    P --> W[Workbook Contract v1.4.0]
    W --> X1[workbook.xlsx]
```

The workbook never recalculates recommendations, confidence, IDs, or scenario economics.

## Supported optimization scope

Active warehouse types: `CLASSIC`, `PRO`, standard `SERVERLESS`. Any future/unknown warehouse type fails closed before optimization. Lakehouse Real-Time is not modeled by this release.

Active candidate domains (one candidate per domain/warehouse): warehouse size, max clusters, min clusters, auto-stop, and warehouse-type migration. Photon (observed from billing) and the warehouse channel (from the configuration) are reported but never recommended. Query rewrite, storage, schedule, Spot-policy, and statement-timeout optimization are outside this release.

## Output directory

```text
<output-dir>/
├── packages/
│   └── <workspace_id>__<warehouse_id>.json
├── workbook.xlsx
├── evidence.json          # versioned replay input for --from-evidence
├── manifest.json
└── publisher.log
```

Each package validates against Python semantic/hash invariants and the committed Draft 2020-12 JSON Schema before publication. `evidence.json` is validated the same way (`schema/evidence.schema.json`); see [`docs/contracts/evidence-format.md`](docs/contracts/evidence-format.md).

## Professional workbook

The workbook contains exactly four sheets:

1. **Executive Summary** — coverage, baseline cost, signed portfolio impact, confidence and top warehouse impacts.
2. **Warehouse Portfolio** — one row per recommendation package.
3. **Scenario Plans** — one row per scenario.
4. **Recommendation Registry** — one row per candidate/technique decision.

Economic cells are numeric whenever available. Positive annual impact means savings; negative annual impact means cost increase and uses accounting-style parentheses/red formatting. Separate `Currency` and `Impact Type` columns make the meaning explicit. Unavailable economics are never coerced to zero.

See [`docs/contracts/workbook-contract.md`](docs/contracts/workbook-contract.md).

## Synthetic golden-scenario fleet (end-to-end test)

```bash
python publisher.py --demo --output-dir ./demo_out
```

The demo fleet has one simulated warehouse per golden scenario (each tagged `golden_scenario=<row id>` in its config tags). Each warehouse's evidence is simulated from a workload description: query windows by day rule (weekdays, all days, month-end, half-year-end) with concurrent or periodic queries, auto-stop deciding running time and restarts, and clusters following concurrency within min/max. Billed DBU is cluster-hours times the published DBU/hour per size (with small daily noise) and cost is DBU times the SKU list price. The simulated rows are turned into collector query results and normalized by the real evidence extraction, so the run enters the same downstream pipeline as live evidence. `--demo-count N` (1..40) runs the first N warehouses.

Expected output: one immutable package per simulated warehouse, one `workbook.xlsx`, replay evidence, manifest and log. The output directory is not tracked; regenerate it with the command above.

## Live collector execution

Authenticate with a Databricks CLI OAuth profile. Static PAT profiles are rejected.

SQL warehouse / Serverless execution target:

```bash
databricks auth login --host https://<workspace> --profile WHOPT
python publisher.py \
  --profile WHOPT \
  --serverless-warehouse-id <warehouse-id> \
  --source-region us-east-1 \
  --output-dir ./out
```

Every flag, its default and its meaning: [`docs/architecture/lld.md`](docs/architecture/lld.md) section 5.2. `--sql-warehouse-id` is also supported. A validated all-purpose cluster may be used with `--all-purpose-cluster-id`; jobs compute is rejected. REST interactions are read-only and limited to execution-target resolution/validation and optional compatibility evidence. Authoritative optimizer configuration and metrics remain system-table driven.

`--source-region` must be an AWS region in `pricing/aws_ec2_on_demand.json` when the fleet has Pro/Classic warehouses: otherwise a live run stops after collection, before writing packages, evidence or the workbook. Add `--allow-missing-aws-price` to publish anyway, with Pro/Classic AWS cost unavailable. Serverless-only fleets and `--from-evidence` replays are not affected.

To reproduce an earlier collection despite later billing corrections, add `--billing-ingested-through YYYY-MM-DD`: every billing query then reads only records ingested on or before that date. A UTC day enters the cost basis only 24 hours after it ends and once its workspace's billing watermark has passed it.

## Weekly scheduled run

Run the publisher once a week as a Databricks job: a Python task or a notebook that calls `publisher.py` in live mode (the flags above) with an OAuth CLI profile available to the job (static PAT profiles are rejected, and the Databricks CLI must be on the job's `PATH`) and `--output-dir` on a Volume, for example `/Volumes/<catalog>/<schema>/<volume>/whopt/<run-date>`. Keep each run's dated output directory: every run reads the system tables afresh, and the directories are the audit trail.

What each run shows, per warehouse package:

- `observed_change`: the configuration change since the previous epoch, that is, a plan step that was applied;
- steps that became ready as their evidence matured (`execution_state` `READY` or `READY_FOR_CANARY` in `recommended_plan`);
- `recommended_plan.reevaluation` with `earliest_reevaluation_time` while nothing is ready yet and the plan is waiting for the stability period or cost history to clear.

`carried_forward_plan` (the earlier epoch's plan carried forward) is reserved in the contract and not produced yet ([`docs/reference/known-limitations.md`](docs/reference/known-limitations.md)).

Weekly is enough: every gate measured in days (the stability period, the cost history) clears within a week, and a canary's result is read on the first run after its stability period. The job is read-only and never applies a change: owners apply approved steps in the Databricks UI or API themselves, and the next run measures the epoch that follows.

## Source policy

- `system.compute.warehouses` — configuration snapshots/epochs (an epoch is a period of unchanged type, channel, size, min/max clusters and auto-stop; name/tag-only snapshots do not start one).
- `system.compute.warehouse_events` — scaling/resource-state evidence, including a daily resource series for the current epoch (last 120 UTC days) that drives the AWS baseline and recent time at min/max.
- `system.query.history` — latency, queueing, spill, workload source/client evidence.
- `system.billing.usage` — DBU usage/cost, Photon observation, owner attribution.
- `system.billing.list_prices` — Databricks list pricing.
- `pricing/aws_ec2_on_demand.json` — versioned regional forward AWS price registry for Pro/Classic modeled infrastructure cost.

## Documentation

[`docs/README.md`](docs/README.md) indexes every document by audience: product, architecture, contracts, reference, skills, proposals and archive. Start with [`docs/architecture/context.md`](docs/architecture/context.md) for the domain language, then the two contract docs under `docs/contracts/`.

## Reusable Databricks publisher skill suite

This repo also contains a reusable nine-skill system under `.claude/skills/` for building equivalent deterministic publishers for other Databricks services without copying SQL-Warehouse domain assumptions.

Start with `databricks-publisher-orchestrator`. It coordinates service discovery, source contracts, technique catalog, economics, public contracts, implementation, independent validation, and release sealing through explicit artifact gates. The generic reporter vocabulary uses `resource`, not `warehouse`, so Job Cluster/Lakeflow implementations rediscover their own identity/configuration/economics while reusing the proven publisher architecture.

See [`docs/skills.md`](docs/skills.md). Validate the suite with:

```bash
python tools/validate_skill_suite.py
```

The suite version is declared in `.claude/skills/suite.yaml`; the validator reports the current eval counts. The optimization reasoning model is explicit: First Principles → Causal Mechanism → Constraints-First Optimization → Counterfactual → Second-Order Effects → Validation & Learning (`.claude/skills/databricks-publisher-orchestrator/references/optimization-thinking-framework.md`).

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite covers collector/normalization/model/optimizer behavior, deterministic identities, golden scenarios (`tests/golden/`), Reporter Contract validation, exact workbook columns/grains, numeric economic presentation, unsupported-type fail-closed behavior and the synthetic golden-scenario fleet flow.
