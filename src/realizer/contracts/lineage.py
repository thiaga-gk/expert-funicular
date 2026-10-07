"""Realizer lineages of one warehouse: the read-side history of the other lineages and the cross-lineage events a run
decides.

An output directory can hold several lineages (RPKG-*) for one warehouse: packages of later Publisher runs, profile or
scope alternatives, a canary rollback after its canary. ``LineageRecord`` is one of them as the Analyzer read it (its
source package and its ledger); ``WarehouseHistory`` is every other lineage of the run's warehouse and the policy
snapshots recorded in the output directory. ``LineageEvent`` is an event the Reconciler decided for a lineage (the run's
own or another), which the Packager appends to that lineage's ledger. These are internal hand-offs, not public contract.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping, Optional, Tuple

if TYPE_CHECKING:  # annotations only
    from realizer.contracts.realization import EventType, RealizationLedgerEntry


@dataclass(frozen=True)
class LineageRecord:
    """Another lineage of the run's warehouse: its source package (the copy the Realizer kept when the lineage was
    created) and its ledger as it stood when the run started, in recorded order."""
    realization_package_id: str
    recommendation_package_id: str
    package: Mapping[str, Any]
    entries: Tuple["RealizationLedgerEntry", ...]


@dataclass(frozen=True)
class WarehouseHistory:
    """What the output directory holds about the run's warehouse besides the run's own lineage: the other lineages
    (by package id) and the recorded policy snapshots (PSNAP id -> policy settings)."""
    lineages: Tuple[LineageRecord, ...] = ()
    policy_snapshots: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class LineageEvent:
    """An event for one lineage's ledger that follows from the lineages of the warehouse taken together (ownership,
    supersession, a successor, a rollback by a later package) or from a policy change (PolicyDiff).

    Its times are derived from the facts it follows from, never from the run that found it, so the same event is the
    same ledger entry whichever run records it."""
    realization_package_id: str
    event_type: "EventType"
    effective_at_utc: str
    recorded_at_utc: str
    payload: Mapping[str, Any]
    application_episode_id: Optional[str] = None
