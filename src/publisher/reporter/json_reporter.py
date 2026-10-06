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
    """A filesystem-safe fragment of an ID: anything outside [A-Za-z0-9_.-] becomes "_" (empty becomes "unknown"), so
    the package file name can never escape packages/."""
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
    """Write the run outputs under *output_dir* and return a map of output key to file path.

    Each package is schema-validated, then written as ``packages/<workspace>__<warehouse>.json`` (sorted keys). Then
    ``evidence.json`` (replay envelope), ``workbook.xlsx`` when *generate_workbook*, and ``manifest.json`` (which lists
    the packages, exclusions, unseen billed warehouses and the other files). Keys: ``package:<workspace>:<warehouse>``,
    ``workbook`` (only when generated), ``manifest`` and ``evidence``.

    Every packages/*.json file remains the source of truth. workbook.xlsx is a presentation-only projection and is
    never read back into the decision engine. A schema failure propagates (jsonschema ValidationError from the Reviewer's schema
    check, which aborts the run before that package is written); a missing openpyxl raises RuntimeError with install guidance unless the workbook is disabled.
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
        LOGGER.debug("contract.v1.package.written path=%s", path)
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
    LOGGER.info("evidence.written path=%s warehouses=%d", evidence_path, len(evidence))
    workbook_path: Optional[Path] = None
    if generate_workbook:
        LOGGER.info("workbook.v1.start packages=%d", len(packages))
        try:
            from publisher.reporter.workbook_reporter import write_workbook
        except ImportError as exc:
            LOGGER.warning("workbook.v1.failed reason=openpyxl_missing")
            raise RuntimeError("Workbook generation requires openpyxl (pip install openpyxl). Use --no-workbook only when producing JSON packages without it.") from exc
        try:
            workbook_path = write_workbook(output_dir / "workbook.xlsx", packages, unsupported_warehouse_exclusions(evidence))
        except Exception as exc:
            LOGGER.warning("workbook.v1.failed error_type=%s", type(exc).__name__)  # type only: the message may echo cell content
            raise
        result["workbook"] = str(workbook_path)
        LOGGER.info("workbook.v1.done path=%s", workbook_path)
    else:
        LOGGER.warning("workbook.v1.skipped reason=no_workbook packages=%d", len(packages))
    manifest_path = output_dir / "manifest.json"
    exclusions = unsupported_warehouse_exclusions(evidence)
    manifest = build_manifest(packages, entries, exclusions, unseen_billed_warehouses, workbook_path, evidence_path)
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    LOGGER.info("manifest.written path=%s packages=%d exclusions=%d", manifest_path, len(packages), len(exclusions))
    result["manifest"] = str(manifest_path)
    result["evidence"] = str(evidence_path)
    LOGGER.info("contract.v1.package.done packages=%d", len(packages))
    return result
