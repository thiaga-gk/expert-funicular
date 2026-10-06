"""Eligibility: the supported-type gate fails closed per warehouse and is recorded as a manifest exclusion."""
from __future__ import annotations

from typing import Dict, List, Sequence, Set, Tuple

from publisher.analyzer.analyzer import _is_supported_warehouse
from publisher.common.errors import PublisherError
from publisher.contracts.scope import WarehouseSelector
from publisher.contracts.source import WarehouseEvidence


def selected_warehouse_keys(evidence: Sequence[WarehouseEvidence], selectors: Sequence[WarehouseSelector]) -> Set[Tuple[str, str]]:
    """``(workspace_id, warehouse_id)`` of the warehouses a run's selection names; every warehouse when it names none.

    Fails closed: a selector that matches no warehouse in the evidence, or a bare warehouse ID that matches warehouses
    in more than one workspace, raises PublisherError (CLI exit status 2) instead of silently reporting less.
    """
    keys = {(e.workspace_id, e.warehouse_id) for e in evidence}
    if not selectors:
        return keys
    chosen: Set[Tuple[str, str]] = set()
    for s in selectors:
        matches = sorted(k for k in keys if k[1] == s.warehouse_id and (s.workspace_id is None or k[0] == s.workspace_id))
        if not matches:
            raise PublisherError(f"--warehouse {s.label()} is not in the evidence of this run")
        if len(matches) > 1:
            raise PublisherError(f"--warehouse {s.label()} exists in {len(matches)} workspaces; use <workspace_id>/<warehouse_id>")
        chosen.add(matches[0])
    return chosen


def unsupported_warehouse_exclusions(evidence: Sequence[WarehouseEvidence]) -> List[Dict[str, str]]:
    """Supported-type gate: list warehouses that fail closed, one entry each, in deterministic order.

    An unsupported or future type is never routed into Pro/Classic/Serverless algorithms; it is excluded
    per warehouse and recorded in the run manifest, so the rest of the fleet is still evaluated. Entries carry
    identity, name, the unsupported type and reason_code UNSUPPORTED_WAREHOUSE_TYPE, sorted by (workspace_id, warehouse_id).
    """
    return [
        {
            "workspace_id": e.workspace_id,
            "warehouse_id": e.warehouse_id,
            "warehouse_name": e.warehouse_name,
            "warehouse_type": e.current_epoch.config.warehouse_type,
            "reason_code": "UNSUPPORTED_WAREHOUSE_TYPE",
        }
        for e in sorted(evidence, key=lambda x: (x.workspace_id, x.warehouse_id))
        if not _is_supported_warehouse(e)
    ]
