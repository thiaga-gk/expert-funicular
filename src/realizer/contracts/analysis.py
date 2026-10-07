"""Realizer Contract v1.0.0: observation-side states (actual-state evidence quality, change origin, recorded intent)
and the Analyzer's hand-off (``RealizationAnalysis``).

The enums are the normalized values a post-change observation carries. They are part of the public Realizer Contract
and are re-exported by ``realizer.contracts.realization``, which presents the complete v1.0.0 surface. The hand-off
dataclasses are internal values passed from the Analyzer to the next capabilities; they are not public contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

if TYPE_CHECKING:  # annotations only: realization.py imports this module for the enums
    from realizer.contracts.realization import RealizationLedgerEntry


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


@dataclass(frozen=True)
class ObservationAnalysis:
    """What one normalized post-change observation says, derived once from the observation and the source package.

    ``raw`` is the observation itself, read lazily by the later capabilities at the points the engine always read it
    (most of its fields are optional and only some branches read them), so a malformed field that no branch reads stays
    harmless. The three times are UTC ISO strings (``observed_at_utc`` falls back to ``recorded_at_utc``,
    ``recorded_at_utc`` to the observed time, ``effective_at_utc`` to ``applied_at_utc``, then the observed time).
    ``observed_config`` is the observation's configuration (omitted knobs inherit the source package's), and
    ``source_config`` the package's; each hash covers the normalized knob values. ``evidence_id`` and
    ``evidence_record`` are the observed-configuration evidence (REVID-*) document the Packager writes.
    """
    raw: Mapping[str, Any]
    observed_at_utc: str
    recorded_at_utc: str
    effective_at_utc: str
    observed_config: Mapping[str, Any]
    source_config: Mapping[str, Any]
    source_config_hash: str
    observed_config_hash: str
    evidence_id: str
    evidence_record: Mapping[str, Any]


@dataclass(frozen=True)
class RealizationHistory:
    """The ledger as it stood when the run started (read only), in recorded order.

    ``previous_application`` is its latest application observation (APPLICATION_OBSERVED or
    MODIFIED_APPLICATION_OBSERVED) and ``prior_measurement`` its latest ECONOMIC_MEASUREMENT_RECORDED, each ``None``
    when there is none. ``closure`` is the latest drift or observed rollback recorded on the previous application's
    episode (``None`` while the episode is open): a closed episode never absorbs a new application.
    ``open_applications`` are the first application entries of the episodes still open, in the order they started
    (several when a plan's steps were applied one after another). Nothing a run appends is ever read back into its own decisions.
    """
    entries: Tuple["RealizationLedgerEntry", ...]
    previous_application: Optional["RealizationLedgerEntry"]
    prior_measurement: Optional["RealizationLedgerEntry"]
    closure: Optional["RealizationLedgerEntry"] = None
    open_applications: Tuple["RealizationLedgerEntry", ...] = ()


@dataclass(frozen=True)
class RealizationAnalysis:
    """Analyzer output. ``observation`` is ``None`` for a lineage-only run (no observation).

    ``config_drift_observed`` is true when the observed configuration hash differs from the one recorded with the
    previous application and the observation is later than that application's record time.
    """
    observation: Optional[ObservationAnalysis]
    history: RealizationHistory
    config_drift_observed: bool


# ``__all__`` is the public Realizer Contract surface (the enums, re-exported by ``realization.py``); the hand-off
# dataclasses above are internal values and are imported by name.
__all__ = ["IntentState", "OriginAttribution", "EvidenceQuality"]
