"""Analyzer output: findings and per-knob analysis results."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List


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
