"""Packager facade: record the run in the ledger and project the realization snapshot.

The capabilities before it decide; the Packager records. ``begin`` records the lineage (and keeps the copy of its source
package) before the Analyzer reads anything, ``run`` records every event the other capabilities decided, in the order
the single-file Realizer recorded them (``realizer.packager.ledger.commit_events``), then the cross-lineage events and
the PolicyDiff the Reconciler decided, each in its lineage's ledger (``realizer.packager.lineage.record_events``), keeps
the settings of the policy it recorded under, reloads the ledger, applies the replay cut and projects the snapshot
(``realizer.packager.snapshot.project_snapshot``), re-projects the other lineages it appended to and builds the
warehouse's state index. The final files (the realization packages, the snapshots and the state index) are written by
the Reporter from the result, not here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from realizer.common.utils import utc_iso
from realizer.contracts.realization import PackagingResult, ReviewResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import RealizerPolicy
from realizer.packager import ledger, lineage
from realizer.packager.snapshot import project_snapshot
from realizer.reconciler.lifecycle import regeneration_request


def begin(context: RunContext) -> None:
    """Record the lineage (REALIZATION_CREATED), then keep the copy of its source package that other runs of the
    warehouse find it by. The Analyzer can fail after it, so it is recorded first."""
    ledger.record_creation(context)
    lineage.keep_source(context.request.output_dir, context.request.package)


def run(context: RunContext, reviewed: ReviewResult) -> PackagingResult:
    """Record the run's events, then project the snapshot of the resulting ledger.

    A failure the capabilities kept (``halt``) is raised after the events decided before it, so the ledger a failed run
    leaves is the one it always left, and nothing is appended to another lineage. The cross-lineage events follow the
    run's own events; the ledger is then reloaded and, for a replay (``replay_through``), cut to the entries recorded
    through that time (the cut was checked before the run wrote anything). The cut applies to the run's own lineage;
    the other lineages are projected from their whole ledgers.
    """
    request = context.request
    written = ledger.commit_events(context, reviewed)
    events = reviewed.reconciliation.lineage_events
    others = reviewed.analysis.warehouse.lineages
    grown = lineage.record_events(request.output_dir, context.realization_package_id, request.package, events, others)
    if lineage.recorded_policy(events, reviewed.validation is not None):
        written = written + (lineage.keep_policy(request.output_dir, context.policy),)
    entries = ledger.load_ledger(ledger.ledger_path(request.output_dir, context.realization_package_id))
    if request.replay_through:
        cut = utc_iso(request.replay_through)
        entries = [e for e in entries if e.recorded_at_utc <= cut]
    snapshot = project_snapshot(request.package, entries)
    changed, every = lineage.project_others(request.output_dir, others, grown)
    return PackagingResult(realization=snapshot, entries=tuple(entries), written=written, others=changed,
                           state_index=lineage.state_index(request.package, [snapshot, *every]))


def request_regeneration(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], output_dir: Path,
    policy: RealizerPolicy, recorded: str, effective: str, *, reason: str, evidence_id: Optional[str] = None,
) -> None:
    """Request fresh Publisher analysis from the application's actual configuration: record it, then write its RERUN file."""
    ledger.append_regeneration(package, output_dir, regeneration_request(package, app_payload, observation, policy, reason=reason, evidence_id=evidence_id), recorded, effective)
