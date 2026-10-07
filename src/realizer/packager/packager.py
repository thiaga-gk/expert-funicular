"""Packager facade: record the run in the ledger and project the realization snapshot.

The capabilities before it decide; the Packager records. ``begin`` records the lineage before the Analyzer reads
anything, ``run`` records every event the other capabilities decided, in the order the single-file Realizer recorded
them (``realizer.packager.ledger.commit_events``), reloads the ledger, applies the replay cut and projects the snapshot
(``realizer.packager.snapshot.project_snapshot``). The two final files (the realization package and the snapshot) are
written by the engine from the result, not here.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from realizer.common.utils import parse_utc
from realizer.contracts.realization import PackagingResult, ReviewResult
from realizer.contracts.request import RunContext
from realizer.governor.policy import RealizerPolicy
from realizer.packager import ledger
from realizer.packager.snapshot import project_snapshot
from realizer.reconciler.lifecycle import regeneration_request


def begin(context: RunContext) -> None:
    """Record the lineage (REALIZATION_CREATED). The Analyzer can fail after it, so it is recorded first."""
    ledger.record_creation(context)


def run(context: RunContext, reviewed: ReviewResult) -> PackagingResult:
    """Record the run's events, then project the snapshot of the resulting ledger.

    A failure the capabilities kept (``halt``) is raised after the events decided before it, so the ledger a failed run
    leaves is the one it always left. The ledger is then reloaded and, for a replay (``replay_through``), cut to the
    entries recorded through that time (the cut was checked before the run wrote anything).
    """
    request = context.request
    written = ledger.commit_events(context, reviewed)
    entries = ledger.load_ledger(ledger.ledger_path(request.output_dir, context.realization_package_id))
    if request.replay_through:
        cut = parse_utc(request.replay_through)
        entries = [e for e in entries if parse_utc(e.recorded_at_utc) <= cut]
    snapshot = project_snapshot(request.package, entries)
    return PackagingResult(realization=snapshot, entries=tuple(entries), written=written)


def request_regeneration(
    package: Mapping[str, Any], app_payload: Mapping[str, Any], observation: Mapping[str, Any], output_dir: Path,
    policy: RealizerPolicy, recorded: str, effective: str, *, reason: str, evidence_id: Optional[str] = None,
) -> None:
    """Request fresh Publisher analysis from the application's actual configuration: record it, then write its RERUN file."""
    ledger.append_regeneration(package, output_dir, regeneration_request(package, app_payload, observation, policy, reason=reason, evidence_id=evidence_id), recorded, effective)
