"""Golden case loader: one folder per case under ``tests/golden/cases/<name>/``.

Layout of a case folder:

    case.yaml                   name, description, optional notes/now/analysis_time, and the ``expect`` block
    source/evidence.json        normalized evidence items (evidence-level cases)
    source/snapshots.json       system.compute.warehouses rows (collector-level cases)
    source/collector_rows.json  optional {query label: rows} for the other collector queries
    expected/recommendation.json  canonical packages the publisher builds from the evidence (evidence-level cases)
    expected/evidence.json      serialized WarehouseEvidence the collector extracts (collector-level cases)

``load_case`` returns the case as one payload dict (the keys above, without ``expected``), so a test reads a case
without knowing the folder layout. ``expected/`` is written only by ``scripts/update_goldens.py``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

CASES = Path(__file__).resolve().parent / "cases"
_SOURCES = ("evidence", "snapshots", "collector_rows")


def case_names() -> List[str]:
    return sorted(p.name for p in CASES.iterdir() if (p / "case.yaml").is_file())


def case_dir(name: str) -> Path:
    path = CASES / name
    if not (path / "case.yaml").is_file():
        raise FileNotFoundError(f"golden case {name!r} not found under {CASES}")
    return path


def load_case(name: str) -> Dict[str, Any]:
    path = case_dir(name)
    payload = yaml.safe_load((path / "case.yaml").read_text(encoding="utf-8"))
    for key in _SOURCES:
        source = path / "source" / f"{key}.json"
        if source.is_file():
            payload[key] = json.loads(source.read_text(encoding="utf-8"))
    return payload


def load_expected(name: str, kind: str) -> Any:
    """``kind`` is ``recommendation`` or ``evidence``."""
    return json.loads((case_dir(name) / "expected" / f"{kind}.json").read_text(encoding="utf-8"))
