"""Deterministic, read-only Databricks SQL Warehouse Realizer.

``realizer.realizer`` is the engine: it records what happened after a RecommendationPackage (PKG-*) was published as
an append-only realization ledger (RLE-*) and projects a RealizationPackage (RPKG-*) / snapshot (RSNAP-*). It is being
extracted from the frozen single-file oracle in ``legacy/realizer_single_file/`` into capability subpackages
(docs/architecture/realizer-repo-reorg-design.md); the typed contracts, configuration and shared helpers live in the
subpackages.
"""
