"""Reconciler re-evaluation under a changed policy: the explicit PolicyDiff of an active application episode.

A closed validation keeps the policy snapshot it was evaluated under; history is never rewritten. When a run evaluates
an active application episode under a policy whose snapshot (PSNAP-*) differs from the one its last validation (or last
PolicyDiff) recorded, the run records ``POLICY_DIFF_RECORDED`` on that episode: the previous and the new snapshot ids and
each changed setting with its previous and new value (``null`` when the previous policy's settings were not recorded in
the output directory, so the change is never guessed).
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional

from realizer.contracts import realization as rc
from realizer.contracts.analysis import RealizationAnalysis
from realizer.contracts.lineage import LineageEvent
from realizer.contracts.reconciliation import ReconciliationResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import policy_to_dict

# Entries that record the policy an episode was last evaluated under.
POLICY_EVENTS = (rc.EventType.VALIDATION_READY, rc.EventType.VALIDATION_COMPLETED, rc.EventType.POLICY_DIFF_RECORDED)


def changed_settings(previous: Optional[Mapping[str, Any]], current: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """Each setting whose value differs, ``{field, previous, current}`` in field order; ``None`` without the previous
    settings."""
    if previous is None:
        return None
    return [{"field": k, "previous": previous.get(k), "current": current.get(k)} for k in sorted(set(previous) | set(current))
            if previous.get(k) != current.get(k)]


def policy_diff(context: RunContext, analysis: RealizationAnalysis, result: ReconciliationResult) -> Optional[LineageEvent]:
    """The PolicyDiff of the run's active application episode, or ``None`` (no active episode, never evaluated, or
    evaluated under the same policy). A policy whose snapshot cannot be computed records no diff here: the run fails
    where the snapshot has always been read, the validation episode."""
    observed, active = analysis.observation, result.active
    if observed is None or active is None:
        return None
    last = None
    for entry in analysis.history.entries:
        if entry.application_episode_id == active.application_episode_id and entry.event_type in POLICY_EVENTS:
            last = entry.payload.get("policy_snapshot_id") or last
    if last is None:
        return None
    try:
        current_id = context.policy.snapshot_id
    except Exception:  # noqa: BLE001 - a policy that cannot be hashed fails where its snapshot has always been read (the validation)
        return None
    if last == current_id:
        return None
    current = policy_to_dict(context.policy)
    return LineageEvent(
        realization_package_id=context.realization_package_id, event_type=rc.EventType.POLICY_DIFF_RECORDED,
        effective_at_utc=observed.observed_at_utc, recorded_at_utc=observed.recorded_at_utc,
        application_episode_id=active.application_episode_id,
        payload={
            "previous_policy_snapshot_id": last,
            "policy_snapshot_id": current_id,
            "changed_settings": changed_settings(analysis.warehouse.policy_snapshots.get(last), current),
            "observed_at_utc": observed.observed_at_utc,
        },
    )
