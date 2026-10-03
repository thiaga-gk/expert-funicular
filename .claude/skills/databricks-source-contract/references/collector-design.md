# Collector Design Rules

1. Aggregate in Databricks SQL/system tables whenever practical.
2. Preserve stable resource identity and source region.
3. Keep raw SQL statement text and credentials out of evidence/logs.
4. Use temporal joins for pricing/config snapshots when required.
5. Preserve signed/corrected billing quantities.
6. Make collection idempotent for the same source snapshot/window.
7. Return structured normalized evidence with explicit source provenance.
8. Support regional collection + deterministic merge when source tables are regional.
