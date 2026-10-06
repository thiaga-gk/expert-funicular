"""Realizer Contract v1.0.0: reconciliation states (step conformance, application and plan progress, source-package
disposition, regeneration).

These describe how the observed configuration relates to the immutable PKG plan and where its lifecycle stands. They
are part of the public Realizer Contract and are re-exported by ``realizer.contracts.realization``, which presents the
complete v1.0.0 surface.
"""
from __future__ import annotations

from enum import Enum


class SourcePackageDisposition(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"


class ImplementationConformance(str, Enum):
    EXACT = "EXACT"
    MODIFIED = "MODIFIED"
    NOT_APPLIED = "NOT_APPLIED"
    CONTRARY = "CONTRARY"
    UNRELATED = "UNRELATED"


class ApplicationState(str, Enum):
    NOT_APPLIED = "NOT_APPLIED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    DRIFTED = "DRIFTED"


class PlanProgressState(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


class RegenerationStatus(str, Enum):
    NOT_REQUIRED = "NOT_REQUIRED"
    REQUESTED = "REQUESTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REPLACED = "REPLACED"


__all__ = [
    "SourcePackageDisposition", "ImplementationConformance", "ApplicationState", "PlanProgressState",
    "RegenerationStatus",
]
