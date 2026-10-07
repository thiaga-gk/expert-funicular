"""Reporter facade: write the realization package and its snapshot, and project the legacy compatibility view.

The Reporter presents what the Packager projected: it decides, infers and recomputes nothing and never touches the
ledger. ``run`` writes the realization package (``realization/packages/<RPKG>.json``) and then the snapshot
(``realization/snapshots/<RSNAP>.json``), the same canonical JSON in both, after checking it against the committed schema. ``legacy_compatibility`` is the Reporter
v1.0.0 compatibility projection of a snapshot (nothing on the realize path calls it).

The single-file release's ``.xlsx`` renderer was not carried over: it needs the ``artifact_tool`` runtime and the retired
Publisher's workbook reporter, and nothing on the realize path calls it. The workbook projection it renders is
``realizer.contracts.report``; a renderer is added when it has code to hold.
"""
from __future__ import annotations

from typing import Any, Dict, List

from realizer.common.utils import write_json
from realizer.contracts import realization as rc
from realizer.contracts.realization import PackagingResult
from realizer.contracts.realization_schema import validate_realization_package
from realizer.contracts.report import RealizationReport
from realizer.contracts.request import RunContext


def run(context: RunContext, packaged: PackagingResult) -> RealizationReport:
    """Write the realization package, then the snapshot, from the projected snapshot (the same payload in both)."""
    snapshot = packaged.realization
    payload = snapshot.to_dict()
    validate_realization_package(payload)
    output_dir = context.request.output_dir
    package_path = output_dir / "realization" / "packages" / f"{context.realization_package_id}.json"
    snapshot_path = output_dir / "realization" / "snapshots" / f"{snapshot.snapshot_id}.json"
    write_json(package_path, payload)
    write_json(snapshot_path, payload)
    return RealizationReport(realization=snapshot, files=(package_path, snapshot_path))


# ---------------------------------------------------------------------------
# Compatibility projection (legacy Reporter v1.0.0 remains immutable)
# ---------------------------------------------------------------------------


def legacy_compatibility(snapshot: rc.RealizationPackage) -> Dict[str, Any]:
    state = snapshot.current_state["realization_state"]
    mapping = {
        "NOT_APPLIED": "NOT_APPLIED",
        "APPLIED": "APPLIED",
        "VALIDATING": "VALIDATING",
        "REALIZED": "REALIZED",
        "UNDER_REALIZED": "REALIZED",
        "VALIDATION_FAILED": "FAILED_VALIDATION",
        "ROLLED_BACK": "ROLLED_BACK",
        "MONITORING": "REALIZED",
        "INVALIDATED": "APPLIED",
    }
    if state not in mapping:
        return {"status": rc.CompatibilityStatus.UNREPRESENTABLE.value, "legacy_realization_status": None, "reasons": [f"REALIZER_STATE_{state}_HAS_NO_LEGACY_MAPPING"]}
    reasons: List[str] = []
    quality = rc.CompatibilityStatus.LOSSLESS
    if state in {"UNDER_REALIZED", "MONITORING"}:
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append(f"{state}_COLLAPSES_TO_LEGACY_REALIZED")
    if state == "INVALIDATED":
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("INVALIDATED_COLLAPSES_TO_LEGACY_APPLIED")
    if snapshot.application.get("state") == "APPLIED" and not snapshot.application.get("actor"):
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("LEGACY_APPLIED_BY_REQUIRED_BUT_ACTOR_UNKNOWN")
    if snapshot.application.get("implementation_conformance") == "MODIFIED":
        quality = rc.CompatibilityStatus.LOSSY
        reasons.append("LEGACY_HAS_NO_MODIFIED_CONFORMANCE")
    return {"status": quality.value, "legacy_realization_status": mapping[state], "reasons": reasons}
