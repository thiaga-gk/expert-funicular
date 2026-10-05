"""Execution settings: repository-relative locations of the committed schema and pricing files."""
from __future__ import annotations

from pathlib import Path


# Repository root: the committed schema/ and pricing/ directories are resolved from it, so the package runs from a
# checkout (pip install -e .), never from a copied site-packages install.
REPO_ROOT = Path(__file__).resolve().parents[3]
if not (REPO_ROOT / "schema").is_dir():
    raise ImportError(f"publisher must run from a repository checkout (pip install -e .): no schema/ under {REPO_ROOT}")


_CONTRACT_SCHEMA_PATH = REPO_ROOT / "schema" / "recommendation-package.schema.json"


_EVIDENCE_SCHEMA_PATH = REPO_ROOT / "schema" / "evidence.schema.json"
