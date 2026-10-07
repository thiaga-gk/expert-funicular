"""Reviewer economics: how far the measured value is recognized.

A measurement is CONFIRMED when the hard guardrails passed and the counterfactual baseline supports a claim, REJECTED
when the guardrails failed, and PROVISIONAL otherwise, including a canary-type change inside its guard window. Moved unchanged from the single-file Realizer's measurement
payload.
"""
from __future__ import annotations

from realizer.contracts import realization as rc


def recognition_status(validation_passed: bool, counterfactual_claim_eligible: bool, provisional: bool = False) -> str:
    """The recognition status of a measurement from the guardrail verdict and the counterfactual's claim eligibility;
    PROVISIONAL while a canary-type change is still inside its guard window (*provisional*)."""
    if not validation_passed:
        return rc.RecognitionStatus.REJECTED.value
    return rc.RecognitionStatus.CONFIRMED.value if counterfactual_claim_eligible and not provisional else rc.RecognitionStatus.PROVISIONAL.value
