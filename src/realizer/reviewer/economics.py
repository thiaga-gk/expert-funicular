"""Reviewer economics: how far the measured value is recognized.

A measurement is CONFIRMED when the hard guardrails passed and the counterfactual baseline supports a claim, REJECTED
when the guardrails failed, and PROVISIONAL otherwise. Moved unchanged from the single-file Realizer's measurement
payload.
"""
from __future__ import annotations

from realizer.contracts import realization as rc


def recognition_status(validation_passed: bool, counterfactual_claim_eligible: bool) -> str:
    """The recognition status of a measurement from the guardrail verdict and the counterfactual's claim eligibility."""
    return rc.RecognitionStatus.CONFIRMED.value if validation_passed and counterfactual_claim_eligible else rc.RecognitionStatus.REJECTED.value if not validation_passed else rc.RecognitionStatus.PROVISIONAL.value
