"""Selection: the package's overall status from the recommended scenario and the candidates."""
from __future__ import annotations

from typing import Sequence

from publisher.contracts import reporter_contract as rc


def _overall_status(candidates: Sequence[rc.CandidateRecommendation], scenarios: Sequence[rc.Scenario]) -> rc.AssessmentStatus:
    """Package status from the recommended plan; the migration experiment alone never makes a warehouse optimizable.

    Steps to apply in the recommended plan: OPTIMIZATION_AVAILABLE (REVIEW_REQUIRED when every step is a review).
    An empty recommended plan with a compatible-or-unvalidated alternative whose priced base savings are positive:
    REVIEW_REQUIRED. Otherwise
    the most severe reason nothing ships: BLOCKED, then INSUFFICIENT_EVIDENCE, else NO_CHANGE.
    """
    S = rc.RecommendationStatus
    by_id = {c.candidate_id: c.status for c in candidates}

    def step_statuses(s: rc.Scenario) -> set:
        return {by_id[x.candidate_id] for x in s.execution_plan if x.candidate_id in by_id}

    plan = set().union(*(step_statuses(s) for s in scenarios if s.status == rc.ScenarioStatus.RECOMMENDED))
    if plan & {S.ACTIONABLE, S.TEST_REQUIRED}:
        return rc.AssessmentStatus.OPTIMIZATION_AVAILABLE
    if S.REVIEW in plan:
        return rc.AssessmentStatus.REVIEW_REQUIRED
    for s in scenarios:
        total = s.portfolio_economics.savings_365d.total
        if (s.status == rc.ScenarioStatus.ALTERNATIVE and step_statuses(s) and total is not None and total.base > 0
                and s.compatibility.status != rc.CompatibilityStatus.INCOMPATIBLE):
            return rc.AssessmentStatus.REVIEW_REQUIRED
    statuses = set(by_id.values())
    if S.BLOCKED in statuses:
        return rc.AssessmentStatus.BLOCKED
    if S.INSUFFICIENT_EVIDENCE in statuses:
        return rc.AssessmentStatus.INSUFFICIENT_EVIDENCE
    return rc.AssessmentStatus.NO_CHANGE
