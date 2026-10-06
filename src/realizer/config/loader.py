"""Input loading: the source package, its accepted shape and the optional policy override file.

The Realizer has no execution modes or run-scope inputs; loading is only what the single-file CLI and ``realize()``
already did, in the same order (package, then observation, then policy).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from realizer.common.utils import load_json


def load_package(path: str | Path) -> Any:
    """The RecommendationPackage JSON at *path*, parsed (its shape is checked by ``validate_package_shape``)."""
    return load_json(path)


def load_policy_mapping(path: Optional[str | Path]) -> Optional[Any]:
    """The policy override mapping at *path*, or ``None`` when no file is given (``RealizerPolicy.from_mapping`` input)."""
    return load_json(path) if path else None


def validate_package_shape(package: Mapping[str, Any]) -> None:
    required = ["recommendation_package_id", "contract_version", "warehouse", "current_config", "candidates", "scenarios", "recommended_plan", "provenance"]
    missing = [k for k in required if k not in package]
    if missing:
        raise ValueError(f"PKG missing required fields: {missing}")
    if not str(package["recommendation_package_id"]).startswith("PKG-"):
        raise ValueError("invalid recommendation_package_id")
    if package.get("contract_version") != "1.0.0":
        raise ValueError(f"unsupported Publisher contract: {package.get('contract_version')}")
