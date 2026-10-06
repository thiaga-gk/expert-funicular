# Mining method

How `scripts/mine_patterns.py` turns a run into a pattern report, and how to read each part.

## Contents
1. Inputs
2. Features
3. Signature catalog
4. Premises
5. Report parts and how to read them
6. Known limits

## 1. Inputs

The miner reads the folder that holds `evidence.json`, which is the versioned envelope, schema-checked on load, together with the
`packages/*.json` next to it. A Hive-style run folder (`run_date=.../run_ts=...`) is found automatically when it is the only
run below `--run-dir`. When the run excluded a warehouse, `manifest.json` lists it, and it has evidence but no package. It
is still matched, but no expectation is checked against it.

## 2. Features

All features come from the **current configuration epoch** and the warehouse-level billing fields, the same authority
the publisher uses. Older epochs feed only `epoch_count`, `prior_size_step`, `prior_type` and `p95_vs_prior`.

| Feature | Source | Meaning |
|---|---|---|
| `warehouse_type`, `warehouse_size`, `size_rank`, `warehouse_channel`, `min_clusters`, `max_clusters`, `auto_stop_minutes` | current epoch `config` | Configuration (`size_rank` is the index in `SIZE_ORDER`) |
| `epoch_count`, `current_epoch_age_days` | `epochs`, `analysis_time` | Configuration history and maturity |
| `prior_size_step`, `prior_type`, `p95_vs_prior` | previous epoch | Size step of the last change (negative = downsize), the previous type, and p95 now ÷ the previous epoch's p95 |
| `nonprod` | tags through `nonprod_tag_match` | The warehouse is tagged as non-production by the publisher's own rule |
| `cost_history_days`, `baseline_forecast_cost_365`, `dbu_cost_known`, `billed_days_30`, `forecast_model`, `billing_correction_share`, `unpriced_dbu_365` | billing fields, `forecast_metadata`, `daily_cost_365` | Cost maturity, forecast model, corrections, unpriced usage |
| `queries_30`, `active_days_30`, `p95_execution_ms_30`, `max_execution_ms_30`, `queued_query_pct_30`, `p95_wait_capacity_ms_when_queued_30`, `spill_query_pct_30`, `total_spill_gib_30`, `failure_cancel_pct_30`, `result_cache_pct_30`, `compute_wait_queries_30` | current epoch `query` (30 days) | Demand and query quality |
| `growth_ratio_8w` | `queries_week_1..8` | Queries in the last 2 weeks ÷ weeks 7–8 |
| `peak_concurrency_recent`, `peak_concurrency_epoch`, `epoch_peak_ratio` | `concurrency_histogram` RECENT / EPOCH | Highest observed concurrency level, and epoch peak ÷ recent peak (peaks outside the 30-day window) |
| `uptime_minutes`, `time_at_max_pct`, `time_at_min_pct`, `starts_30`, `scale_ups_30` | current epoch `events` | Running time and cluster-count behaviour |
| `busy_ratio`, `p50_gap_minutes`, `p90_gap_minutes`, `kept_alive_gap_count` | current epoch `gaps` | Busy minutes ÷ running minutes, and the idle gaps between queries |
| `compute_wait_share`, `gap_p90_below_auto_stop`, `gap_p50_above_auto_stop`, `ceiling_headroom`, `pinned`, `pct_bi` | derived | Share of queries that waited for compute; whether the typical gap is shorter or longer than auto-stop; max clusters minus clusters needed at one cluster per 10 concurrent queries; min = max > 1; the largest BI source share |
| `primary_source`, `pct_<category>`, `owner_known`, `region_count`, `photon_status` | `attribution`, `source_region`, `photon_observation` | Query sources, owner attribution, regions, Photon |

A missing value is `None`. A numeric comparison against `None` is false, so a row never matches on missing evidence.

## 3. Signature catalog

`signature-catalog.yaml` holds one entry per registry row. Each entry has:
- `role`: `primary` for a workload or capacity shape, or `modifier` for a cross-cutting attribute such as type, billing, tags or maturity.
  A warehouse matches any number of rows. **A gap is a warehouse that matches no primary row.**
- `signature`: conditions that must all hold (`eq ne in not_in gt gte lt lte is_null not_null`, or `any: [...]`). A
  row with no signature can't be detected from evidence (cross-warehouse, REST-only or flag-driven), so it is listed as
  manual.
- `expect`: the row's expected behaviour, written as assertions on the package. The syntax is the golden DSL (see the
  `tests/golden/test_golden_publisher.py` docstring), plus `in` / `not_in` (applied to each value) and `any: [...]`. Each candidate also
  carries a `direction`: `up`, `down`, `same` or (for type) `change`. Write invariants that must hold for *any* warehouse
  with the signature, such as "an under-sized warehouse is never downsized". Don't pin exact targets; those belong in a case.
- `drift`: the features compared against the demo-fleet warehouse that instantiates the row.

Signatures describe patterns. Their numbers are deliberately looser or different than publisher policy, and they must
never be copied into `Thresholds`.

## 4. Premises

A premise is an assumption that the expected behaviour of one or more rows rests on. The catalog's `premises` list
records each one with:
- `assumes`: the publisher setting that encodes it, for example `restart_cost_cluster_minutes_pro_classic = 4.0`;
- `rows`: the rows that depend on it;
- `when`: which warehouses it applies to;
- `check`: what live evidence looks like when the assumption is true.

| Premise | Assumes | Live check | Rows |
|---|---|---|---|
| P-RESTART-PRO | Pro/Classic restart about 4 cluster-minutes | p95 wait for compute ≤ 8 min | C01 C02 C05 C13 C25 C49 C51 C53 C54 |
| P-RESTART-SERVERLESS | Serverless restart about 0.1 cluster-minutes | p95 wait for compute ≤ 60 s | C02 C14 A01 |
| P-SIZE-RATE | billed size-pair DBU ratio within 25% of the published ratio | `size_rate_deviation` ≤ 0.25 | C06 C07 C22 C47 C55 |
| P-QUERIES-PER-CLUSTER | 10 concurrent queries per Pro/Classic cluster | queueing at max with ≥ 5 concurrent queries per cluster | C08 C09 C42 C50 |

A premise is checked only on warehouses that match one of its rows and its `when`. A missing value makes it *unknown*:
missing evidence never contradicts. The report gives counts of holds, contradicted and unknown for each premise, and
lists each contradiction with its feature values.

A **row premise contradiction** differs from a behaviour mismatch. In a mismatch the publisher disagrees with the row.
In a contradiction, reality disagrees with what the row (and usually the publisher) assumes. The two often coincide. A
contradiction on one warehouse is a lead to investigate. A contradiction across many matched warehouses, or one confirmed
by a real-data answer (for example the restart-billing assumption in `docs/proposals/publisher/real-data-run.md`), is grounds to propose new
row text. Changing the setting behind it is a separate, versioned decision change.

Add a premise when a row's expected behaviour depends on a number or mechanism that live evidence can test. Keep the
check a tolerance band around the assumed value, and name the exact setting in `assumes`.

## 5. Report parts and how to read them

- **coverage:** live warehouses per row. A high count on a `fail` row means the failure matters in practice. Rows
  with no live match stay in the registry as edge cases; absence in one workspace is not evidence that the row is unneeded.
- **gaps:** unmatched warehouses grouped by a coarse fingerprint (type, size, clusters, auto-stop bucket, busy, queued,
  spill, primary source). For each group, open one warehouse's features and package and ask what the
  pattern is and what the optimizer should do with it. If the answer is "nothing special, a healthy warehouse", the
  proposal may be a single "healthy / no-change" row rather than one row per group.
- **mismatches:** each mismatch names the row, the warehouse and the failed assertions. There are two possible causes:
  - **publisher bug:** the decision contradicts the row's expected behaviour, and the package rationale confirms it;
  - **signature false positive:** the warehouse isn't really that pattern, for example a canary rollback that also looks
    over-sized. Then tighten the signature, and record what distinguished the two cases.
  Mismatches on a `fail` row get priority `high`.
- **premises / premise_contradictions:** counts per premise, then each contradiction with the values that broke it.
  Many *unknown* results mean the evidence can't test the premise on this account. Note that. Don't read it as the premise holding.
- **drift:** `demo_value` is the demo-fleet warehouse's value, shown against the live min, median and max. A demo value outside
  the live range means the simulation is unrealistic for that row. Propose new `DEMO_FLEET` parameters only when the
  row's expected behaviour still holds with them. When it doesn't, that is a finding about the row, not about drift.
- **real_data:** the raw aggregates for G4b, G5 and S4. Interpret them against the questions in
  `docs/proposals/publisher/real-data-run.md`, and propose the closing text there.

## 6. Known limits

- The features are aggregates, not per-query traces. Polling versus real use (C04) and cache-heavy latency (C21) stay
  approximate until statement types are classified (S4).
- Tag and rename churn (C36), deleted and recreated warehouses (C29), and cross-warehouse duplicates (C24) need the raw
  snapshots or several warehouses together, so they are manual rows.
- One workspace is one sample. Propose a new row only for a pattern the user agrees is general.
