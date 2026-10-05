"""Candidate savings arithmetic (SavingsModel mixin)."""
from __future__ import annotations

from typing import Tuple

from publisher.contracts.candidate import Range3


class SavingsModel:
    """Savings arithmetic: cost ranges from factors and savings deltas against a baseline. Mixed into the Estimator; ``self.t`` is the policy."""

    def _range_from_factor(self,baseline:float,factors:Range3)->Tuple[Range3,Range3]:
        costs=Range3(baseline*factors.low,baseline*factors.base,baseline*factors.high).rounded()
        savings=Range3(max(0,baseline-costs.high),max(0,baseline-costs.base),max(0,baseline-costs.low)).rounded()
        return costs,savings

    @staticmethod
    def _delta_range(baseline: float, costs: Range3) -> Range3:
        return Range3(round(baseline-costs.high,2), round(baseline-costs.base,2), round(baseline-costs.low,2))
