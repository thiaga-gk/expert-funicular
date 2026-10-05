"""Canonical publisher outputs for the behavioral baseline (repo-reorg-design.md sections 13.3, 13.4, 21.3).

One place defines what "the same output" means, for the golden ``expected/`` files, the stored demo expectations,
``scripts/update_goldens.py`` and ``scripts/compare_single_file.py``:

- packages, ``manifest.json`` and ``evidence.json`` are compared byte for byte: they carry no wall-clock or path
  data (``generated_at`` derives from the evidence analysis time);
- ``workbook.xlsx`` is compared as a dump of every cell (value, type, number format) plus sheet names and states,
  because the zip container embeds creation timestamps;
- a collector-level golden case without a pinned ``analysis_time`` runs on the wall clock, so that one evidence
  field is normalized;
- ``publisher.log`` and stdout are not canonical: every line starts with a timestamp and names the output directory.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
for _path in (ROOT, ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from publisher.analyzer.collector.replay import evidence_from_dict  # noqa: E402
from publisher.common.utils import as_serializable  # noqa: E402
from publisher.governor.thresholds import Thresholds  # noqa: E402

DEMO_EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "demo"
WALL_CLOCK = "<wall-clock>"


def dumps(value: Any) -> str:
    """Serialization of every stored expectation (same as the package writer: indent 2, sorted keys)."""
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def workbook_cells(path: Path) -> Dict[str, Any]:
    """Every sheet's name, state and non-empty cells as ``coordinate -> [value, data_type, number_format]``."""
    from openpyxl import load_workbook

    wb = load_workbook(path)
    sheets = []
    for ws in wb.worksheets:
        cells = {}
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                value = cell.value.isoformat() if hasattr(cell.value, "isoformat") else cell.value
                cells[cell.coordinate] = [value, cell.data_type, cell.number_format]
        sheets.append({"name": ws.title, "state": ws.sheet_state, "cells": cells})
    return {"sheets": sheets}


def output_dir_canonical(out: Path) -> Dict[str, Any]:
    """Canonical view of one publisher output directory: relative path -> parsed JSON or workbook cell dump."""
    view: Dict[str, Any] = {}
    for path in sorted(out.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(out).as_posix()
        if rel.endswith(".json"):
            view[rel] = path.read_text(encoding="utf-8")
        elif rel.endswith(".xlsx"):
            view[rel] = dumps(workbook_cells(path))
    return view


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------

def golden_recommendation(p, case: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Packages built from an evidence-level case, in publisher order, exactly as the golden harness builds them."""
    from tests.golden.harness import recompute_forecast

    evidence = [evidence_from_dict(x) for x in case["evidence"]]
    if (case.get("expect") or {}).get("recompute_forecast"):
        for e in evidence:
            recompute_forecast(p, e)
    return [pkg.to_dict() for pkg in p.build_recommendation_packages(evidence, Thresholds())]


def golden_collector_evidence(p, case: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Serialized evidence the real collector extracts from a collector-level case (sqlite-backed CONFIG_EPOCHS_SQL).

    A case without ``analysis_time`` runs on the wall clock; that one field is normalized to ``WALL_CLOCK``.
    """
    from tests.golden.harness import collector_evidence

    items = [as_serializable(e) for e in collector_evidence(p, case)]
    if case.get("analysis_time") is None:
        for item in items:
            item["analysis_time"] = WALL_CLOCK
    return items


def golden_expected_files(p, name: str) -> Dict[str, str]:
    """``expected/<kind>.json`` contents for one case."""
    from tests.golden import load_case

    case = load_case(name)
    if "snapshots" in case:
        return {"evidence.json": dumps(golden_collector_evidence(p, case))}
    return {"recommendation.json": dumps(golden_recommendation(p, case))}


# ---------------------------------------------------------------------------
# Demo fleet
# ---------------------------------------------------------------------------

def demo_expected_files(p) -> Dict[str, str]:
    """Stored demo expectations: every package and the manifest in full, evidence.json and the workbook as digests.

    The demo runs through ``publisher.main`` (the CLI entry), so the expectations follow the CLI path end to end.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "demo"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = p.main(["--demo", "--output-dir", str(out), "--log-level", "ERROR"])
        if rc != 0:
            raise RuntimeError(f"demo run exited {rc}")
        view = output_dir_canonical(out)
        files = {rel: text for rel, text in view.items() if rel.startswith("packages/") or rel == "manifest.json"}
        digests = {rel: hashlib.sha256(text.encode("utf-8")).hexdigest() for rel, text in view.items() if rel not in files}
        files["digests.json"] = dumps(digests)
        return files


def stored_demo_expected() -> Dict[str, str]:
    return {path.relative_to(DEMO_EXPECTED).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(DEMO_EXPECTED.rglob("*")) if path.is_file()}


# ---------------------------------------------------------------------------
# Public contract artifacts
# ---------------------------------------------------------------------------

CONTRACT_EXPECTED = ROOT / "tests" / "fixtures" / "expected" / "contract"
CONTRACT_ARTIFACTS = (
    "schema/recommendation-package.schema.json",
    "schema/evidence.schema.json",
    "examples/recommendation-package.example.json",
)


def contract_expected_files() -> Dict[str, str]:
    """SHA-256 of the committed public contract artifacts: a structural move must leave them byte-identical."""
    return {"digests.json": dumps({rel: sha256_file(ROOT / rel) for rel in CONTRACT_ARTIFACTS})}
