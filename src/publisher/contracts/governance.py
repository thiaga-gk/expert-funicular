"""Governor output: which analyzed warehouses the Optimizer may consider, which were excluded or not selected, and the run scope."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from publisher.contracts.analysis import WarehouseAnalysis
from publisher.contracts.scope import FULL_SCOPE, RunScope


@dataclass
class GovernanceResult:
    """``eligible`` keeps the Analyzer's deterministic order; ``exclusions`` is one manifest entry per excluded selected
    warehouse; ``not_selected`` lists the warehouses outside the run's warehouse selection (identity only); ``scope`` is
    the run scope the Optimizer, Reviewer and Packager apply."""
    eligible: List[WarehouseAnalysis]
    exclusions: List[Dict[str, str]] = field(default_factory=list)
    not_selected: List[Dict[str, str]] = field(default_factory=list)
    scope: RunScope = FULL_SCOPE
