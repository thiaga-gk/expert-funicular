"""Realizer Contract v1.0.0: observation-side states (actual-state evidence quality, change origin, recorded intent).

These are the normalized values a post-change observation carries. They are part of the public Realizer Contract and
are re-exported by ``realizer.contracts.realization``, which presents the complete v1.0.0 surface.
"""
from __future__ import annotations

from enum import Enum


class IntentState(str, Enum):
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DEFERRED = "DEFERRED"


class OriginAttribution(str, Enum):
    CONFIRMED = "CONFIRMED"
    INFERRED = "INFERRED"
    UNKNOWN = "UNKNOWN"
    INDEPENDENT = "INDEPENDENT"
    NOT_ATTRIBUTABLE = "NOT_ATTRIBUTABLE"


class EvidenceQuality(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    PARTIAL = "PARTIAL"
    INSUFFICIENT = "INSUFFICIENT"
    CONFLICTING = "CONFLICTING"
    STALE = "STALE"


__all__ = ["IntentState", "OriginAttribution", "EvidenceQuality"]
