"""Run manifest: what one run emitted, excluded and checked (assembled here, written by the Reporter)."""
from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

from publisher.config.versions import PUBLISHER_VERSION
from publisher.contracts import reporter_contract as rc
from publisher.contracts import workbook_contract as wc
from publisher.contracts.scope import FULL_SCOPE, RunScope


def run_scope_summary(scope: RunScope) -> Optional[Dict[str, Any]]:
    """The run-level scope for the manifest: None for the full scope, otherwise the in-scope knobs per scenario type
    (the same shape as a package's ``run_scope.scenarios``) and a readable label."""
    if scope.is_full:
        return None
    return {"label": scope.describe(), "scenarios": [{"scenario_type": t, "knobs": k} for t, k in scope.scenario_knobs()]}


def warehouse_selection_summary(scope: RunScope, selected_count: int, not_selected_count: int) -> Dict[str, Any]:
    """The run's warehouse selection: what was requested (``all`` or the selectors) and how many warehouses it kept."""
    return {
        "requested": "all" if not scope.warehouses else [s.label() for s in scope.warehouses],
        "selected_count": selected_count,
        "not_selected_count": not_selected_count,
    }


def build_manifest(
    packages: Sequence[rc.RecommendationPackage],
    entries: Sequence[Mapping[str, Any]],
    exclusions: Sequence[Mapping[str, str]],
    unseen_billed_warehouses: Optional[Sequence[Mapping[str, Any]]],
    workbook_path: Optional[Path],
    evidence_path: Path,
    run_scope: Optional[Mapping[str, Any]] = None,
    warehouse_selection: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """The run manifest: versions, the run scope and warehouse selection, one entry per package, exclusions, the
    unseen-warehouse check and the output files. ``run_scope`` None is the full scope; ``warehouse_selection`` None
    means every warehouse in the evidence was selected."""
    return {
        "publisher_version": PUBLISHER_VERSION,
        "reporter_contract_version": rc.CONTRACT_VERSION,
        "generated_at": max((p.provenance.generated_at for p in packages), default=dt.datetime.now(dt.timezone.utc).isoformat()),
        "run_scope": None if run_scope is None else dict(run_scope),
        "warehouse_selection": dict(warehouse_selection) if warehouse_selection is not None
        else warehouse_selection_summary(FULL_SCOPE, len(packages) + len(exclusions), 0),
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
