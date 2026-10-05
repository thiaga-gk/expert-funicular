"""Governor output: which analyzed warehouses the Optimizer may consider, and which were excluded and why."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from publisher.contracts.analysis import WarehouseAnalysis


@dataclass
class GovernanceResult:
    """``eligible`` keeps the Analyzer's deterministic order; ``exclusions`` is one manifest entry per excluded warehouse."""
    eligible: List[WarehouseAnalysis]
    exclusions: List[Dict[str, str]] = field(default_factory=list)
