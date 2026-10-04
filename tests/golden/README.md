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

Scenario fixtures (`c*.json`, run by `tests/test_golden_scenarios.py`; the number is the catalog row):
- c20-sparse-cost-history
- c32-unpriced-sku
- c36-tag-rename-churn
- c48-long-unchanged-warehouse

Golden evidence is synthetic and exists to test decision semantics, blockers, epoch handling, DBU/AWS separation, and scenario contracts. It is not pricing guidance. To add a case, add a JSON file; no test code is needed for path-based assertions.
