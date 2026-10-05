"""Packager output: the reviewed decision-layer result and the canonical public package for each warehouse."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from publisher.contracts import reporter_contract as rc
from publisher.contracts.review import WarehouseOptimization


@dataclass
class PackagingResult:
    """``warehouses`` (internal scenarios, for the package gate) and ``packages`` (public, frozen) are in the same order."""
    warehouses: List[WarehouseOptimization]
    packages: List[rc.RecommendationPackage]
