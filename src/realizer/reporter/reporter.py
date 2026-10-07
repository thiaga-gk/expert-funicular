"""Reporter facade: write the realization package and its snapshot, and project the legacy compatibility view.

The Reporter presents what the Packager projected: it decides, infers and recomputes nothing and never touches the
ledger. ``run`` writes the realization package (``realization/packages/<RPKG>.json``) and then the snapshot
(``realization/snapshots/<RSNAP>.json``), the same canonical JSON in both, after checking it against the committed schema;
then the same two files for each other lineage of the warehouse the run re-projected, and the warehouse's state index
(``realization/state/<workspace>__<warehouse>.json``). ``legacy_compatibility`` is the Reporter
v1.0.0 compatibility projection of a snapshot (nothing on the realize path calls it).

The single-file release's ``.xlsx`` renderer was not carried over: it needs the ``artifact_tool`` runtime and the retired
Publisher's workbook reporter, and nothing on the realize path calls it. The workbook projection it renders is
``realizer.contracts.report``; a renderer is added when it has code to hold.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

from realizer.common.utils import write_json
from realizer.contracts import realization as rc
from realizer.contracts.realization import PackagingResult
from realizer.contracts.realization_schema import validate_realization_package
from realizer.contracts.report import RealizationReport
from realizer.contracts.request import RunContext
from realizer.packager.lineage import state_index_path


def _write(output_dir: Path, snapshot: rc.RealizationPackage) -> Tuple[Path, Path]:
    """Write a realization package, then its snapshot (the same payload in both); return the two paths."""
    payload = snapshot.to_dict()
    validate_realization_package(payload)
    package_path = output_dir / "realization" / "packages" / f"{snapshot.realization_package_id}.json"
    snapshot_path = output_dir / "realization" / "snapshots" / f"{snapshot.snapshot_id}.json"
    write_json(package_path, payload)
    write_json(snapshot_path, payload)
    return package_path, snapshot_path


def run(context: RunContext, packaged: PackagingResult) -> RealizationReport:
    """Write the realization package, then the snapshot, from the projected snapshot (the same payload in both); then
    the re-projected other lineages' files and the state index."""
    output_dir = context.request.output_dir
    files: List[Path] = list(_write(output_dir, packaged.realization))
    for other in packaged.others:
        files.extend(_write(output_dir, other))
    if packaged.state_index is not None:
        path = state_index_path(context.request.state_root, context.request.package)
        write_json(path, packaged.state_index)
        files.append(path)
    return RealizationReport(realization=packaged.realization, files=tuple(files))


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
