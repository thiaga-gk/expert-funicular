# Golden scenarios

Deterministic fixtures for the SQL Warehouse optimizer. Each JSON file contains normalized evidence plus expected semantic outcomes.

Core fixtures retained from the design discussion:
- idle-expensive-warehouse
- spill-constrained
- concurrency-constrained
- cold-start-sensitive
- serverless
- photon-disabled
- newly-changed-config
- sparse-evidence

Additional cost/scenario fixtures:
- serverless-to-pro-costed
- aws-region-missing

Golden evidence is synthetic and exists to test decision semantics, blockers, epoch handling, DBU/AWS separation, and scenario contracts. It is not pricing guidance.
