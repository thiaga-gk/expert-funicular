# Golden scenarios

Deterministic cases for the SQL Warehouse optimizer. Each case is a folder under `tests/golden/cases/<name>/`:

```
cases/<name>/
  case.yaml                      name, description, optional notes / now / analysis_time, and the `expect` block
  source/evidence.json           normalized evidence items (evidence-level cases)
  source/snapshots.json          system.compute.warehouses rows (collector-level cases)
  source/collector_rows.json     optional {query label: rows} for the other collector queries
  expected/recommendation.json   canonical packages the publisher builds from the evidence (evidence-level cases)
  expected/evidence.json         serialized WarehouseEvidence the collector extracts (collector-level cases)
```

`tests/golden/__init__.py` loads a case (`case_names()`, `case_dir(name)`, `load_case(name)`), so a test reads a case without knowing the folder layout. `tests/golden/harness.py` holds the assertion and collector helpers.

Every case is checked twice by `tests/golden/test_golden_publisher.py`: its `expected/` output must equal what the publisher produces now (the behavioral baseline), and a scenario case (`c<NN>-` / `a<NN>-`, the number is the catalog row) must also pass the assertions in its `case.yaml` `expect` block. The assertion DSL (path segments, `equals` / `includes` / `excludes` / `length` / `between`, collector-level `snapshots` cases) is documented in the docstring of `tests/golden/test_golden_publisher.py`. The scenario catalog with status is `docs/proposals/golden-scenarios.md`.

Core cases (also used by `tests/golden/test_golden_expectations.py`):
- idle-expensive-warehouse
- spill-constrained
- concurrency-constrained
- cold-start-sensitive
- serverless
- photon-disabled (Photon observed DISABLED: reported in `current_config.photon`, never recommended)
- newly-changed-config
- sparse-evidence
- serverless-to-pro-costed
- aws-region-missing

Scenario cases (`c*` and `a*`):
- a05-serverless-region-unverified
- a07-billing-freshness
- a08-owner-attribution
- c06-oversized-warehouse (asserts the task-time utilization evidence only)
- c08-concurrency-gates
- c10-over-provisioned-floor (asserts the task-time utilization evidence only)
- c20-sparse-cost-history
- c26-failure-heavy
- c30-billing-corrections (collector-level)
- c31-multi-region-billing (collector-level)
- c32-unpriced-sku
- c36-tag-rename-churn (collector-level)
- c37-seasonal-peak
- c38-growth-trend
- c45-steady-high-utilization (asserts the task-time utilization evidence only)
- c48-long-unchanged-warehouse
- c50-single-cluster-queueing (demo fleet evidence)
- c56-nonprod-aggressive-cut (nonprod tag selects the aggressive cost profile)
- c57-prod-twin-holds-cut (the same evidence tagged production stays balanced)
- c58-cli-conservative-holds-cut (a `cost_profiles` warehouse override to conservative)

The `--demo` fleet is separate from these cases: the cases are hand-built from the core `demo_evidence()` warehouse, while the demo fleet is simulated from workload descriptions (see `tests/golden/test_demo_fleet.py`).

Golden evidence is synthetic and exists to test decision semantics, blockers, epoch handling, DBU/AWS separation, and scenario contracts. It is not pricing guidance.

## Adding a case

Create `tests/golden/cases/<name>/` with `case.yaml` (name, description, `expect`) and `source/`, then generate `expected/`:

```
python scripts/update_goldens.py --case <name> --approved-change "<why>"
```

A case may carry a top-level `cost_profiles` list of raw `--cost-profile` flags (for example `conservative=<workspace>/<warehouse>`); without it every warehouse gets its tag default. No test code is needed for path-based assertions. Each `evidence` item must match `schema/evidence.schema.json` (`docs/contracts/evidence-format.md`); `tests/unit/analyzer/test_evidence_format.py` fails otherwise. `expected/` is written only by `scripts/update_goldens.py`, which requires the approved-change reason; a decision-affecting change regenerates it in the same commit.

## Backfilling older cases

Cases that predate collector fields (`dbu_quantity` on daily cost rows, `events.event_coverage_start`, `query.decision_queries_30`, and the spill-by-statement-source fields `query.spill_sources_30` / `query.top_spill_source_share_pct_30`) are backfilled by `scripts/backfill_fixture_fields.py`, cases only; real evidence is never backfilled and fails closed. Running-idle evidence (`gaps.running_idle_*`, `gaps.running_idle_by_target`) is backfilled by `scripts/backfill_running_idle.py` and per-minute concurrency (`query.concurrency_histogram`) by `scripts/backfill_concurrency.py`, under the same rule. Evidence cases carry the current epoch's daily resource series (`resource_daily`); `scripts/add_resource_daily.py` backfills a synthetic one (event totals spread evenly) into older cases, and real evidence without it fails closed.

Each tool takes case folders or names and rewrites `source/evidence.json` in place (for example `python scripts/backfill_concurrency.py tests/golden/cases/*`); regenerate `expected/` afterwards.
