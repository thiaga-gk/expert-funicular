"""Governor facade: eligibility and run constraints for an analyzed fleet.

The Governor decides what the Optimizer may consider; it neither observes (Analyzer) nor recommends (Optimizer).
"""
from __future__ import annotations

from publisher.common.errors import PublisherError
from publisher.common.logging import LOGGER
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.request import MODE_LIVE, RunContext
from publisher.contracts.scope import FULL_SCOPE, RunScope
from publisher.governor.constraints import aws_region_price_guard
from publisher.governor.eligibility import selected_warehouse_keys, unsupported_warehouse_exclusions


def govern(analysis: FleetAnalysis, scope: RunScope = FULL_SCOPE) -> GovernanceResult:
    """Eligibility: the selected, analyzed (supported) warehouses and the fail-closed exclusions for the other selected ones.

    The run scope's warehouse selection (all by default) narrows the fleet first; a selector that names no warehouse
    raises PublisherError. Eligible warehouses keep the Analyzer's order. Each exclusion is logged as a WARNING
    (identity and reason code only) and is recorded in the run manifest by the Reporter; the rest of the selection is
    still evaluated. The scope is passed on for the Optimizer, Reviewer and Packager.
    """
    selected = selected_warehouse_keys(analysis.evidence, scope.warehouses)
    exclusions = [x for x in unsupported_warehouse_exclusions(analysis.evidence) if (x["workspace_id"], x["warehouse_id"]) in selected]
    not_selected = [
        {"workspace_id": e.workspace_id, "warehouse_id": e.warehouse_id}
        for e in sorted(analysis.evidence, key=lambda x: (x.workspace_id, x.warehouse_id))
        if (e.workspace_id, e.warehouse_id) not in selected
    ]
    eligible = [w for w in analysis.warehouses if (w.evidence.workspace_id, w.evidence.warehouse_id) in selected]
    for x in exclusions:
        LOGGER.warning("governor.excluded workspace_id=%s warehouse_id=%s reason_code=%s", x["workspace_id"], x["warehouse_id"], x["reason_code"])
    for w in eligible:
        LOGGER.debug("governor.eligible workspace_id=%s warehouse_id=%s", w.evidence.workspace_id, w.evidence.warehouse_id)
    LOGGER.info("governor.govern_done eligible=%d excluded=%d not_selected=%d scope=%s",
                len(eligible), len(exclusions), len(not_selected), scope.describe())
    return GovernanceResult(eligible=eligible, exclusions=exclusions, not_selected=not_selected, scope=scope)


def run(context: RunContext, analysis: FleetAnalysis) -> GovernanceResult:
    """Given the evidence, what may the Optimizer consider? Live runs also enforce the AWS price-registry constraint.

    A live run whose region is missing from the AWS price registry stops here (PublisherError) unless the request allows
    publishing with AWS cost unavailable, which is logged as a WARNING. Demo and replay runs skip the constraint.
    """
    request = context.request
    if request.mode == MODE_LIVE:
        registry = analysis.aws_price_registry or {}
        refusal = aws_region_price_guard(analysis.evidence, request.source_region, registry, allow_missing=request.allow_missing_aws_price)
        if refusal:
            raise PublisherError(refusal)
        if request.allow_missing_aws_price and request.source_region not in (registry.get("regions") or {}):
            LOGGER.warning("aws.region_not_in_registry region=%s: Pro/Classic AWS cost is unavailable (--allow-missing-aws-price)", request.source_region)
    return govern(analysis, context.scope)
