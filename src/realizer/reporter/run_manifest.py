"""The run manifest of a fleet run (``<run folder>/manifest.json``): what the run read and what it wrote, per warehouse.

A presentation of what the fleet run already decided; nothing is computed here. Deterministic: sorted keys, warehouses
in run order, paths of written files relative to the run folder, and no wall-clock time (the run folder's name carries
the start time; ``as_of_utc`` is the measurement time the caller gave, else null).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from realizer.config.loader import SUPPORTED_PUBLISHER_CONTRACT
from realizer.config.settings import PRODUCT_VERSION, REALIZER_VERSION
from realizer.contracts.fleet import FleetRequest
from realizer.contracts.measurement import MEASUREMENT_EVIDENCE_FORMAT_VERSION
from realizer.contracts.observation import OBSERVATION_FORMAT_VERSION
from realizer.contracts.realization import CONTRACT_VERSION, LEDGER_CONTRACT_VERSION
from realizer.contracts.report import RUN_STATUSES, WarehouseRun

RUN_MANIFEST_VERSION = "1.0.0"


def build_run_manifest(request: FleetRequest, runs: Sequence[WarehouseRun], files: Mapping[tuple, Sequence[str]],
                       workbook_file: Optional[str]) -> Dict[str, Any]:
    """The manifest document: versions and contracts, the source, the Publisher run, the state directory, the workbook,
    the status counts and one row per warehouse (``files``: the paths each warehouse's runs wrote under the run folder,
    keyed by ``(workspace_id, warehouse_id)``)."""
    return {
        "run_manifest_version": RUN_MANIFEST_VERSION,
        "realizer_version": REALIZER_VERSION,
        "release": PRODUCT_VERSION,
        "contracts": {
            "reads_reporter_json_contract": SUPPORTED_PUBLISHER_CONTRACT,
            "reads_observation_format": OBSERVATION_FORMAT_VERSION,
            "reads_measurement_evidence_format": MEASUREMENT_EVIDENCE_FORMAT_VERSION,
            "writes_realization_contract": CONTRACT_VERSION,
            "writes_ledger_contract": LEDGER_CONTRACT_VERSION,
        },
        "source": request.source,
        "as_of_utc": request.as_of,
        "evidence_dir": None if request.evidence_dir is None else str(request.evidence_dir),
        "publisher_run": {
            "run_dir": str(request.publisher_run.run_dir),
            "manifest_package_count": request.publisher_run.package_count,
            "selected_package_count": len(request.publisher_run.entries),
        },
        "state_dir": str(request.state_dir),
        "workbook_file": workbook_file,
        "status_counts": {status: sum(1 for r in runs if r.status == status) for status in RUN_STATUSES},
        "warehouses": [{
            "workspace_id": r.workspace_id,
            "warehouse_id": r.warehouse_id,
            "recommendation_package_id": r.recommendation_package_id,
            "status": r.status,
            "reason": r.reason,
            "realization_package_ids": sorted(x.realization_package_id for x in r.realizations),
            "files": sorted(files.get((r.workspace_id, r.warehouse_id), ())),
        } for r in runs],
    }


def write_run_manifest(path: Path, manifest: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
