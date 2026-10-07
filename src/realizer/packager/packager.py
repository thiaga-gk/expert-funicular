"""Packager facade: record the run in the ledger and project the realization snapshot.

The capabilities before it decide; the Packager records. ``lock`` holds the advisory lock of the package's ledger for a
whole run (``realizer.packager.ledger.ledger_lock``: a second run on the same ledger is refused, and a truncated last
line is quarantined under it). ``begin`` records the lineage (and keeps the copy of its source
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
from typing import Any, ContextManager, Dict, Iterable, Mapping, Optional, Tuple

from realizer.analyzer.lineages import key_of
from realizer.common.utils import load_json, parse_utc
from realizer.config.output_layout import warehouse_lock_path
from realizer.contracts import realization as rc
from realizer.contracts.lineage import LineageRecord
from realizer.contracts.realization import PackagingResult, ReviewResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import RealizerPolicy
from realizer.packager import ledger, lineage
from realizer.packager.snapshot import project_snapshot
from realizer.reconciler.lifecycle import regeneration_request


def lock(context: RunContext) -> ContextManager[None]:
    """The lock of the run's ledger, held from before the lineage is recorded until the Reporter has written its files."""
    return ledger.ledger_lock(ledger.ledger_path(context.request.state_root, context.realization_package_id))


def warehouse_lock(state_dir: Path, workspace_id: Any, warehouse_id: Any) -> ContextManager[None]:
    """The advisory lock of one warehouse in the state (``realization/state/<workspace>__<warehouse>.lock``), held by a
    CLI run while it realizes the warehouse's lineages: a second run of the same warehouse at the same time is refused
    (``LedgerLockedError``) instead of deciding the cross-lineage events from ledgers another run is changing."""
    return ledger.exclusive_lock(warehouse_lock_path(state_dir, key_of(workspace_id, warehouse_id)),
                                 f"warehouse {workspace_id}/{warehouse_id} is locked by another run")


def open_lineages(state_dir: Path, workspace_id: Any, warehouse_id: Any) -> Tuple[LineageRecord, ...]:
    """The lineages of the warehouse kept in the state that a fleet run realizes again, oldest package first
    (``realizer.packager.lineage.open_lineages``)."""
    return lineage.open_lineages(state_dir, workspace_id, warehouse_id)


def warehouse_view(state_dir: Path, workspace_id: Any, warehouse_id: Any, realization_package_ids: Iterable[str]) -> Tuple[
        Tuple[rc.RealizationPackage, ...], Dict[str, Tuple[rc.RealizationLedgerEntry, ...]], Optional[Mapping[str, Any]], Dict[str, Mapping[str, Any]]]:
    """What a fleet run hands to the realization workbook for one warehouse: the latest realization package of each of
    *realization_package_ids* (projected from its whole ledger, as the Reporter last wrote it), in source package id
    order, their ledger entries by realization package id, the warehouse's state index as written (``None`` when there
    is none) and their source packages by package id."""
    wanted = set(realization_package_ids)
    kept = [(record, snapshot) for record, snapshot in lineage.kept_snapshots(state_dir, workspace_id, warehouse_id)
            if record.realization_package_id in wanted]
    kept.sort(key=lambda pair: pair[0].recommendation_package_id)
    path = lineage.state_index_path(state_dir, {"warehouse": {"workspace_id": workspace_id, "warehouse_id": warehouse_id}})
    index = load_json(path) if path.is_file() else None
    return (tuple(snapshot for _, snapshot in kept), {record.realization_package_id: tuple(record.entries) for record, _ in kept}, index,
            {record.recommendation_package_id: record.package for record, _ in kept})


def begin(context: RunContext) -> None:
    """Record the lineage (REALIZATION_CREATED), then keep the copy of its source package that other runs of the
    warehouse find it by. The Analyzer can fail after it, so it is recorded first."""
    ledger.record_creation(context)
    lineage.keep_source(context.request.state_root, context.request.package)


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
    grown = lineage.record_events(request.state_root, context.realization_package_id, request.package, events, others)
    if lineage.recorded_policy(events, reviewed.validation is not None):
        written = written + (lineage.keep_policy(request.state_root, context.policy),)
    entries = ledger.load_ledger(ledger.ledger_path(request.state_root, context.realization_package_id))
    if request.replay_through:
        cut = parse_utc(request.replay_through)
        entries = [e for e in entries if parse_utc(e.recorded_at_utc) <= cut]
    snapshot = project_snapshot(request.package, entries)
    changed, every = lineage.project_others(request.state_root, others, grown)
    return PackagingResult(realization=snapshot, entries=tuple(entries), written=written, others=changed,
                           state_index=lineage.state_index(request.package, [snapshot, *every]))


def request_regeneration(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], output_dir: Path,
    policy: RealizerPolicy, recorded: str, effective: str, *, reason: str, evidence_id: Optional[str] = None,
) -> None:
    """Request fresh Publisher analysis from the application's actual configuration: record it, then write its RERUN file."""
    ledger.append_regeneration(package, output_dir, regeneration_request(package, app_payload, observation, policy, reason=reason, evidence_id=evidence_id), recorded, effective)
