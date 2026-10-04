# Golden scenarios

Deterministic fixtures for the SQL Warehouse optimizer. Each JSON file contains normalized evidence plus expected semantic outcomes (an `expect` block). The assertion DSL (path segments, `equals` / `includes` / `excludes` / `length`, collector-level `snapshots` fixtures) is documented in the docstring of `tests/test_golden_scenarios.py`. The scenario catalog with status is `docs/proposals/golden-scenarios.md`.

Core fixtures (used by `tests/test_publisher.py`):
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

Scenario fixtures (`c*.json` and `a*.json`, run by `tests/test_golden_scenarios.py`; the number is the catalog row):
- a07-billing-freshness
- c08-concurrency-gates
- c20-sparse-cost-history
- c26-failure-heavy
- c31-multi-region-billing (collector-level)
- c32-unpriced-sku
- c36-tag-rename-churn (collector-level)
- c48-long-unchanged-warehouse

Golden evidence is synthetic and exists to test decision semantics, blockers, epoch handling, DBU/AWS separation, and scenario contracts. It is not pricing guidance. To add a case, add a JSON file; no test code is needed for path-based assertions. Each `evidence` item must match `schema/evidence.schema.json` (`docs/contracts/evidence-format.md`); `tests/test_evidence_format.py` fails otherwise.

Evidence fixtures carry the current epoch's daily resource series (`resource_daily`). `tools/add_resource_daily.py` backfills a synthetic one (event totals spread evenly) into older fixtures; real evidence without it fails closed.
