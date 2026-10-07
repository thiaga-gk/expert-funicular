# Exemplar layout (SQL Warehouse publisher)

The SQL Warehouse publisher in this repository started as one `publisher.py` and was split into capability packages once the design settled. Use it as the reference shape for a production publisher; the reorganization record is `docs/architecture/publisher/repo-reorg-design.md`.

## Packages

| Path | Role |
|---|---|
| `src/publisher/publisher.py` | Facade: `publish()` calls each capability once, in pipeline order, and holds no domain logic |
| `src/publisher/{analyzer,governor,optimizer,reviewer,packager,reporter}/` | One package per pipeline capability (observe, decide what may be considered, decide, gate, assemble, write) |
| `src/publisher/{contracts,config,common}/` | Typed contracts and schemas, versions and product constants, shared utilities |
| `src/publisher/cli/` | Adapter: parses arguments, builds the request, maps errors to exit status; never imports a capability package |
| `src/workflow_shared/` | Neutral read-only Databricks access (OAuth through a CLI profile, REST GET, a UTC-pinned SQL client) and vocabulary shared with the Realizer; imports neither product, and each product injects its logger, user agent and error type |

Boundaries are enforced by tests (`tests/unit/cli/test_cli.py`, `tests/unit/workflow_shared/test_boundaries.py`).

## Run-level features a production publisher needs

- **Three input modes on one pipeline:** live collection, replay of a versioned evidence file (`--from-evidence`, schema-checked on write and load), and a simulated demo fleet that enters the same downstream pipeline as live evidence.
- **Run scope:** restrict which scenarios and techniques are built and which resources are assessed, recorded in the package (`run_scope`), never changing what is observed. A scoped candidate is identical to the same candidate in a full run.
- **Cost profiles:** named risk appetites that move only the thresholds of cuts, never evidence floors, product constants or the rules that add capacity.
- **Run folders:** each run writes to its own `run_date=/run_ts=` folder (Hive-style) with nothing time-dependent inside the files, so identical replays write identical files; a flat mode for scripted pipelines.
- **Price registries and guards:** a versioned regional cloud price registry, and a live run that stops rather than guess when a region is missing.
- **A downstream consumer:** the Realizer reads the packages after a change is applied and accepts exactly one package contract version, so a contract change is a coordinated change of both products.
