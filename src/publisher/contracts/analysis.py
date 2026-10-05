"""Analyzer output: findings and per-knob analysis results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from publisher.contracts.source import WarehouseEvidence


@dataclass(frozen=True)
class Finding:
    """Atomic explainability record tying a fact to its source, threshold, and epoch."""
    finding_id: str
    metric: str
    value: Any
    unit: str
    threshold: Any
    comparator: str
    source_table: str
    source_field: str
    epoch_id: str
    window: str
    interpretation: str
    # CURRENT, or CARRIED_ACROSS_CHANGE for evidence read across a configuration change that cannot affect it.
    epoch_scope: str = "CURRENT"


@dataclass
class AnalysisResult:
    """Analyzer output for one knob: facts and findings only, never a recommendation."""
    warehouse_id: str
    knob: str
    current_epoch_id: str
    findings: List[Finding]
    facts: Dict[str, Any]


@dataclass
class WarehouseAnalysis:
    """Per-knob findings for one supported, calibrated warehouse."""
    evidence: WarehouseEvidence
    analyses: Dict[str, AnalysisResult]


@dataclass
class FleetAnalysis:
    """Analyzer output for one run: every collected warehouse's evidence, and findings for the supported ones.

    ``warehouses`` holds only supported types, in deterministic (workspace, name, id) order; unsupported types are in
    ``evidence`` but never analyzed. ``unseen_billed_warehouses`` is None when the check did not run (demo, replay).
    """
    evidence: List[WarehouseEvidence]
    warehouses: List[WarehouseAnalysis]
    unseen_billed_warehouses: Optional[List[Dict[str, Any]]] = None
    aws_price_registry: Optional[Dict[str, Any]] = None
