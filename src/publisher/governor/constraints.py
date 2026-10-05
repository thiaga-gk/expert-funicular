"""Run-level constraints: a live run needs AWS instance prices for its region when it has Pro/Classic warehouses."""
from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from publisher.contracts.source import WarehouseEvidence


def aws_region_price_guard(evidence: Sequence[WarehouseEvidence], source_region: str, aws_registry: Mapping[str, Any], *, allow_missing: bool) -> Optional[str]:
    """Return a refusal message when supported Pro/Classic warehouses need AWS prices for a region the registry lacks.

    Without the region's instance prices every Pro/Classic AWS, total and package figure would silently be null (or,
    with a wrong label, priced for the wrong region); a live run stops instead unless the operator opts in.
    """
    if allow_missing:
        return None
    regions = sorted((aws_registry.get("regions") or {}).keys())
    if source_region in regions:
        return None
    pro_classic = [e for e in evidence if e.current_epoch.config.warehouse_type in {"PRO", "CLASSIC"}]
    if not pro_classic:
        return None
    return (f"--source-region {source_region or '(empty)'!r} is not in the AWS price registry (regions: {', '.join(regions) or 'none'}); "
            f"{len(pro_classic)} Pro/Classic warehouse(s) need AWS instance prices. Pass the workspace's AWS region, extend the "
            "registry, or pass --allow-missing-aws-price to publish with AWS cost unavailable.")
