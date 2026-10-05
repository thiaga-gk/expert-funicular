"""Run manifest: what one run emitted, excluded and checked (assembled here, written by the Reporter)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from publisher.config.versions import PUBLISHER_VERSION
from publisher.contracts import reporter_contract as rc
from publisher.contracts import workbook_contract as wc


def build_manifest(
    packages: Sequence[rc.RecommendationPackage],
    entries: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, str]],
    unseen_billed_warehouses: Optional[Sequence[Mapping[str, Any]]],
    workbook_path: Optional[Path],
    evidence_path: Path,
) -> Dict[str, Any]:
    """The run manifest: versions, one entry per package, exclusions, the unseen-warehouse check and the output files."""
    return {
        "publisher_version": PUBLISHER_VERSION,
        "reporter_contract_version": rc.CONTRACT_VERSION,
        "generated_at": max((p.provenance.generated_at for p in packages), default=dt.datetime.now(dt.timezone.utc).isoformat()),
        "package_count": len(packages),
        "packages": entries,
        "excluded_warehouse_count": len(exclusions),
        "excluded_warehouses": exclusions,
        # Live runs only: billed warehouses with no configuration snapshot (None = not checked: demo or replay).
        "unseen_billed_warehouse_check": "NOT_CHECKED" if unseen_billed_warehouses is None else "CHECKED",
        "unseen_billed_warehouse_count": None if unseen_billed_warehouses is None else len(unseen_billed_warehouses),
        "unseen_billed_warehouses": [dict(x) for x in unseen_billed_warehouses or []],
        "canonical_json_semantics": f"Each packages/*.json file is exactly one Reporter JSON Contract v{rc.CONTRACT_VERSION} RecommendationPackage.",
        "workbook_contract_version": wc.WORKBOOK_CONTRACT_VERSION,
        "workbook_status": "GENERATED" if workbook_path is not None else "SKIPPED",
        "workbook_file": None if workbook_path is None else workbook_path.name,
        "evidence_file": evidence_path.name,
    }
