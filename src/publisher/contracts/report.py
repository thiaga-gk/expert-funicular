"""Reporter output: what one run wrote and its headline counts."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from publisher.contracts import reporter_contract as rc


@dataclass
class ReportContract:
    """``files`` maps an output label (``package:<workspace>:<warehouse>``, ``workbook``, ``manifest``, ``evidence``) to its path."""
    files: Dict[str, str]
    packages: List[rc.RecommendationPackage]
    analyzed_count: int
    excluded_count: int
    candidate_count: int
    scenario_count: int
