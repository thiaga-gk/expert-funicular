"""Reconciler facade: how the observed actual state relates to the published plan and its lifecycle.

The Reconciler matches the observation to the plan and decides the lifecycle events it implies. It creates no
optimization targets, prices nothing and writes nothing; the engine records the events it decides.
"""
from __future__ import annotations

from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.governance import GovernanceResult
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RunContext
from realizer.reconciler import lifecycle, matching


def run(context: RunContext, analysis: RealizationAnalysis, governance: GovernanceResult) -> ReconciliationResult:
    """The plan match and the lifecycle events of this run's observation (nothing for a lineage-only run).

    ``run`` never raises. A failure (malformed package plan or observation, a policy whose snapshot cannot be hashed,
    insufficient-evidence input) is kept as ``halt`` together with the events decided before it, so the engine records
    exactly those and then raises it: the ledger a failed run leaves behind is the one it always left.
    """
    observed = analysis.observation
    if observed is None:
        return ReconciliationResult()
    state = lifecycle.LifecyclePlan()
    try:
        state.match = matching.match(context.request.package, observed.raw, observed.observed_config)
        lifecycle.plan(state, context, analysis, governance)
    except Exception as exc:  # noqa: BLE001 - kept, raised by the engine after the events decided before it
        state.halt = exc
    return state.result()
