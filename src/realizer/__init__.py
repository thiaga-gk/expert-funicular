"""Deterministic, read-only Databricks SQL Warehouse Realizer.

``realizer.realizer`` is the facade: it records what happened after a RecommendationPackage (PKG-*) was published as
an append-only realization ledger (RLE-*) and projects a RealizationPackage (RPKG-*) / snapshot (RSNAP-*), by calling
the capability subpackages once each (docs/architecture/realizer/repo-reorg-design.md); the typed contracts,
configuration and shared helpers live in the subpackages. The single-file implementation it was extracted from is kept
only as the git tag ``realizer-reorg-oracle-1.0.0``.
"""
