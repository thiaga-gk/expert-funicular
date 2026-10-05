"""JSON reporter: persist each frozen package (schema-validated at write), evidence.json, the workbook projection and the manifest."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from publisher.analyzer.collector.replay import save_evidence
from publisher.common.logging import LOGGER
from publisher.contracts import reporter_contract as rc
from publisher.contracts.source import WarehouseEvidence
from publisher.governor.eligibility import unsupported_warehouse_exclusions
from publisher.packager.manifest import build_manifest
from publisher.reviewer.package import validate_locked_package_schema


def _safe_artifact_name(value: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in str(value))
    return text or "unknown"


def package_locked_contract(
    output_dir: Path,
    packages: Sequence[rc.RecommendationPackage],
    evidence: Sequence[WarehouseEvidence],
    *,
    generate_workbook: bool = True,
    unseen_billed_warehouses: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, str]:
    """Write locked JSON packages plus the Workbook Contract projection.

    Every packages/*.json file remains the source of truth. workbook.xlsx is a
    presentation-only projection and is never read back into the decision engine.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    packages_dir = output_dir / "packages"
    packages_dir.mkdir(parents=True, exist_ok=True)
    LOGGER.info("contract.v1.package.start output_dir=%s packages=%d", output_dir, len(packages))
    entries = []
    result: Dict[str, str] = {}
    for package in packages:
        payload = package.to_dict()
        validate_locked_package_schema(payload)
        name = f"{_safe_artifact_name(package.warehouse.workspace_id)}__{_safe_artifact_name(package.warehouse.warehouse_id)}.json"
        path = packages_dir / name
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        entries.append({
            "recommendation_package_id": package.recommendation_package_id,
            "workspace_id": package.warehouse.workspace_id,
            "warehouse_id": package.warehouse.warehouse_id,
            "warehouse_name": package.warehouse.warehouse_name,
            "epoch_id": package.warehouse.current_epoch.epoch_id,
            "package_hash_full_sha256": package.provenance.package_hash_full_sha256,
            "path": f"packages/{name}",
        })
        result[f"package:{package.warehouse.workspace_id}:{package.warehouse.warehouse_id}"] = str(path)

    evidence_path = output_dir / "evidence.json"
    save_evidence(evidence_path, evidence)
    workbook_path: Optional[Path] = None
    if generate_workbook:
        LOGGER.info("workbook.v1.start packages=%d", len(packages))
        try:
            from publisher.reporter.workbook_reporter import write_workbook
        except ImportError as exc:
            raise RuntimeError("Workbook generation requires openpyxl (pip install openpyxl). Use --no-workbook only when producing JSON packages without it.") from exc
        workbook_path = write_workbook(output_dir / "workbook.xlsx", packages, unsupported_warehouse_exclusions(evidence))
        result["workbook"] = str(workbook_path)
        LOGGER.info("workbook.v1.done path=%s", workbook_path)
    manifest_path = output_dir / "manifest.json"
    exclusions = unsupported_warehouse_exclusions(evidence)
    manifest = build_manifest(packages, entries, exclusions, unseen_billed_warehouses, workbook_path, evidence_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["manifest"] = str(manifest_path)
    result["evidence"] = str(evidence_path)
    LOGGER.info("contract.v1.package.done packages=%d", len(packages))
    return result
