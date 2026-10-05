# Replay evidence format

`evidence.json` is the normalized evidence a run optimized from. It is written next to `packages/` and `manifest.json`, and `--from-evidence <file> [<file> ...]` replays it: identical evidence and identical policy, model and contract versions produce identical packages.

It is an internal replay input, not a public contract. Consumers should read the packages (`docs/contracts/reporter-json-contract.md`), not this file.

## Format version

There is one format version, **1.0.0**, declared in `evidence_format_version`. The publisher reads exactly that version and fails closed on anything else: a missing version, another version, or a bare list. There is no compatibility path for older files; regenerate them by re-running the collector or the demo. A change to the file's shape bumps the version and the schema together.

## Shape

```json
{
  "evidence_format_version": "1.0.0",
  "publisher_version": "2.10.0",
  "warehouses": [ { "...": "one normalized WarehouseEvidence per workspace/warehouse" } ]
}
```

- `publisher_version` records which publisher wrote the file. It is provenance only and does not affect replay.
- Each entry in `warehouses` has every field of `WarehouseEvidence` (`src/publisher/contracts/source.py`), each epoch has every field of `EpochEvidence`, and unknown fields are rejected. Free-form sections (`query`, `events`, `gaps`, `cost`, `daily_cost_365`, `billing_features`, `aws_cost_model`, `sql_sku_prices`, `compatibility_evidence`) are objects whose keys are owned by the collector and estimators.
- `resource_daily` is required on every epoch: the current epoch's daily resource series, and `null` on older epochs. A current epoch without a series is not backfilled for real evidence: the recent resource window is unavailable (`resource_window_from` returns `None`). `scripts/add_resource_daily.py` backfills fixtures only. A current epoch that was billed but has no retained warehouse event carries `resource_daily: null` and `events.resource_daily_status: UNAVAILABLE_BILLED_WITHOUT_RETAINED_EVENTS`.
- Fields the collector added to the free-form sections carry no schema constraint and fail closed when missing: `events.event_coverage_start` (first retained warehouse event), `events.resource_coverage_from` (first fully covered day when billed days precede the first retained event, else null; absent means the resource window is unavailable), `query.decision_queries_30` (with `p95_compilation_ms_30` and `p95_result_fetch_ms_30`), the daily cost rows' `dbu_quantity`, `billing_features.current_epoch_node_types` / `current_epoch_sql_tiers`, the running-idle keys `gaps.running_idle_*` with the sparse `gaps.running_idle_by_target` map (auto-stop fails closed without them), `query.concurrency_histogram` (a map of scope to level-to-minutes weights; Pro/Classic ceiling and floor targets fail closed without it, and an older epoch without it cannot hold a cut) and `query.spill_sources_30` / `query.top_spill_source_share_pct_30` (an upsize fails closed without them). `scripts/backfill_fixture_fields.py`, `scripts/backfill_running_idle.py` and `scripts/backfill_concurrency.py` backfill these into fixtures only; real evidence is never backfilled.
- The current epoch's `query` object may hold two collector sections that carry no schema constraint; absent means not collected (a current epoch only, never an older one), and the decisions then read the current epoch alone and run no canary guard. `canary_guard` is `{CURRENT, PREVIOUS}`, one row each of the canary guard collector (queries, failed or cancelled share, queued queries and share, executed queries, spilled executed queries and share, executed queries on statement sources seen in both epochs and their p95 execution time). `carried` is `{demand, capacity}`, each a `period_start` plus the carried fields of its period; `demand` may also hold `concurrency_histogram` as `{RECENT, BUSY_START}`. Which evidence carries and what the analyzer does with it: [LLD §10.2](../architecture/lld.md); the guard rules: [LLD §15.9](../architecture/lld.md). The format version stays 1.0.0.
- `forecast_metadata.dbu_forecast_status` is required. An unknown forward DBU cost is `null` with a status that says why, never `0`.
- The file is serialized with sorted keys and two-space indentation, so identical evidence gives identical bytes.

## Validation

The committed Draft 2020-12 schema is `schema/evidence.schema.json`. It is enforced in both directions:

- `save_evidence` validates before writing and writes nothing if the evidence is invalid.
- `load_evidence_files` validates every file before using it, then merges. Conflicting modeled evidence, epochs, daily cost records or billing features for the same `(workspace_id, warehouse_id)` stop the run (LLD §28.3).

Error messages name the failing path and rule (for example `warehouses/0/forecast_metadata: 'dbu_forecast_status' is a required property`). They never echo field values, because the file carries owner emails and tags.

A test pins the schema to the dataclasses, so adding a field to `WarehouseEvidence` without updating the schema and the version fails the suite.

## Sensitivity

The file contains owner emails (`attribution.owner`, `observed_owners`) and warehouse tags. Treat it like the packages: do not commit run output, and do not log field values. Output directories are gitignored.

## Changing the format

1. Update the dataclass in `src/publisher/contracts/source.py` and `schema/evidence.schema.json` together.
2. Bump `EVIDENCE_FORMAT_VERSION` and the schema `const`, and update this document.
3. Update the golden cases in `tests/golden/cases/` (their `source/evidence.json` items use this item shape) and regenerate the demo.
