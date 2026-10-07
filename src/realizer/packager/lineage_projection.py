"""Snapshot projection of the cross-lineage events: who owns the active application's change, which package replaced
the source package and which package succeeded it.

Each helper reads ledger entries only and decides nothing: the Reconciler decided the events. Where a lineage holds
several events of one kind (a later run found a newer owner or replacement), the one the projection uses is chosen from
the event payloads, never from the order the events were recorded in, so the snapshot does not depend on the order in
which the packages were realized.
"""
from __future__ import annotations

import datetime as dt
from typing import Any, Dict, List, Mapping, Optional, Sequence

from realizer.common.utils import utc_iso
from realizer.contracts import realization as rc
from realizer.estimator.attribution import claimed_by_other_lineage


def _stamp(value: Any) -> float:
    return dt.datetime.fromisoformat(utc_iso(value).replace("Z", "+00:00")).timestamp()


def _rank(generated: Any, conformance: Any, package_id: Any) -> tuple:
    """Most recently generated first, then EXACT before MODIFIED, then the lowest package id (the ownership rule)."""
    return (-_stamp(generated), 0 if conformance == rc.ImplementationConformance.EXACT.value else 1, str(package_id))


def owners(entries: Sequence[rc.RealizationLedgerEntry], application_episode_id: Optional[str]) -> List[Dict[str, Any]]:
    """The owner of each knob of the episode's change another lineage owns (the best-ranked owner its
    ``APPLICATION_CLAIMED_BY_OTHER_LINEAGE`` entries name), in knob order; empty when the episode owns its change."""
    if not application_episode_id:
        return []
    best: Dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        if entry.event_type != rc.EventType.APPLICATION_CLAIMED_BY_OTHER_LINEAGE or entry.application_episode_id != application_episode_id:
            continue
        p = entry.payload
        knob = str(p.get("knob"))
        if knob not in best or _rank(p.get("owner_generated_at_utc"), p.get("owner_implementation_conformance"), p.get("owner_recommendation_package_id")) < \
                _rank(best[knob].get("owner_generated_at_utc"), best[knob].get("owner_implementation_conformance"), best[knob].get("owner_recommendation_package_id")):
            best[knob] = p
    return [{"knob": knob, "realization_package_id": p.get("owner_realization_package_id"),
             "recommendation_package_id": p.get("owner_recommendation_package_id")} for knob, p in sorted(best.items())]


def attribution(attr: Mapping[str, Any], entries: Sequence[rc.RealizationLedgerEntry], application_episode_id: Optional[str]) -> Dict[str, Any]:
    """The snapshot's attribution with the exclusivity rule applied when another lineage owns the active change."""
    return claimed_by_other_lineage(attr, owners(entries, application_episode_id))


def replacement(entries: Sequence[rc.RealizationLedgerEntry]) -> Optional[rc.RealizationLedgerEntry]:
    """The ``REPLACEMENT_PACKAGE_LINKED`` entry in force: the latest replacement a caller named (an explicit
    replacement), else the newest replacement package an automatic supersession names (newest generated, then the
    lowest package id)."""
    linked = [e for e in entries if e.event_type == rc.EventType.REPLACEMENT_PACKAGE_LINKED]
    named = [e for e in linked if not e.payload.get("replacement_generated_at_utc")]
    if named:
        return named[-1]
    return min(linked, key=lambda e: (-_stamp(e.payload["replacement_generated_at_utc"]), str(e.payload.get("replacement_package_id"))), default=None)


def successor_package_id(entries: Sequence[rc.RealizationLedgerEntry]) -> Optional[str]:
    """The first successor package linked to the lineage (earliest generated, then the lowest package id)."""
    linked = [e.payload for e in entries if e.event_type == rc.EventType.SUCCESSOR_PACKAGE_LINKED]
    if not linked:
        return None
    first = min(linked, key=lambda p: (_stamp(p.get("successor_generated_at_utc")), str(p.get("successor_package_id"))))
    return first.get("successor_package_id")
