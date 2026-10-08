# Golden scenarios

Deterministic cases for the SQL Warehouse optimizer. Each case is a folder under `tests/golden/publisher/cases/<name>/`:

```
cases/<name>/
  case.yaml                      name, description, optional notes / now / analysis_time, and the `expect` block
  source/evidence.json           normalized evidence items (evidence-level cases)
  source/snapshots.json          system.compute.warehouses rows (collector-level cases)
  source/collector_rows.json     optional {query label: rows} for the other collector queries
  expected/recommendation.json   canonical packages the publisher builds from the evidence (evidence-level cases)
  expected/evidence.json         serialized WarehouseEvidence the collector extracts (collector-level cases)
```

`tests/golden/publisher/__init__.py` loads a case (`case_names()`, `case_dir(name)`, `load_case(name)`), so a test reads a case without knowing the folder layout. `tests/golden/publisher/harness.py` holds the assertion and collector helpers.

Every case is checked twice by `tests/golden/publisher/test_golden_publisher.py`: its `expected/` output must equal what the publisher produces now (the behavioral baseline), and a scenario case (`c<NN>-` / `a<NN>-`, the number is the catalog row) must also pass the assertions in its `case.yaml` `expect` block. The assertion DSL (path segments, `equals` / `includes` / `excludes` / `length` / `between`, collector-level `snapshots` cases) is documented in the docstring of `tests/golden/publisher/test_golden_publisher.py`. The scenario catalog with status is `docs/proposals/publisher/golden-scenarios.md`.

The case list is the folder listing: each `case.yaml` `description` says what the case covers. Cases named `c<NN>-` or `a<NN>-` match a row of the scenario catalog; the unprefixed core cases are also used by `tests/golden/publisher/test_golden_expectations.py`.

The `--demo` fleet is separate from these cases: the cases are hand-built from the core `demo_evidence()` warehouse, while the demo fleet is simulated from workload descriptions (see `tests/golden/publisher/test_demo_fleet.py`).

Golden evidence is synthetic, except in live cases. It exists to test decision semantics, blockers, epoch handling, DBU/AWS separation, and scenario contracts, and it is not pricing guidance. A live case (`origin: live` and `collected: <date>` in `case.yaml`) is created from a real warehouse by the `databricks-golden-miner` skill (`.claude/skills/databricks-golden-miner/scripts/make_case.py`) after the scenario is approved. Its `source/evidence.json` is the evidence exactly as collected, so it contains real warehouse names and IDs, owner emails, tags and costs. Those values ship with any copy or release ZIP of the repository.

## Adding a case

Create `tests/golden/publisher/cases/<name>/` with `case.yaml` (name, description, `expect`) and `source/`, then generate `expected/`:

```
python scripts/update_goldens.py --case <name> --approved-change "<why>"
```

A case may carry a top-level `cost_profiles` list of raw `--cost-profile` flags (for example `conservative=<workspace>/<warehouse>`); without it every warehouse gets its tag default. No test code is needed for path-based assertions. Each `evidence` item must match `schema/evidence.schema.json` (`docs/contracts/publisher/evidence-format.md`); `tests/unit/publisher/analyzer/test_evidence_format.py` fails otherwise. `expected/` is written only by `scripts/update_goldens.py`, which requires the approved-change reason; a decision-affecting change regenerates it in the same commit.

## Backfilling older cases

Cases that predate collector fields (`dbu_quantity` on daily cost rows, `events.event_coverage_start`, `query.decision_queries_30`, and the spill-by-statement-source fields `query.spill_sources_30` / `query.top_spill_source_share_pct_30`) are backfilled by `scripts/backfill_fixture_fields.py`, cases only; real evidence is never backfilled and fails closed. Running-idle evidence (`gaps.running_idle_*`, `gaps.running_idle_by_target`) is backfilled by `scripts/backfill_running_idle.py` per-minute concurrency (`query.concurrency_histogram`) by `scripts/backfill_concurrency.py` and the floor-demand maps (`resource_daily[].floor_minutes_by_demand_clusters`) by `scripts/backfill_floor_demand.py`, under the same rule. Evidence cases carry the current epoch's daily resource series (`resource_daily`); `scripts/add_resource_daily.py` backfills a synthetic one (event totals spread evenly) into older cases, and real evidence without it fails closed.

Each tool takes case folders or names and rewrites `source/evidence.json` in place (for example `python scripts/backfill_concurrency.py tests/golden/publisher/cases/*`); regenerate `expected/` afterwards.

## Realizer golden scenarios

`tests/golden/realizer/` holds the Realizer's scenarios GR-001..GR-040 (`test_golden_realization.py`; the scenario list is `docs/proposals/realizer/golden-scenarios.md` and the registry `tests/fixtures/realizer/golden-registry.json`). They are not case folders: most tests build their observation inline from the frozen Publisher package and check the realization snapshot; the rows about several packages of one warehouse (supersession, handoff, ownership, a modified step, a scenario's denominator) realize the real package fixtures and the end-to-end cases' packages with their measured observations into one state. The registry states each row's kind and whether it is non-waivable, and every run's ledgers pass the ledger invariant checker (`tests/regression/realizer/invariants.py`). The scenarios that call `realize()` are also frozen as observations with stored canonical outputs under `tests/fixtures/realizer/` and compared by `tests/regression/realizer/`; regenerate those with `python scripts/update_realizer_goldens.py --approved-change "<why>"`.

### End-to-end cases, Publisher to Realizer

`tests/golden/realizer/cases/<name>/` holds the end-to-end cases `GE-NN` (`test_golden_e2e.py`, helpers in `e2e.py`). A case is a real Publisher package for a demo-fleet warehouse under a given run scope and cost profile, a simulated world in which the owner changes that warehouse after the package (configuration changes on a timeline), the live measurement on that world (the same collector, projector and `realize()` a live run uses) and an `expect` block about the measurement: conformance, application time, windows, the replay against the oracle and the published range, the billed cross-check, other knob changes and refusals. The oracle is the same world with the measured knob held at its old value; the simulator's noise depends only on the scenario and the day, so the billed difference is the change's own effect. Agreement is a consistency check of the replay, not accuracy evidence, which only real data gives. The case format and the assertion DSL are documented in the docstring of `test_golden_e2e.py`; the case list is the folder listing.

Each case stores two stages, regenerated separately: `expected/publisher/package.json` (the Publisher demo run once per distinct scope and cost profile) and `expected/realizer/` (measurement evidence, observation, oracle and the realization files, from the stored package). The fast tests run the Realizer stage from the stored package; one test reruns the Publisher demo and compares the stored packages.

```
python scripts/update_realizer_e2e_goldens.py --approved-change "<why>" [--stage publisher|realizer|all] [--case <name>]
python scripts/update_realizer_e2e_goldens.py --check            # report drift, write nothing
```
