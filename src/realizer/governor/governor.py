"""Governor facade: given the analyzed observation and the policy, what conclusions are permitted.

The Governor evaluates the policy checks once the Analyzer has run and hands them over as ``Deferred`` values, so the
conclusions a run never reaches are never allowed to fail it. It reconciles nothing, prices nothing and writes nothing.
"""
from __future__ import annotations

from realizer.common.utils import Deferred
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import counterfactual_eligible, economic_quality_result, package_currency, readiness, validate_performance


def run(context: RunContext, analysis: RealizationAnalysis) -> GovernanceResult:
    """The policy checks of this run's observation, each held lazily (all absent for a lineage-only run).

    The checks are pure, so evaluating them before they are needed changes nothing; a check whose input is malformed
    keeps its exception for the consumer to raise at the point it needs the result (``Deferred.get``).
    """
    policy = context.policy
    snapshot_id = Deferred.of(lambda: policy.snapshot_id)
    observation = analysis.observation
    if observation is None:
        return GovernanceResult(policy=policy, policy_snapshot_id=snapshot_id)
    raw, package = observation.raw, context.request.package
    performance = Deferred.of(validate_performance, raw, policy, package)
    return GovernanceResult(
        policy=policy,
        policy_snapshot_id=snapshot_id,
        # Readiness reads the guardrail verdict only when it can be reached: a malformed performance input still raises
        # where the performance verdict is needed, never earlier.
        readiness=Deferred.of(readiness, raw, policy, performance.value if performance.error is None else None),
        performance=performance,
        counterfactual=Deferred.of(counterfactual_eligible, raw),
        economic_quality=Deferred.of(economic_quality_result, raw, policy, package_currency(context.request.package)),
    )
