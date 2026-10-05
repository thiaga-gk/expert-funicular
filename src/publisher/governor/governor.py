"""Governor facade: eligibility and run constraints for an analyzed fleet."""
from __future__ import annotations

from publisher.common.logging import LOGGER
from publisher.contracts.analysis import FleetAnalysis
from publisher.contracts.governance import GovernanceResult
from publisher.contracts.request import MODE_LIVE, RunContext
from publisher.governor.constraints import aws_region_price_guard
from publisher.governor.eligibility import unsupported_warehouse_exclusions


def govern(analysis: FleetAnalysis) -> GovernanceResult:
    """Eligibility: the analyzed (supported) warehouses, and the fail-closed exclusions for every other one."""
    return GovernanceResult(eligible=list(analysis.warehouses), exclusions=unsupported_warehouse_exclusions(analysis.evidence))


def run(context: RunContext, analysis: FleetAnalysis) -> GovernanceResult:
    """Given the evidence, what may the Optimizer consider? Live runs also enforce the AWS price-registry constraint.

    A live run whose region is missing from the AWS price registry stops here (SystemExit) unless the request allows
    publishing with AWS cost unavailable, which is logged.
    """
    request = context.request
    if request.mode == MODE_LIVE:
        registry = analysis.aws_price_registry or {}
        refusal = aws_region_price_guard(analysis.evidence, request.source_region, registry, allow_missing=request.allow_missing_aws_price)
        if refusal:
            raise SystemExit(refusal)
        if request.allow_missing_aws_price and request.source_region not in (registry.get("regions") or {}):
            LOGGER.warning("aws.region_not_in_registry region=%s: Pro/Classic AWS cost is unavailable (--allow-missing-aws-price)", request.source_region)
    return govern(analysis)
