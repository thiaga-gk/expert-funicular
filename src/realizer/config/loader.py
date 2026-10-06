"""Input loading: the source package and the check that it is a package this Realizer reads, and the optional policy
override file.

The Realizer reads exactly the Reporter JSON Contract the Publisher writes (``SUPPORTED_PUBLISHER_CONTRACT``). A package
is accepted only when it passes the Publisher's committed JSON Schema and its id and full hash match its content, so a
package edited after publishing, or written by another contract version, never reaches the engine.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from publisher.contracts import reporter_contract as publisher_contract
from publisher.reviewer.package import validate_locked_package_schema
from realizer.common.utils import load_json

SUPPORTED_PUBLISHER_CONTRACT = "3.1.0"


def load_package(path: str | Path) -> Any:
    """The RecommendationPackage JSON at *path*, parsed (it is checked by ``validate_package``)."""
    return load_json(path)


def load_policy_mapping(path: Optional[str | Path]) -> Optional[Any]:
    """The policy override mapping at *path*, or ``None`` when no file is given (``RealizerPolicy.from_mapping`` input)."""
    return load_json(path) if path else None


def validate_package(package: Any) -> None:
    """Accept *package* only if it is an unmodified Publisher package of the supported contract; raise ``ValueError``.

    Checks, in order: a mapping; ``contract_version`` equal to ``SUPPORTED_PUBLISHER_CONTRACT``; the Publisher's committed
    Draft 2020-12 schema; the ``PKG-`` id and ``provenance.package_hash_full_sha256`` recomputed from the hashed fields;
    the ``realization`` block is the Publisher's empty placeholder (status NOT_APPLIED, no records; the Realizer's own
    lineage is the RPKG, never this block); and no carried-forward plan (reserved, never published yet).
    """
    if not isinstance(package, Mapping):
        raise ValueError("a RecommendationPackage must be a JSON object")
    version = package.get("contract_version")
    if version != SUPPORTED_PUBLISHER_CONTRACT:
        raise ValueError(f"unsupported Publisher contract: {version} (this Realizer reads {SUPPORTED_PUBLISHER_CONTRACT})")
    try:
        validate_locked_package_schema(package)
    except Exception as exc:  # jsonschema.ValidationError: report the location, never the package values
        location = "/".join(str(p) for p in getattr(exc, "absolute_path", ())) or "<root>"
        raise ValueError(f"package fails the Reporter JSON Contract {SUPPORTED_PUBLISHER_CONTRACT} schema at {location}") from exc
    payload = publisher_contract.package_hash_payload_from_document(package)
    if publisher_contract.content_id("PKG", payload) != package["recommendation_package_id"]:
        raise ValueError(f"package id {package['recommendation_package_id']} does not match the package content")
    if publisher_contract.sha256_hex(payload) != package["provenance"]["package_hash_full_sha256"]:
        raise ValueError("provenance.package_hash_full_sha256 does not match the package content")
    realization = package["realization"]
    if realization.get("status") != "NOT_APPLIED" or realization.get("realization_records"):
        raise ValueError("package realization block must be the Publisher's empty NOT_APPLIED placeholder")
    if package["carried_forward_plan"] is not None:
        raise ValueError("packages with a carried-forward plan are not supported yet")
