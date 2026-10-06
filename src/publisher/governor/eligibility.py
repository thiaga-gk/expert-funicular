"""Eligibility: the supported-type gate fails closed per warehouse and is recorded as a manifest exclusion."""
from __future__ import annotations

from typing import Dict, List, Sequence

from publisher.analyzer.analyzer import _is_supported_warehouse
from publisher.contracts.source import WarehouseEvidence


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
